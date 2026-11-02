import json
import socketserver
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_agent import agent


EPOCH = 'a' * 32
SESSION = 'b' * 32


def snapshot(**overrides):
    values = dict(resac_events='v1', resac_epoch=EPOCH, resac_command_ready='v4',
                  resac_fan_session=SESSION, resac_fan_state='v2,%s,1,1,1,1,automatic' % SESSION,
                  resac_sensor_status='ok', resac_sensor_diagnostic='', resac_command_result='')
    values.update(overrides)
    return dict(response='get', value=values)


def event(key, value):
    return dict(request='raw', data='v1,%s,%s' % (key, value))


def deliver(bridge, *messages):
    bridge._receive(b''.join(json.dumps(message).encode() for message in messages))


def ready_bridge(**values):
    bridge = agent.BridgeEvents()
    bridge.snapshot_pending = True
    deliver(bridge, snapshot(**values))
    return bridge


class Request:
    def __init__(self, config, payload, **kwargs):
        self.payload = payload
        self.process = self

    def poll(self):
        return 0

    def finish(self):
        return 200, b'{}'

    def close(self):
        pass


@contextmanager
def bridge_server():
    """Broadcast replies and Mailbox events like the installed Yun TCP server."""
    lock = threading.Lock()
    clients, requests = [], []
    values = snapshot()['value']

    def broadcast(message):
        encoded = json.dumps(message).encode()
        with lock:
            for client in clients[:]:
                try:
                    client.sendall(encoded)
                except OSError:
                    pass

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with lock:
                clients.append(self.request)
            data = ''
            try:
                while True:
                    block = self.request.recv(4096)
                    if not block:
                        return
                    data += block.decode()
                    while data:
                        try:
                            message, consumed = json.JSONDecoder().raw_decode(data)
                        except ValueError:
                            break
                        data = data[consumed:].lstrip()
                        requests.append(message)
                        if message['command'] == 'get':
                            broadcast(dict(response='get', value=dict(values)))
                        else:
                            values[message['key']] = message['value']
                            broadcast(dict(response='put', key=message['key'], value=message['value']))
            finally:
                with lock:
                    clients.remove(self.request)

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    bridge = agent.BridgeEvents(port=server.server_address[1])

    def pump(predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError('Bridge condition timed out')
            bridge.poll(.01)

    try:
        pump(lambda: bridge.ready)
        yield bridge, requests, broadcast, pump
    finally:
        bridge.close()
        server.shutdown()
        server.server_close()
        worker.join()


class BridgeEventTests(unittest.TestCase):
    def test_fragmented_concatenated_objects_and_broadcast_replies(self):
        bridge = agent.BridgeEvents()
        bridge.snapshot_pending = True
        messages = [snapshot(), event('resac_sensor_status', 'read_error'),
                    dict(response='get', key='resac_sensor_status', value='stale'),
                    dict(response='get', value=snapshot()['value']),
                    dict(response='put', key='resac_sensor_status', value='stale')]
        stream = b''.join(json.dumps(message).encode() for message in messages)
        for start in range(0, len(stream), 3):
            bridge._receive(stream[start:start + 3])
        self.assertTrue(bridge.ready)
        self.assertEqual(bridge.get('resac_sensor_status'), 'read_error')
        # Braces, escapes, nested objects, and multibyte UTF-8 do not break framing.
        stream = json.dumps(dict(request='raw', data='ignored "}\\é', nested=[{}]), ensure_ascii=False).encode()
        for char in stream:
            bridge._receive(bytes([char]))
        deliver(bridge, event('resac_sensor_status', 'ok'))
        self.assertEqual(bridge.get('resac_sensor_status'), 'ok')

    def test_stream_parser_works_with_both_legacy_json_apis(self):
        for read in (json.loads, json.JSONDecoder().raw_decode):
            legacy = SimpleNamespace(write=json.dumps, read=read)
            with patch.object(agent, 'json', legacy):
                bridge = ready_bridge()
                deliver(bridge, event('resac_sensor_status', 'read_error'))
                self.assertEqual(bridge.get('resac_sensor_status'), 'read_error')

    def test_unknown_events_and_unbounded_messages_are_rejected(self):
        bridge = ready_bridge()
        before = dict(bridge.values)
        deliver(bridge, dict(request='raw', data='v2,resac_sample,unknown'),
                event('unrelated', 'value'), dict(request='raw', data={}))
        self.assertEqual(bridge.values, before)
        with self.assertRaises(ValueError):
            bridge._receive(b'{"value":"' + b'x' * 16384)

    def test_retained_snapshot_and_burst_samples_keep_matching_diagnostics(self):
        clock = [100.0]
        with patch.object(agent, 'monotonic', lambda: clock[0]):
            bridge = ready_bridge(resac_sample='v1,%s,1,22000,48000,1' % EPOCH)
            config = dict(_bridge=bridge)
            telemetry = agent.Telemetry(config)
            config['_telemetry'] = telemetry
            pending = agent.Buffer(10, telemetry)
            collector = agent.Collector(config, pending)
            collector.tick()
            self.assertEqual(len(pending.queue), 1)
            # Both samples arrive before the consumer runs, with no timer advance.
            for sequence, duration in [(2, 7), (3, 11)]:
                deliver(bridge, event('resac_sensor_diagnostic', 'v1,%s,%d,%d,%d,ok,100,0' %
                                     (EPOCH, sequence, sequence, duration)),
                        event('resac_sample', 'v1,%s,%d,23000,49000,100' % (EPOCH, sequence)))
            collector.tick()
            self.assertEqual([sample['readingId'] for sample in pending.queue],
                             [EPOCH + '-1', EPOCH + '-2', EPOCH + '-3'])
            sensor = [item for item in telemetry.events if item['name'] == 'sensor.read']
            self.assertEqual([item['durationMs'] for item in sensor], [7, 11])
            self.assertEqual([item['attributes']['reading.id'] for item in sensor], [EPOCH + '-2', EPOCH + '-3'])
            self.assertNotIn(b'"get"', bridge.outgoing)

    def test_command_ack_is_processed_without_waiting_for_half_second_timer(self):
        bridge = ready_bridge()
        config = dict(_bridge=bridge)
        telemetry = agent.Telemetry(config)
        config['_telemetry'] = telemetry
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'monotonic', return_value=100):
            processor = agent.CommandProcessor(config, state_path=str(Path(directory) / 'result.json'))
            command_id = str(uuid.uuid4())
            processor._accept(dict(id=command_id, type='device.ping', payload={}))
            processor._arduino()
            processor._arduino()
            self.assertEqual(bridge.outgoing.count(b',ping'), 1)
            processor.bridge_due = 200  # An event must bypass the old polling deadline.
            deliver(bridge, event('resac_command_diagnostic', 'v1,%s,3,ok' % command_id),
                    event('resac_command_result', 'v1,%s,pong,123' % command_id))
            processor._arduino()
            self.assertEqual(processor.reply['result']['reply'], 'pong')
            self.assertEqual(telemetry.events[-1]['name'], 'avr.execute')
            self.assertEqual(telemetry.events[-1]['durationMs'], 3)

    def test_fan_change_uploads_immediately_without_extra_heartbeats(self):
        bridge = ready_bridge()
        with patch.object(agent, 'CurlUpload', side_effect=Request) as requests, \
                patch.object(agent, 'monotonic', return_value=100):
            reporter = agent.FanReporter(dict(_bridge=bridge))
            reporter.tick()
            self.assertEqual(requests.call_count, 1)
            deliver(bridge, event('resac_fan_state', 'v2,%s,1,1,2,2,manual' % SESSION))
            reporter.tick()
            self.assertEqual(requests.call_count, 2)
            self.assertEqual(requests.call_args.args[1]['speed'], 2)
            self.assertEqual(bridge.outgoing.count(b'resac_fan_heartbeat'), 1)
            self.assertNotIn(b'"get"', bridge.outgoing)

    def test_automation_uses_live_events_and_does_not_treat_reconnect_snapshot_as_fresh(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'monotonic', return_value=100):
            bridge = ready_bridge(resac_sample='v1,%s,1,25000,40000,1' % EPOCH)
            auto = agent.AutoController(dict(_bridge=bridge), path=str(Path(directory) / 'curve.json'),
                                        state_path=str(Path(directory) / 'command.json'))
            auto.configure(dict(enabled=True, thresholdsC=[22, 24, 26, 28, 30]))
            commands = SimpleNamespace(current=None, reply=None)
            auto.tick(commands)
            self.assertFalse(auto.busy)
            deliver(bridge, event('resac_sample', 'v1,%s,2,25000,40000,2' % EPOCH))
            auto.tick(commands)
            self.assertTrue(auto.busy)
            self.assertEqual(auto.target, 2)
            auto.runner = None
            bridge._disconnect()
            bridge.snapshot_pending = True
            deliver(bridge, snapshot(resac_sample='v1,%s,3,30000,40000,3' % EPOCH))
            auto.tick(commands)
            self.assertIsNone(auto.sample_at)
            self.assertFalse(auto.busy)

    def test_reboot_invalidates_state_and_discards_unsent_actions(self):
        bridge = ready_bridge()
        bridge.put('resac_command', 'uncertain-action')
        deliver(bridge, event('resac_events', ''))
        self.assertFalse(bridge.ready)
        self.assertFalse(bridge.outgoing)
        with self.assertRaises(IOError):
            bridge.get('resac_fan_state')
        deliver(bridge, event('resac_events', 'v1'))
        self.assertEqual(json.loads(bridge.outgoing), dict(command='get'))
        deliver(bridge, snapshot(resac_epoch='', resac_fan_state='v2,none,0,0,0,1,reset'))
        collector = agent.Collector(dict(_bridge=bridge), agent.Buffer(2))
        collector.tick()
        self.assertRegex(bridge.get('resac_epoch'), '^[a-f0-9]{32}$')
        self.assertNotEqual(bridge.get('resac_epoch'), EPOCH)
        self.assertNotIn(b'uncertain-action', bridge.outgoing)

    def test_completed_samples_survive_disconnect_and_remain_uploadable(self):
        bridge = ready_bridge()
        config = dict(_bridge=bridge)
        telemetry = agent.Telemetry(config)
        config['_telemetry'] = telemetry
        pending = agent.Buffer(10, telemetry)
        collector = agent.Collector(config, pending)
        collector.tick()
        deliver(bridge, event('resac_sensor_diagnostic', 'v1,%s,1,1,7,ok,100,0' % EPOCH),
                event('resac_sample', 'v1,%s,1,22000,48000,100' % EPOCH))
        bridge._disconnect()
        collector.tick()
        self.assertEqual(pending.first()['readingId'], EPOCH + '-1')
        self.assertFalse(bridge.ready)
        self.assertTrue(any(item['name'] == 'sensor.read' for item in telemetry.events))

    def test_sample_burst_is_bounded_and_overflow_is_counted(self):
        bridge = ready_bridge()
        for sequence in range(1, 131):
            deliver(bridge, event('resac_sample', 'v1,%s,%d,22000,48000,100' % (EPOCH, sequence)))
        self.assertEqual(len(bridge.samples), 128)
        pending = agent.Buffer(1000)
        agent.Collector(dict(_bridge=bridge), pending).tick()
        self.assertEqual(pending.dropped, 2)
        self.assertEqual(len(pending.queue), 128)
        self.assertEqual(pending.first()['readingId'], EPOCH + '-3')
        # Identical notifications are drained and deduplicated even without a
        # changed cache revision, rather than accumulating until the next sample.
        deliver(bridge, event('resac_sample', 'v1,%s,130,22000,48000,100' % EPOCH))
        agent.Collector(dict(_bridge=bridge), pending).tick()
        self.assertEqual(len(pending.queue), 128)
        self.assertFalse(bridge.samples)

    def test_write_backpressure_invalidates_cache_and_discards_queued_commands(self):
        bridge = ready_bridge()
        bridge.put('resac_command', 'must-not-replay')
        with self.assertRaises(IOError):
            bridge.put('resac_upload_status', 'x' * 8192)
        self.assertFalse(bridge.ready)
        self.assertFalse(bridge.outgoing)
        with self.assertRaises(IOError):
            bridge.get('resac_command_ready')

    def test_older_firmware_requires_an_explicit_upgrade(self):
        bridge = agent.BridgeEvents()
        bridge.snapshot_pending = True
        with self.assertRaises(ValueError):
            deliver(bridge, dict(response='get', value=dict(resac_command_ready='v4')))
        self.assertFalse(bridge.ready)

    def test_one_connection_one_snapshot_and_immediate_socket_wakeup(self):
        with bridge_server() as (bridge, requests, broadcast, pump):
            config = dict(_bridge=bridge)
            pending = agent.Buffer(10)
            collector = agent.Collector(config, pending)
            clock = [100.0]
            with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'CurlUpload', side_effect=Request), \
                    patch.object(agent, 'monotonic', lambda: clock[0]):
                auto = agent.AutoController(config, path=str(Path(directory) / 'curve.json'))
                reporter = agent.FanReporter(config, automation=auto)
                commands = SimpleNamespace(current=None, reply=None)
                for _ in range(20):
                    clock[0] += 1
                    collector.tick()
                    auto.tick(commands)
                    reporter.tick()
                    bridge.poll(.005)
                broadcast(event('resac_sample', 'v1,%s,1,25000,40000,1' % EPOCH))
                pump(lambda: bool(bridge.samples))
                collector.tick()
                self.assertEqual(pending.first()['readingId'], EPOCH + '-1')
                self.assertEqual([request for request in requests if request['command'] == 'get'],
                                 [dict(command='get')])
                connection = bridge.connection
                # Another client's stale reply must not replace the pushed sample.
                broadcast(dict(response='get', key='resac_sample', value='stale'))
                broadcast(event('resac_sensor_status', 'read_error'))
                pump(lambda: bridge.get('resac_sensor_status') == 'read_error')
                self.assertIs(bridge.connection, connection)
                self.assertEqual(bridge.get('resac_sample'), 'v1,%s,1,25000,40000,1' % EPOCH)

    def test_disconnect_reconnect_takes_snapshot_without_replaying_command(self):
        with bridge_server() as (bridge, requests, broadcast, pump):
            bridge.put('resac_command', 'must-not-replay')
            bridge._disconnect()
            self.assertFalse(bridge.outgoing)
            self.assertFalse(bridge.ready)
            bridge.retry_at = 0
            pump(lambda: bridge.ready)
            self.assertEqual(sum(request['command'] == 'get' for request in requests), 2)
            self.assertFalse(any(request.get('value') == 'must-not-replay' for request in requests))
            self.assertFalse(bridge.sample_live)


if __name__ == '__main__':
    unittest.main()
