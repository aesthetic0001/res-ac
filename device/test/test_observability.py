import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock, mock_open
import socket
import uuid

spec = importlib.util.spec_from_file_location('observed_agent', Path(__file__).parents[1] / 'res_ac_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

class TelemetryTests(unittest.TestCase):
    def test_transport_diagnostics_classify_without_exporting_raw_errors(self):
        cases = [(7, b'curl: (7) Failed to connect to private.example: Connection refused SECRET', 'connection_refused', 'connect'),
                 (7, b'Network is unreachable SECRET', 'network_unreachable', 'connect'),
                 (7, b'No route to host SECRET', 'host_unreachable', 'connect'),
                 (7, b'Could not connect to server SECRET', 'connect_failed', 'connect'),
                 (6, b'Could not resolve host SECRET', 'dns_resolution', 'dns'),
                 (60, b'SSL certificate problem SECRET', 'tls_certificate', 'tls'),
                 (35, b'SSL connect error SECRET', 'tls_handshake', 'tls'),
                 (56, b'Recv failure: Connection reset by peer SECRET', 'connection_reset', 'unknown'),
                 (28, b'Timeout SECRET', 'timeout', 'connect_or_dns')]
        for code, message, reason, stage in cases:
            status, attrs = agent.curl_diagnostics(b'000 0.001 0 0 0 1.36', message, code)
            self.assertEqual(status, 0)
            self.assertEqual(attrs['error.reason'], reason)
            self.assertEqual(attrs['error.stage'], stage)
            self.assertEqual(attrs['http.timing.total_ms'], 1360)
            self.assertNotIn('SECRET', str(attrs))
            self.assertNotIn('private.example', str(attrs))
        self.assertEqual(agent.curl_diagnostics(b'000 0.01 0.1 0 0 10', b'', 28)[1]['error.stage'], 'tls')
        self.assertEqual(agent.curl_diagnostics(b'000 0.01 0.1 0.2 0 10', b'', 28)[1]['error.stage'], 'response')
        self.assertEqual(agent.curl_diagnostics(b'503 0 0 0 0 1', b'SECRET', 0)[1]['error.reason'], 'http_error')
        # Accept old HTTP-code-only output; never export malformed numeric fields.
        self.assertEqual(agent.curl_diagnostics(b'201', b'', 0), (201, {}))
        self.assertEqual(agent.curl_diagnostics(b'private', b'', 0)[1]['error.reason'], 'invalid_response')
        attrs = agent.curl_diagnostics(b'200 NaN inf -1 999999 0.01', b'', 0)[1]
        self.assertEqual(attrs, {'http.timing.total_ms': 10})

    def test_network_snapshot_has_route_interface_and_wifi_without_addresses(self):
        files = {
            '/proc/net/route': 'Iface Destination Gateway Flags\nwlan0 00000000 DEADBEEF 0003\n',
            '/proc/net/wireless': 'header\nheader\n wlan0: 0000 70. -42. -90. 0\n',
            '/sys/class/net/wlan0/operstate': 'up\n'
        }
        def opened(path):
            if path not in files: raise IOError('missing')
            return mock_open(read_data=files[path])()
        with patch('builtins.open', side_effect=opened):
            self.assertEqual(agent.network_snapshot(), {'ipv4_default_route': 'present', 'interface_state': 'up',
                                                       'wifi_quality': 70, 'wifi_signal_dbm': -42})
            files['/proc/net/route'] = 'Iface Destination Gateway Flags\n'
            files['/sys/class/net/wlan0/operstate'] = 'down\n'
            self.assertEqual(agent.network_snapshot()['ipv4_default_route'], 'absent')
            self.assertEqual(agent.network_snapshot()['interface_state'], 'down')
            files['/proc/net/route'] = 'malformed'
            self.assertEqual(agent.network_snapshot()['ipv4_default_route'], 'unknown')
        with patch('builtins.open', side_effect=IOError('not readable')):
            self.assertEqual(agent.network_snapshot(), {'ipv4_default_route': 'unknown', 'interface_state': 'unknown'})

    def test_real_refused_connection_captures_diagnostics_and_preserves_retry_error(self):
        telemetry = agent.Telemetry({})
        # Bind without listening so curl gets a real ECONNREFUSED, while holding
        # the port to prevent a race with another listener.
        with socket.socket() as refused:
            refused.bind(('127.0.0.1', 0))
            config = dict(server_url='https://127.0.0.1:%d' % refused.getsockname()[1], device_id='test',
                          device_token='SECRET_TOKEN', ca_file='/etc/ssl/certs/ca-certificates.crt', _telemetry=telemetry)
            snapshots = [{'ipv4_default_route': 'present', 'interface_state': 'up'},
                         {'ipv4_default_route': 'absent', 'interface_state': 'down'}]
            with patch.object(agent, 'network_snapshot', side_effect=snapshots):
                upload = agent.CurlUpload(config, route='fan-state')
                try:
                    upload.process.wait(timeout=15)
                    with self.assertRaises(agent.UploadError) as failure: upload.finish()
                    self.assertEqual(failure.exception.curl_code, 7)
                finally: upload.close()
        event = telemetry.events[0]
        attrs = event['attributes']
        self.assertEqual(attrs['outcome'], 'connect')
        self.assertEqual(attrs['error.stage'], 'connect')
        self.assertIn(attrs['error.reason'], ['connection_refused', 'connect_failed'])
        self.assertEqual(attrs['network.start.ipv4_default_route'], 'present')
        self.assertEqual(attrs['network.ipv4_default_route'], 'absent')
        self.assertEqual(attrs['network.interface_state'], 'down')
        self.assertIn('http.timing.total_ms', attrs)
        self.assertNotIn('SECRET_TOKEN', str(event))
        self.assertNotIn('127.0.0.1', str(event))

    def test_telemetry_attempts_are_observed_without_starting_additional_uploads(self):
        telemetry = agent.Telemetry({})
        config = dict(server_url='https://example.invalid', device_id='test', device_token='SECRET',
                      ca_file='/tmp/test-ca.pem', _telemetry=telemetry)
        process = MagicMock(); process.poll.return_value = 0
        with patch.object(agent.subprocess, 'Popen', return_value=process):
            for code, output in [(7, b'000 0 0 0 0 1'), (0, b'200 0 0 0 0 1')]:
                process.stdin = MagicMock()
                process.returncode = code
                process.communicate.return_value = (output, b'Connection refused SECRET')
                upload = agent.CurlUpload(config, route='telemetry')
                try:
                    if code:
                        with self.assertRaises(agent.UploadError): upload.finish()
                    else: self.assertEqual(upload.finish()[0], 200)
                finally: upload.close()
        self.assertEqual(len(telemetry.events), 2)
        self.assertEqual(telemetry.events[0]['attributes']['request.kind'], 'telemetry')
        self.assertEqual(telemetry.events[0]['attributes']['error.reason'], 'connection_refused')
        self.assertEqual(telemetry.events[1]['attributes']['outcome'], 'ok')
        self.assertEqual(telemetry.events[1]['attributes']['request.kind'], 'telemetry')
        self.assertIsNone(telemetry.request)
        self.assertEqual(process.communicate.call_count, 2)

    def test_observing_telemetry_completion_respects_scheduled_heartbeat(self):
        config = dict(server_url='https://example.invalid', device_id='test', device_token='SECRET', ca_file='/tmp/test-ca.pem')
        telemetry = agent.Telemetry(config)
        config['_telemetry'] = telemetry
        process = MagicMock(); process.poll.return_value = 0; process.returncode = 0
        process.communicate.return_value = (b'200 0 0 0 0 1', b'')
        with patch.object(agent.subprocess, 'Popen', return_value=process) as spawn:
            telemetry.tick(agent.Buffer(1))
            telemetry.tick(agent.Buffer(1))
            for unused in range(5): telemetry.tick(agent.Buffer(1))
        self.assertEqual(spawn.call_count, 1)
        self.assertIsNone(telemetry.request)
        self.assertEqual(len(telemetry.events), 1)
        self.assertEqual(telemetry.events[0]['attributes']['request.kind'], 'telemetry')

    def test_bounded_queue_drops_and_sensor_attempt_identity(self):
        epoch = 'a' * 32
        class Bridge:
            value = 'v1,%s,1,2,7,ok,123,0' % epoch
            def get(self, key): return self.value
        bridge = Bridge()
        telemetry = agent.Telemetry({}, bridge)
        parent = agent.traceparent()
        telemetry.sensor()  # Success must wait for the matching sample/context.
        self.assertEqual(telemetry.events, [])
        telemetry.sensor(parent, epoch + '-1')
        telemetry.sensor(parent, epoch + '-1')
        self.assertEqual(len(telemetry.events), 1)
        self.assertEqual(telemetry.events[0]['traceparent'], parent)
        self.assertEqual(telemetry.events[0]['durationMs'], 7)
        bridge.value = 'v1,%s,1,3,1000,failed,200,1' % epoch
        telemetry.sensor()
        self.assertEqual(telemetry.events[-1]['attributes']['outcome'], 'failed')
        self.assertNotIn('reading.id', telemetry.events[-1]['attributes'])
        for _ in range(200): telemetry.event('bridge.error', attributes={'outcome': 'failed'})
        self.assertEqual(len(telemetry.events), 128)
        self.assertEqual(telemetry.dropped, 75)

    def test_failed_batch_keeps_identity_and_retries_without_blocking(self):
        clock = [10]
        requests = []
        class Process:
            def poll(self): return 0
        class Request:
            def __init__(self, config, payload, **kwargs):
                self.payload = payload
                self.process = Process()
                self.status = 500
                requests.append(self)
            def finish(self): return self.status, b'{}'
            def close(self): pass
        with patch.object(agent, 'monotonic', lambda: clock[0]), patch.object(agent, 'CurlUpload', Request):
            telemetry = agent.Telemetry({})
            telemetry.event('http.device', attributes={'outcome': 'connect'})
            telemetry.tick(agent.Buffer(1))
            first = requests[0].payload
            telemetry.tick(agent.Buffer(1))
            self.assertIsNotNone(telemetry.batch)
            clock[0] += 6
            telemetry.tick(agent.Buffer(1))
            second = requests[1].payload
            self.assertEqual(first['batchId'], second['batchId'])
            self.assertGreater(second['events'][0]['ageMs'], first['events'][0]['ageMs'])
            requests[1].status = 200
            telemetry.tick(agent.Buffer(1))
            self.assertIsNone(telemetry.batch)

    def test_curl_failures_and_latency_are_observed_once(self):
        telemetry = agent.Telemetry({})
        upload = agent.CurlUpload.__new__(agent.CurlUpload)
        upload.telemetry = telemetry
        upload.reported = False
        upload.started = agent.monotonic() - .25
        upload.parent = agent.traceparent()
        upload.kind = 'readings'
        upload.root_request = False
        upload.request_traceparent = agent.traceparent(upload.parent)
        upload._report(0, 60)
        upload._report(0, 60)
        self.assertEqual(len(telemetry.events), 1)
        self.assertEqual(telemetry.events[0]['attributes']['outcome'], 'tls')
        self.assertGreaterEqual(telemetry.events[0]['durationMs'], 250)

    def test_command_and_avr_spans_use_durable_command_context(self):
        command_id, parent = str(uuid.uuid4()), agent.traceparent()
        class Bridge:
            def get(self, key): return 'v1,%s,12,ok' % command_id
            def put(self, key, value): pass
        config = {}
        telemetry = agent.Telemetry(config, Bridge())
        config['_telemetry'] = telemetry
        with tempfile.TemporaryDirectory() as directory:
            processor = agent.CommandProcessor(config, Bridge(), directory + '/result.json')
            processor._accept(dict(id=command_id, type='device.ping', payload={}, traceparent=parent))
            processor._complete('succeeded', dict(reply='pong'))
            processor._save()
            resumed = agent.CommandProcessor(config, Bridge(), directory + '/result.json')
            self.assertEqual(resumed.reply['traceparent'], parent)
            self.assertEqual([e['name'] for e in telemetry.events], ['command.execute', 'avr.execute'])
            self.assertTrue(all(e['traceparent'] == parent for e in telemetry.events))
            for event in telemetry.events:
                self.assertEqual(event['attributes']['command.type'], 'device.ping')
                self.assertEqual(event['attributes']['command.source'], 'manual')
                self.assertEqual(agent.decode_json(event['attributes']['command.payload']), {})

    def test_command_payload_contents_are_bounded_to_supported_fields(self):
        session = 'a' * 32
        for command_type, payload in [
                ('device.ping', {}), ('fan.command', dict(button='power', sessionId=session)),
                ('fan.level', dict(level=0, sessionId=session)),
                ('fan.calibrate', dict(confirmOff=True, sessionId=session)),
                ('fan.curve', dict(enabled=True, thresholdsC=[-1, 20, 24, 24, 30], hysteresisC=.5, sessionId=session)),
                ('ir.send', dict(protocol='NEC', address=56960, command=22, repeats=3, sessionId=session))]:
            attributes = agent.command_payload_attributes(command_type, dict(payload, token='private'))
            self.assertEqual(agent.decode_json(attributes['command.payload']), payload)
            self.assertNotIn('private', attributes['command.payload'])
        self.assertEqual(agent.command_payload_attributes('unknown', dict(token='private')), {})
        for payload in [dict(level=True), dict(level=6), dict(level=float('nan')), dict(sessionId='private')]:
            self.assertEqual(agent.decode_json(agent.command_payload_attributes('fan.level', payload)['command.payload']), {})

    def test_command_metadata_on_local_dispatch_and_bridge_failure(self):
        class Bridge:
            fail = False
            def get(self, key):
                if self.fail:
                    raise IOError('Bridge unavailable')
                return ''
            def put(self, key, value): pass
        bridge = Bridge()
        telemetry = agent.Telemetry({}, bridge)
        with tempfile.TemporaryDirectory() as directory:
            processor = agent.CommandProcessor({'_telemetry': telemetry}, bridge, directory + '/result.json')
            processor._accept(dict(id=str(uuid.uuid4()), type='device.ping', payload={}))
            processor._arduino()
            bridge.fail = True
            processor.bridge_due = 0
            processor._arduino()
            self.assertEqual([event['name'] for event in telemetry.events], ['command.local', 'bridge.dispatch', 'bridge.error'])
            for event in telemetry.events:
                self.assertEqual(event['attributes']['command.type'], 'device.ping')
                self.assertEqual(event['attributes']['command.source'], 'manual')

    def test_malformed_command_type_does_not_leak_into_diagnostics(self):
        class Bridge:
            def get(self, key): return ''
            def put(self, key, value): pass
        telemetry = agent.Telemetry({})
        with tempfile.TemporaryDirectory() as directory:
            processor = agent.CommandProcessor({'_telemetry': telemetry}, Bridge(), directory + '/result.json')
            processor._accept(dict(id=str(uuid.uuid4()), type='private value', payload={}))
            self.assertEqual(processor.reply['status'], 'failed')
            self.assertTrue(all(e['attributes']['command.type'] == 'unknown' for e in telemetry.events))
            self.assertNotIn('private value', str(telemetry.events))

    def test_reading_drop_has_original_trace_and_buffer_has_bound(self):
        telemetry = agent.Telemetry({})
        pending = agent.Buffer(1, telemetry)
        parent = agent.traceparent()
        pending.add(dict(readingId='one', traceparent=parent))
        pending.add(dict(readingId='two', traceparent=agent.traceparent()))
        self.assertEqual(pending.dropped, 1)
        self.assertEqual(telemetry.events[0]['traceparent'], parent)
        self.assertEqual(telemetry.events[0]['name'], 'reading.drop')

    def test_http_attempt_ids_are_propagated_and_preserve_parent_across_retries(self):
        parent = agent.traceparent()
        telemetry = agent.Telemetry({})
        config = dict(server_url='https://example.invalid', device_id='test', device_token='x' * 32,
                      ca_file='/tmp/test-ca.pem', _telemetry=telemetry)
        process = MagicMock()
        process.poll.return_value = 0
        contexts = []
        with patch.object(agent.subprocess, 'Popen', return_value=process):
            for unused in range(2):
                process.stdin = MagicMock()
                upload = agent.CurlUpload(config, dict(readingId='stable', traceparent=parent))
                contexts.append(upload.request_traceparent)
                with open(upload.files[0]) as stream:
                    self.assertIn('traceparent: ' + upload.request_traceparent, stream.read())
                self.assertEqual(upload.parent, parent)
                self.assertFalse(upload.root_request)
                upload._report(201, 0)
                upload.close()
        self.assertNotEqual(contexts[0], contexts[1])
        self.assertTrue(all(value[3:35] == parent[3:35] for value in contexts))
        for event, context in zip(telemetry.events, contexts):
            self.assertEqual(event['name'], 'http.device')
            self.assertEqual(event['traceparent'], parent)
            self.assertEqual(event['spanId'], context[36:52])
            self.assertNotEqual(event['spanId'], parent[36:52])

    def test_standalone_http_request_is_a_root_with_its_own_propagated_id(self):
        telemetry = agent.Telemetry({})
        config = dict(server_url='https://example.invalid', device_id='test', device_token='x' * 32,
                      ca_file='/tmp/test-ca.pem', _telemetry=telemetry)
        process = MagicMock(); process.poll.return_value = 0
        with patch.object(agent.subprocess, 'Popen', return_value=process):
            upload = agent.CurlUpload(config, route='commands', method='GET')
            upload._report(204, 0)
            upload.close()
        event = telemetry.events[0]
        self.assertEqual(event['name'], 'http.request')
        self.assertEqual(event['traceparent'], upload.request_traceparent)
        self.assertEqual(event['spanId'], upload.request_traceparent[36:52])
