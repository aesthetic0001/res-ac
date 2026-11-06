import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_agent import agent
from test_bridge_events import ready_bridge, deliver, event, Request, SESSION


class CommandBatchingTests(unittest.TestCase):
    def bridge(self):
        bridge = ready_bridge(resac_command_protocol='batched-v1',
                              resac_command_notify=agent.COMMAND_GPIO_PROTOCOL)
        bridge.command_gpio = SimpleNamespace(open=lambda: None, set=lambda value: None, close=lambda: None)
        return bridge

    def test_atomic_context_rotates_on_invalidation_and_reconnect(self):
        bridge = self.bridge()
        bridge.fan_control(heartbeat=1)
        first = bridge.control_session
        self.assertEqual(bridge.get('resac_fan_control'), 'v1,%s,1' % first)
        deliver(bridge, event('resac_fan_session', first))
        self.assertTrue(bridge.control_confirmed)
        bridge.fan_control(heartbeat=2)
        self.assertEqual(bridge.control_session, first)
        bridge.invalidate_fan()
        self.assertNotEqual(bridge.control_session, first)
        self.assertFalse(bridge.control_confirmed)
        self.assertEqual(bridge.get('resac_fan_control'), 'v1,%s,2' % bridge.control_session)
        bridge._snapshot()
        self.assertIsNone(bridge.control_session)

    def test_fan_reporter_sends_one_atomic_heartbeat_and_rotates_invalidated_epoch(self):
        bridge = self.bridge()
        reporter = agent.FanReporter(dict(_bridge=bridge))
        with patch.object(agent, 'CurlUpload', Request):
            reporter.tick()
            first = bridge.control_session
            self.assertIsNotNone(first)
            self.assertNotIn(b'resac_fan_invalidate', bridge.outgoing)
            self.assertNotIn(b'resac_fan_heartbeat', bridge.outgoing)
            deliver(bridge, event('resac_fan_session', first))
            reporter.tick()
            self.assertEqual(first, bridge.control_session)
            deliver(bridge, event('resac_fan_session', ''))
            reporter.tick()
            self.assertNotEqual(first, bridge.control_session)

    def test_combined_completion_correlates_id_and_reports_phase_durations(self):
        clock = [100.0]
        bridge = self.bridge()
        config = dict(_bridge=bridge)
        telemetry = agent.Telemetry(config)
        config['_telemetry'] = telemetry
        ident = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'monotonic', lambda: clock[0]):
            processor = agent.CommandProcessor(config, state_path=str(Path(directory) / 'journal.json'))
            processor._accept(dict(id=ident, type='device.ping', payload={}))
            processor._arduino()
            frame = bridge.notification['frame']
            clock[0] += .02
            deliver(bridge, dict(response='put', key='resac_command', value=frame))
            self.assertTrue(bridge.notification['signaled'])
            for bad in ['v5,1,2,3,v1,%s,pong,1' % uuid.uuid4(),
                        'v5,4294967296,2,3,v1,%s,pong,1' % ident,
                        'v5,1,-1,3,v1,%s,pong,1' % ident,
                        'v1,%s,pong,1' % ident]:
                deliver(bridge, event('resac_command_result', bad))
                processor._arduino()
                self.assertIsNotNone(processor.current)
            clock[0] += .58
            deliver(bridge, event('resac_command_result', 'v5,100,120,10,v1,%s,pong,1234' % ident))
            processor._arduino()
            self.assertEqual(processor.reply['result']['reply'], 'pong')
            self.assertIsNone(bridge.notification)
            events = {item['name']: item for item in telemetry.events}
            self.assertEqual(events['command.execute']['attributes']['command.timing.fetch_ms'], 100)
            self.assertEqual(events['command.execute']['attributes']['command.timing.safety_ms'], 120)
            self.assertEqual(events['avr.execute']['durationMs'], 10)
            self.assertEqual(len(telemetry.events), 4)  # local root, dispatch, execution, AVR; phases add no events.
            self.assertAlmostEqual(events['command.execute']['attributes']['command.timing.notify_ms'], 20, delta=1)
            self.assertAlmostEqual(events['command.execute']['attributes']['command.timing.roundtrip_ms'], 580, delta=1)

    def test_timeout_invalidates_atomic_context_before_accepting_another_action(self):
        bridge = self.bridge()
        bridge.fan_control(heartbeat=1)
        first = bridge.control_session
        deliver(bridge, event('resac_fan_session', first),
                event('resac_fan_state', 'v2,%s,1,1,1,1,manual' % first))
        with tempfile.TemporaryDirectory() as directory:
            processor = agent.CommandProcessor(dict(_bridge=bridge), state_path=str(Path(directory) / 'journal.json'))
            processor._accept(dict(id=str(uuid.uuid4()), type='fan.level',
                payload=dict(level=2, sessionId=first), expiresAt='2099-01-01T00:00:00Z'))
            processor._arduino()
            self.assertTrue(processor.prepared)
            processor.deadline = 0
            processor._arduino()
            self.assertNotEqual(bridge.control_session, first)
            self.assertEqual(processor.reply['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
