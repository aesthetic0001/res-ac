import importlib.util
import json
import os
from pathlib import Path
import secrets
import socketserver
import ssl
import uuid
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer

spec = importlib.util.spec_from_file_location('agent', Path(__file__).resolve().parents[1] / 'res_ac_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentTests(unittest.TestCase):
    def test_frame_validation_and_stable_retry_ids(self):
        epoch = secrets.token_hex(16)
        frame = 'v1,%s,7,-1250,48500,1000' % epoch
        sample = agent.parse_sample(frame)
        self.assertEqual(sample['temperatureC'], -1.25)
        self.assertEqual(sample['humidity'], 48.5)
        self.assertEqual(sample['readingId'], agent.parse_sample(frame)['readingId'])
        for invalid in [frame.replace('v1', 'v2'), 'v1,%s,0,1,1,1' % epoch,
                        'v1,%s,1,NaN,1,1' % epoch, 'v1,%s,1,0,100001,1' % epoch,
                        'v1,%s,1,0,1,-1' % epoch]:
            with self.assertRaises(ValueError):
                agent.parse_sample(invalid)

    def test_buffer_is_bounded_deduplicates_and_handles_inflight_eviction(self):
        queue = agent.Buffer(2)
        first, second, third = [dict(readingId=str(i)) for i in range(3)]
        self.assertTrue(queue.add(first))
        self.assertFalse(queue.add(first))
        self.assertIs(queue.first(), first)
        queue.add(second)
        queue.add(third)
        self.assertEqual(queue.dropped, 1)
        queue.remove(first)  # An overflow eviction during upload must not remove a newer sample.
        self.assertIs(queue.first(), second)
        queue.remove(second)
        self.assertIs(queue.first(), third)

    def test_legacy_json_read_write_adapter(self):
        class Legacy:
            write = staticmethod(json.dumps)
            read = staticmethod(json.JSONDecoder().raw_decode)
        with patch.object(agent, 'json', Legacy):
            value = {'readingId': 'sample', 'temperatureC': 22.5, 'humidity': 48}
            self.assertEqual(agent.decode_json(agent.encode_json(value)), value)
            with self.assertRaises(ValueError):
                agent.decode_json('{} garbage')
        class LegacyObject:
            write = staticmethod(json.dumps)
            read = staticmethod(json.loads)
        with patch.object(agent, 'json', LegacyObject):
            self.assertEqual(agent.decode_json(agent.encode_json(value)), value)
        class BytesOnlyLegacy:
            @staticmethod
            def read(value):
                if not isinstance(value, bytes):
                    raise ValueError('Legacy firmware requires bytes')
                return json.loads(value)
        with patch.object(agent, 'json', BytesOnlyLegacy), patch.object(agent.sys, 'version_info', (2, 7)):
            self.assertEqual(agent.decode_json('{"sensor":"ok"}'), {'sensor': 'ok'})

    def test_bridge_fragmented_response_and_boot_epoch(self):
        values = {}
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                raw = b''
                while True:
                    raw += self.request.recv(1024)
                    try:
                        request = json.loads(raw)
                        break
                    except ValueError:
                        pass
                if request['command'] == 'put':
                    values[request['key']] = request['value']
                response = json.dumps({'key': request['key'], 'value': values.get(request['key'])}).encode()
                for start in range(0, len(response), 3):
                    self.request.sendall(response[start:start + 3])
        server = socketserver.TCPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            bridge = agent.BridgeStore(port=server.server_address[1])
            self.assertIsNone(bridge.get('resac_epoch'))
            epoch = secrets.token_hex(16)
            bridge.put('resac_epoch', epoch)
            self.assertEqual(bridge.get('resac_epoch'), epoch)
            pending = agent.Buffer(2)
            values.clear()
            values['resac_sensor_status'] = 'ok'
            # A collector restart initializes a fresh epoch/interval before reading frames.
            original_get = bridge.get
            def read(key):
                if key == 'resac_sample':
                    return 'v1,%s,1,22500,48000,1000' % values['resac_epoch']
                return original_get(key)
            with patch.object(bridge, 'get', read):
                collector = agent.Collector({'interval_seconds': 10}, pending, bridge)
                collector.tick()
            self.assertEqual(values['resac_interval_ms'], '10000')
            self.assertEqual(pending.first()['temperatureC'], 22.5)
            prior_epoch = values['resac_epoch']
            prior_id = pending.first()['readingId']
            values['resac_epoch'] = ''  # AVR reboot resets its sample sequence.
            collector.next_due = 0
            with patch.object(bridge, 'get', read):
                collector.tick()
            self.assertNotEqual(values['resac_epoch'], prior_epoch)
            self.assertNotEqual(pending.queue[-1]['readingId'], prior_id)
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_verified_https_post_hides_token_from_process_arguments_and_cleans_files(self):
        with tempfile.TemporaryDirectory() as directory:
            cert = str(Path(directory) / 'cert.pem')
            key = str(Path(directory) / 'key.pem')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-keyout', key, '-out', cert], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            received = []
            ping_id = str(uuid.uuid4())
            offered = []
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    received.append((self.path, self.headers['Authorization'], None))
                    commands = [] if offered else [dict(id=ping_id, type='device.ping', payload={})]
                    offered.append(True)
                    self.send_response(200 if commands else 204)
                    self.end_headers()
                    if commands:
                        self.wfile.write(json.dumps(dict(commands=commands)).encode())
                def do_POST(self):
                    received.append((self.path, self.headers['Authorization'], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    self.send_response(200 if self.path.endswith('/result') else 201)
                    self.end_headers(); self.wfile.write(b'{"duplicate":false}')
                def log_message(self, *args):
                    pass
            server = HTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            config = dict(server_url='https://127.0.0.1:%s' % server.server_port,
                          device_id='test-board', device_token=secrets.token_urlsafe(32), ca_file=cert)
            sample = dict(readingId='test-1', temperatureC=22.5, humidity=48)
            real_popen = subprocess.Popen
            files = []
            def inspect(arguments, **kwargs):
                self.assertNotIn(config['device_token'], ' '.join(arguments))
                self.assertNotIn(config['server_url'], ' '.join(arguments))
                path = arguments[arguments.index('--config') + 1]
                files.append(path)
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                return real_popen(arguments, **kwargs)
            try:
                with patch.object(agent.subprocess, 'Popen', inspect):
                    self.assertEqual(agent.post(config, sample)[0], 201)
                self.assertEqual(received[0], ('/api/devices/test-board/readings', 'Bearer ' + config['device_token'], sample))
                class Arduino:
                    def __init__(self):
                        self.response = ''
                    def get(self, key):
                        return 'v1' if key == 'resac_command_ready' else self.response
                    def put(self, key, value):
                        if key == 'resac_command' and value:
                            self.response = 'v1,%s,pong,4321' % ping_id
                processor = agent.CommandProcessor(config, Arduino(), str(Path(directory) / 'ping.json'))
                try:
                    with patch.object(agent.subprocess, 'Popen', inspect):
                        deadline = time.monotonic() + 5
                        while time.monotonic() < deadline:
                            processor.tick()
                            if processor.cached and processor.reply is None:
                                break
                            time.sleep(0.01)
                    self.assertIsNotNone(processor.cached)
                    self.assertIsNone(processor.reply)
                    self.assertIn(('/api/devices/test-board/commands', 'Bearer ' + config['device_token'], None), received)
                    self.assertIn(('/api/devices/test-board/commands/%s/result' % ping_id,
                                   'Bearer ' + config['device_token'],
                                   dict(status='succeeded', result=dict(reply='pong', uptimeMs=4321), traceparent=processor.cached['traceparent'])), received)
                finally:
                    processor.close()
                self.assertTrue(all(not Path(path).exists() for path in files))
                # Trusting the issuer must not accept a different server identity.
                origin = config['server_url']
                config['server_url'] = 'https://localhost:%d' % server.server_port
                with self.assertRaises(agent.UploadError):
                    agent.post(config, sample)
                config['server_url'] = origin
                config['ca_file'] = str(Path(directory) / 'untrusted.pem')
                with open(config['ca_file'], 'w') as stream:
                    stream.write('not a trusted CA')
                with self.assertRaises(agent.UploadError):
                    agent.post(config, sample)
            finally:
                server.shutdown(); server.server_close(); worker.join()

    def test_sampling_continues_while_an_upload_is_pending(self):
        ticks = []
        closed = []
        command_ticks = []
        class Commands:
            def __init__(self, config, **kwargs):
                pass
            def tick(self, **kwargs):
                command_ticks.append(True)
            def close(self):
                closed.append(True)
        class FanReporter:
            def __init__(self, config, **kwargs): pass
            def tick(self): pass
            def close(self): pass
        class AutoController:
            busy = False
            def __init__(self, config): pass
            def tick(self, commands): pass
            def manual_override(self): pass
            def configure(self, payload): pass
        class Collector:
            def __init__(self, config, pending):
                self.pending = pending
            def tick(self):
                ticks.append(True)
                if len(ticks) == 1:
                    self.pending.add(dict(readingId='epoch-1', temperatureC=20, humidity=50))
                if len(ticks) == 7:
                    raise KeyboardInterrupt
            def report_upload(self, status):
                pass
        class Process:
            def __init__(self):
                self.checks = 0
            def poll(self):
                self.checks += 1
                return None if self.checks < 4 else 0
        class Upload:
            def __init__(self, config, sample):
                self.process = Process()
            def finish(self):
                return 201, b'{}'
            def close(self):
                closed.append(True)
        with patch.object(agent, 'AutoController', AutoController), patch.object(agent, 'FanReporter', FanReporter), patch.object(agent, 'Collector', Collector), patch.object(agent, 'CurlUpload', Upload), \
                patch.object(agent, 'CommandProcessor', Commands), patch.object(agent.time, 'sleep'):
            with self.assertRaises(KeyboardInterrupt):
                agent.run(dict(queue_limit=2))
        self.assertEqual(len(ticks), 7)
        self.assertEqual(len(command_ticks), 6)
        self.assertTrue(closed)

    def test_ping_round_trip_retries_and_restart_preserve_result(self):
        command_id = str(uuid.uuid4())
        calls = []
        class Request:
            def __init__(self, config, payload=None, **options):
                self.payload, self.options = payload, options
                self.response = None
                self.process = self
                self.closed = False
                calls.append(self)
            def poll(self):
                return 0 if self.response is not None else None
            def finish(self):
                return self.response
            def close(self):
                self.closed = True
        class Bridge:
            def __init__(self):
                self.values = {}
                self.deliveries = []
                self.ready = False
            def get(self, key):
                return ('v1' if self.ready else '') if key == 'resac_command_ready' else self.values.get(key)
            def put(self, key, value):
                self.values[key] = value
                if key == 'resac_command' and value:
                    self.deliveries.append(value)
        bridge = Bridge()
        clock = [10.0]
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'CurlUpload', Request), \
                patch.object(agent, 'monotonic', lambda: clock[0]):
            path = str(Path(directory) / 'command.json')
            processor = agent.CommandProcessor({}, bridge, path)
            processor.tick()
            self.assertFalse(calls)  # Recovery/older firmware must not claim commands.
            bridge.ready = True
            clock[0] += 2
            processor.tick()
            poll = calls[-1]
            self.assertEqual(poll.options, dict(route='commands', method='GET', timeout=35))
            # An open long poll never blocks ticks or produces an AVR command.
            for _ in range(5):
                processor.tick()
            self.assertEqual(len(calls), 1)
            self.assertFalse(bridge.deliveries)
            poll.response = (200, json.dumps(dict(commands=[dict(id=command_id, type='device.ping', payload={})])).encode())
            processor.tick()
            self.assertTrue(poll.closed)
            self.assertEqual(bridge.deliveries, ['v1,%s,ping' % command_id])
            self.assertEqual(len(calls), 1)  # No success until the AVR responds.
            bridge.values['resac_command_result'] = 'v1,%s,pong,12345' % command_id
            clock[0] += 0.5
            processor.tick()
            result = calls[-1]
            self.assertRegex(result.payload['traceparent'], agent.TRACEPARENT)
            expected = dict(status='succeeded', result=dict(reply='pong', uptimeMs=12345), traceparent=result.payload['traceparent'])
            self.assertEqual(result.payload, expected)
            self.assertEqual(result.options['route'], 'commands/%s/result' % command_id)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            result.response = (500, b'{}')
            processor.tick()
            self.assertEqual(len(calls), 2)
            clock[0] += 1
            processor.tick()
            self.assertEqual(calls[-1].payload, expected)
            self.assertEqual(len(bridge.deliveries), 1)
            processor.close()
            # A restart resumes the saved result, without executing ping again.
            processor = agent.CommandProcessor({}, bridge, path)
            processor.tick()
            self.assertEqual(calls[-1].payload, expected)
            calls[-1].response = (200, b'{"ok":true}')
            processor.tick()
            self.assertEqual(calls[-1].options['method'], 'GET')
            # A redelivery after a lost acknowledgment reuses that exact result.
            calls[-1].response = poll.response
            processor.tick()
            self.assertEqual(calls[-1].payload, expected)
            self.assertEqual(len(bridge.deliveries), 1)
            processor.close()

    def test_unsupported_commands_and_unresponsive_arduino_fail(self):
        class Bridge:
            def __init__(self):
                self.sent = []
            def get(self, key):
                return 'v1,%s,pong,10' % str(uuid.uuid4())  # Stale, unrelated reply.
            def put(self, key, value):
                self.sent.append((key, value))
        with tempfile.TemporaryDirectory() as directory:
            bridge = Bridge()
            processor = agent.CommandProcessor({}, bridge, str(Path(directory) / 'command.json'))
            processor._accept(dict(id=str(uuid.uuid4()), type='unsupported', payload={}))
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertEqual(processor.reply['result']['error'], 'Unsupported command type')
            self.assertFalse(bridge.sent)
            processor.reply = None
            with patch.object(agent, 'monotonic', return_value=10):
                processor._accept(dict(id=str(uuid.uuid4()), type='device.ping', payload={}))
                processor._arduino()
                self.assertIsNone(processor.reply)
            with patch.object(agent, 'monotonic', return_value=21):
                processor._arduino()
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertEqual(processor.reply['result']['error'], 'Arduino ping timed out')

    def test_config_requires_https_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            certificate = Path(directory) / 'ca.pem'
            certificate.write_text('test fixture')
            config = dict(server_url='https://example.invalid', device_id='test-board',
                          device_token=secrets.token_urlsafe(32), ca_file=str(certificate))
            path = Path(directory) / 'agent.json'
            path.write_text(json.dumps(config)); path.chmod(0o600)
            self.assertEqual(agent.load_config(str(path))['device_id'], 'test-board')
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                agent.load_config(str(path))
            path.chmod(0o600)
            config['server_url'] = 'http://example.invalid'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                agent.load_config(str(path))


if __name__ == '__main__':
    unittest.main()
