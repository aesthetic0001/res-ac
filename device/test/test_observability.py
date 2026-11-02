import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import uuid

spec = importlib.util.spec_from_file_location('observed_agent', Path(__file__).parents[1] / 'res_ac_agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

class TelemetryTests(unittest.TestCase):
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
                *[('fan.curve', dict(enabled=True, thresholdsC=[-1, 20, 24, 24, 30], hysteresisC=hysteresis, sessionId=session))
                  for hysteresis in [0, .1, 10, 1.7976931348623157e308]],
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
