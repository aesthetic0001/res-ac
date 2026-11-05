import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from test_agent import agent


class PersistentHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[2]
        cls.helper = Path(os.environ.get('RESAC_HTTP_HELPER', root / 'private/http-build/res-ac-http-native'))
        if not cls.helper.is_file():
            raise unittest.SkipTest('Build the native helper or set RESAC_HTTP_HELPER for transport integration tests')
        cls.directory = tempfile.TemporaryDirectory()
        cls.cert = str(Path(cls.directory.name) / 'cert.pem')
        key = str(Path(cls.directory.name) / 'key.pem')
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', key, '-out', cls.cert], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cls.cert, key)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.received = []
        self.held = threading.Event()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_GET(self):
                outer.received.append((self.path, self.client_address, self.headers, None))
                outer.held.set()
                outer.release.wait(5)
                self.reply(204, b'')

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.received.append((self.path, self.client_address, self.headers, value))
                if value.get('hold'):
                    outer.held.set()
                    outer.release.wait(5)
                body = b'x' * value['bytes'] if 'bytes' in value else b'{"ok":true}'
                self.reply(201, body, value.get('close', False), value.get('chunked', False))

            def reply(self, status, body, close=False, chunked=False):
                try:
                    self.send_response(status)
                    self.send_header('Transfer-Encoding' if chunked else 'Content-Length',
                                     'chunked' if chunked else str(len(body)))
                    if close:
                        self.send_header('Connection', 'close')
                    self.end_headers()
                    if chunked:
                        for offset in range(0, len(body), 2048):
                            block = body[offset:offset + 2048]
                            self.wfile.write(('%x\r\n' % len(block)).encode() + block + b'\r\n')
                        self.wfile.write(b'0\r\n\r\n')
                    else:
                        self.wfile.write(body)
                except (OSError, ssl.SSLError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.socket = self.context.wrap_socket(self.server.socket, server_side=True)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.config = dict(server_url='https://127.0.0.1:%d' % self.server.server_port,
                           device_id='board', device_token='PRIVATE_TOKEN', ca_file=self.cert,
                           http_helper=str(self.helper))
        self.http = agent.HttpTransport(self.config)
        self.config['_http'] = self.http
        self.uploads = []

    def tearDown(self):
        self.release.set()
        for upload in self.uploads:
            upload.close()
        self.http.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def upload(self, payload=None, **options):
        request = agent.CurlUpload(self.config, payload, **options)
        self.uploads.append(request)
        return request

    def completed(self, request, timeout=3):
        deadline = time.monotonic() + timeout
        while not request.completed:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, 'No HTTP completion event')
            self.http.wait(timeout=remaining)

    def test_reuses_one_process_and_tls_connection_without_request_files(self):
        real_popen = subprocess.Popen
        with patch.object(agent, 'temporary', side_effect=AssertionError('Request file created')), \
                patch.object(agent.subprocess, 'Popen', wraps=real_popen) as spawned:
            connections = []
            for number in range(3):
                request = self.upload(dict(readingId='sample-%d' % number, traceparent=agent.traceparent()))
                self.completed(request)
                self.assertEqual(request.finish(), (201, b'{"ok":true}'))
                connections.append(request.result.connections)
                self.assertEqual(request.files, [])
                request.close()
            self.assertEqual(spawned.call_count, 1)
            arguments = spawned.call_args.args[0]
            self.assertEqual(arguments, [str(self.helper)])
            self.assertNotIn(self.config['device_token'], ' '.join(arguments))
        self.assertEqual(connections, [1, 0, 0])
        self.assertEqual(len({item[1] for item in self.received}), 1)
        for request in self.received:
            self.assertEqual(request[2]['Authorization'], 'Bearer PRIVATE_TOKEN')
            self.assertRegex(request[2]['traceparent'], agent.TRACEPARENT)

    def test_held_poll_does_not_block_upload_and_completion_wakes_wait(self):
        poll = self.upload(route='commands', method='GET', timeout=5)
        reading = self.upload(dict(readingId='concurrent', traceparent=agent.traceparent()))
        self.completed(reading)
        self.assertTrue(self.held.wait(1))
        self.assertFalse(poll.completed)
        self.assertEqual(reading.finish()[0], 201)
        self.assertEqual(len({item[1] for item in self.received}), 2)
        self.release.set()
        self.completed(poll)
        self.assertEqual(poll.finish()[0], 204)

    def test_helper_exit_fails_pending_attempt_and_restarts_without_replay(self):
        telemetry = agent.Telemetry(self.config)
        self.config['_telemetry'] = telemetry
        request = self.upload(dict(readingId='retained', hold=True, traceparent=agent.traceparent()))
        self.http.wait(timeout=0)
        self.assertTrue(self.held.wait(2))
        process_id = self.http.process.pid
        self.http.process.kill()
        self.completed(request)
        with self.assertRaises(agent.UploadError) as failure:
            request.finish()
        self.assertEqual(failure.exception.curl_code, 2)
        self.assertEqual(telemetry.events[-1]['attributes']['error.stage'], 'local')
        request.close()
        self.release.set()
        retried = self.upload(dict(readingId='retained', traceparent=agent.traceparent()))
        self.completed(retried)
        self.assertNotEqual(self.http.process.pid, process_id)
        self.assertEqual(retried.finish()[0], 201)
        self.assertEqual([item[3]['readingId'] for item in self.received], ['retained', 'retained'])

    def test_hostname_verification_remains_enabled(self):
        self.http.close()
        self.config['server_url'] = 'https://localhost:%d' % self.server.server_port
        self.http = agent.HttpTransport(self.config)
        self.config['_http'] = self.http
        request = self.upload({})
        self.completed(request)
        with self.assertRaises(agent.UploadError) as failure:
            request.finish()
        self.assertEqual(failure.exception.curl_code, 60)
        self.assertFalse(self.received)

    def test_response_limit_is_enforced_without_content_length_trust(self):
        request = self.upload(dict(bytes=32769, chunked=True))
        self.completed(request)
        with self.assertRaises(agent.UploadError) as failure:
            request.finish()
        self.assertEqual(failure.exception.curl_code, 23)
        self.assertLessEqual(len(request.result.body), 32768)

    def test_timeout_is_delivered_as_completion_with_sanitized_diagnostics(self):
        telemetry = agent.Telemetry(self.config)
        self.config['_telemetry'] = telemetry
        request = self.upload(dict(hold=True, traceparent=agent.traceparent()), timeout=1)
        self.completed(request)
        with self.assertRaises(agent.UploadError) as failure:
            request.finish()
        self.assertEqual(failure.exception.curl_code, 28)
        self.assertEqual(telemetry.events[-1]['attributes']['outcome'], 'timeout')
        self.assertIn('http.timing.total_ms', telemetry.events[-1]['attributes'])
        self.assertNotIn('PRIVATE_TOKEN', str(telemetry.events))
        self.assertNotIn('127.0.0.1', str(telemetry.events))

    def test_timeout_on_reused_connection_does_not_claim_dns_failure(self):
        first = self.upload({})
        self.completed(first)
        first.finish()
        first.close()
        telemetry = agent.Telemetry(self.config)
        self.config['_telemetry'] = telemetry
        second = self.upload(dict(hold=True), timeout=1)
        self.completed(second)
        with self.assertRaises(agent.UploadError):
            second.finish()
        self.assertEqual(second.result.connections, 0)
        self.assertEqual(telemetry.events[-1]['attributes']['error.stage'], 'unknown')

    def test_untrusted_certificate_is_rejected(self):
        self.http.close()
        self.config['ca_file'] = '/etc/ssl/certs/ca-certificates.crt'
        self.http = agent.HttpTransport(self.config)
        self.config['_http'] = self.http
        request = self.upload({})
        self.completed(request)
        with self.assertRaises(agent.UploadError) as failure:
            request.finish()
        self.assertEqual(failure.exception.curl_code, 60)
        self.assertFalse(self.received)

    def test_orphaned_helper_exits_on_ipc_eof(self):
        parent, child = socket.socketpair()
        process = subprocess.Popen([str(self.helper)], stdin=child, stdout=child, stderr=subprocess.DEVNULL)
        child.close()
        try:
            parent.close()
            process.wait(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_closed_idle_connection_is_replaced_by_libcurl(self):
        first = self.upload(dict(close=True))
        self.completed(first)
        self.assertEqual(first.finish()[0], 201)
        first.close()
        second = self.upload({})
        self.completed(second)
        self.assertEqual(second.finish()[0], 201)
        self.assertEqual(second.result.connections, 1)

    def test_standalone_probe_uses_same_helper_and_completion_events(self):
        config = dict(self.config)
        del config['_http']
        self.assertEqual(agent.post(config, {})[0], 201)

    def test_cancellation_does_not_disrupt_other_requests(self):
        poll = self.upload(route='commands', method='GET', timeout=5)
        self.http.wait(timeout=0)
        poll.close()
        reading = self.upload({})
        self.completed(reading)
        self.assertEqual(reading.finish()[0], 201)


class SchedulerTests(unittest.TestCase):
    def test_next_reading_starts_before_background_work_after_completion(self):
        events, requests, waits = [], [], []
        class Collector:
            def __init__(self, config, pending): self.pending, self.ticks = pending, 0
            def tick(self):
                self.ticks += 1
                if self.ticks == 1:
                    for number in (1, 2): self.pending.add(dict(readingId=str(number)))
                if self.ticks == 3: raise KeyboardInterrupt
            def report_upload(self, value): pass
        class Commands:
            current, request = None, object()
            def __init__(self, *args, **kwargs): pass
            def tick(self, **kwargs): events.append('commands')
            def close(self): pass
        class Automation:
            busy, runner, next_due = False, None, 103
            def __init__(self, *args, **kwargs): pass
            def tick(self, commands): pass
            def manual_override(self): pass
            def configure(self, payload): pass
        class Fan:
            next_due = 101
            def __init__(self, *args, **kwargs): pass
            def tick(self): events.append('fan')
            def close(self): pass
        class Telemetry:
            request = object()
            def __init__(self, *args): pass
            def event(self, *args, **kwargs): pass
            def tick(self, pending): events.append('telemetry')
            def close(self): pass
        class Upload:
            completed = False
            def __init__(self, config, sample):
                self.id = sample['readingId']
                requests.append(self)
                events.append('start-' + self.id)
            def finish(self):
                events.append('finish-' + self.id)
                return 201, b'{}'
            def close(self): pass
        class Http:
            def wait(self, bridge, timeout):
                waits.append(timeout)
                if timeout:
                    requests[-1].completed = True
                    events.append('event-' + requests[-1].id)
            def close(self): pass
        bridge = SimpleNamespace(connection=object(), ready=True, close=lambda: None)
        with patch.object(agent, 'Collector', Collector), patch.object(agent, 'CommandProcessor', Commands), \
                patch.object(agent, 'AutoController', Automation), patch.object(agent, 'FanReporter', Fan), \
                patch.object(agent, 'Telemetry', Telemetry), patch.object(agent, 'CurlUpload', Upload), \
                patch.object(agent, 'monotonic', return_value=100):
            with self.assertRaises(KeyboardInterrupt):
                agent.run(dict(_bridge=bridge, _http=Http()))
        start = events.index('event-1')
        next_reading = events.index('start-2')
        self.assertLess(events.index('finish-1'), next_reading)
        self.assertLess(next_reading, events.index('fan', start))
        self.assertLess(next_reading, events.index('telemetry', start))
        self.assertTrue(all(timeout in (0, 1) for timeout in waits))

    def test_latest_fan_state_flushes_on_completion_without_heartbeat_wait(self):
        from test_bridge_events import ready_bridge, deliver, event, SESSION
        bridge = ready_bridge()
        requests = []
        def request(*args, **kwargs):
            value = SimpleNamespace(completed=False, finish=lambda: (200, b'{}'), close=lambda: None)
            requests.append((args[1], value))
            return value
        with patch.object(agent, 'CurlUpload', side_effect=request), patch.object(agent, 'monotonic', return_value=100):
            reporter = agent.FanReporter(dict(_bridge=bridge))
            reporter.tick()
            deliver(bridge, event('resac_fan_state', 'v2,%s,1,1,2,2,manual' % SESSION))
            reporter.tick()
            self.assertEqual(len(requests), 1)
            requests[0][1].completed = True
            reporter.tick()
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1][0]['speed'], 2)
            self.assertEqual(bridge.outgoing.count(b'resac_fan_heartbeat'), 1)

    def test_bridge_event_received_before_select_is_not_delayed_until_timer(self):
        class Bridge(agent.BridgeEvents):
            def poll(self, timeout=0):
                self._update('resac_sensor_status', 'ok')
        bridge = Bridge()
        http = agent.HttpTransport({})
        with patch.object(agent.select, 'select', return_value=([], [], [])) as selected:
            http.wait(bridge, timeout=10)
        self.assertEqual(selected.call_args.args[3], 0)

    def test_wait_uses_heartbeat_deadline_instead_of_fixed_completion_poll(self):
        with patch.object(agent, 'monotonic', return_value=100):
            bridge = SimpleNamespace(connection=object(), ready=True)
            telemetry = SimpleNamespace(request=object())
            commands = SimpleNamespace(current=None, request=object())
            automation = SimpleNamespace(runner=None, busy=False, next_due=103)
            fan = SimpleNamespace(next_due=101)
            timeout = agent.next_wakeup(bridge, telemetry, commands, automation, fan, agent.Buffer(1), object(), 0)
            self.assertEqual(timeout, 1)

    def test_completion_state_does_not_poll_subprocess(self):
        result = agent.HttpResult(1)
        request = agent.CurlUpload.__new__(agent.CurlUpload)
        request.result = result
        request.process = SimpleNamespace(poll=lambda: self.fail('Subprocess completion was polled'))
        self.assertFalse(agent.request_completed(request))
        result.completed = True
        self.assertTrue(agent.request_completed(request))
