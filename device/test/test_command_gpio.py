import errno
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_agent import agent
from test_bridge_events import bridge_server, deliver, event, snapshot


class FakeGPIO:
    def __init__(self):
        self.levels = []
        self.opened = False

    def open(self):
        self.opened = True

    def set(self, pending):
        self.levels.append(pending)

    def close(self):
        if self.opened:
            self.set(False)
        self.opened = False


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.gpio = FakeGPIO()
        self.bridge = agent.BridgeEvents(command_gpio=self.gpio)
        self.bridge.snapshot_pending = True
        deliver(self.bridge, snapshot(resac_command_notify=agent.COMMAND_GPIO_PROTOCOL))
        self.command_id = str(uuid.uuid4())
        self.frame = 'v1,%s,ping' % self.command_id
        self.clock = patch.object(agent, 'monotonic', return_value=100)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.bridge.close)

    def confirm(self, frame=None):
        deliver(self.bridge, dict(response='put', key='resac_command', value=frame or self.frame))

    def test_only_exact_put_confirmation_asserts_the_held_signal_once(self):
        self.bridge.dispatch_command(self.frame, 110)
        self.assertEqual(self.gpio.levels, [False])
        self.confirm('v1,%s,ping' % uuid.uuid4())
        deliver(self.bridge, dict(response='get', key='resac_command', value=self.frame))
        self.assertEqual(self.gpio.levels, [False])
        self.confirm(); self.confirm()
        self.assertEqual(self.gpio.levels, [False, True])
        self.bridge.cancel_command(str(uuid.uuid4()))
        self.assertTrue(self.bridge.notification['signaled'])
        self.bridge.cancel_command(self.command_id)
        self.assertEqual(self.gpio.levels, [False, True, False])
        self.confirm()
        self.assertEqual(self.gpio.levels, [False, True, False])

    def test_late_confirmation_cannot_wake_an_expired_or_canceled_command(self):
        self.bridge.dispatch_command(self.frame, 110)
        with patch.object(agent, 'monotonic', return_value=110):
            self.confirm()
        self.assertIsNone(self.bridge.notification)
        self.assertNotIn(True, self.gpio.levels)
        self.bridge.dispatch_command(self.frame, 110)
        self.bridge.cancel_command()
        self.confirm()
        self.assertNotIn(True, self.gpio.levels)

    def test_matching_avr_ack_clears_the_signal_before_result_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            processor = agent.CommandProcessor(dict(_bridge=self.bridge),
                                                state_path=str(Path(directory) / 'result.json'))
            processor._accept(dict(id=self.command_id, type='device.ping', payload={}))
            processor._arduino()
            self.confirm()
            deliver(self.bridge, event('resac_command_result', 'v1,%s,pong,123' % self.command_id))
            processor._arduino()
            self.assertEqual(processor.reply['status'], 'succeeded')
            self.assertIsNone(self.bridge.notification)
            self.assertFalse(self.gpio.levels[-1])

    def test_deadline_releases_an_already_asserted_signal(self):
        self.bridge.dispatch_command(self.frame, 110)
        self.confirm()
        with patch.object(agent, 'monotonic', return_value=110):
            self.bridge._expire_notification()
        self.assertIsNone(self.bridge.notification)
        self.assertEqual(self.gpio.levels, [False, True, False])

    def test_second_command_cannot_replace_an_unacknowledged_notification(self):
        self.bridge.dispatch_command(self.frame, 110)
        queued = self.bridge.outgoing
        with self.assertRaises(IOError):
            self.bridge.dispatch_command('v1,%s,ping' % uuid.uuid4(), 110)
        self.assertEqual(self.bridge.outgoing, queued)
        self.assertEqual(self.bridge.notification['frame'], self.frame)

    def test_disconnect_and_avr_reset_drop_notification_without_replay(self):
        for reset in (False, True):
            self.bridge.snapshot_pending = True
            deliver(self.bridge, snapshot(resac_command_notify=agent.COMMAND_GPIO_PROTOCOL))
            self.bridge.dispatch_command(self.frame, 110)
            self.confirm()
            if reset:
                deliver(self.bridge, event('resac_events', ''))
            else:
                self.bridge._disconnect()
            self.assertFalse(self.gpio.levels[-1])
            self.assertIsNone(self.bridge.notification)
            self.assertEqual(self.bridge.outgoing, b'')
            self.confirm()
            self.assertFalse(self.gpio.levels[-1])

    def test_unknown_profile_and_busy_pin_do_not_queue_a_physical_action(self):
        self.bridge.values['resac_command_notify'] = 'unknown'
        with self.assertRaises(IOError):
            self.bridge.dispatch_command(self.frame, 110)
        self.assertEqual(self.bridge.outgoing, b'')
        self.bridge.values['resac_command_notify'] = agent.COMMAND_GPIO_PROTOCOL
        with patch.object(self.gpio, 'open', side_effect=IOError(errno.EBUSY, 'reserved')):
            with self.assertRaises(IOError):
                self.bridge.dispatch_command(self.frame, 110)
        self.assertIsNone(self.bridge.notification)
        self.assertEqual(self.bridge.outgoing, b'')

    def test_older_firmware_does_not_touch_gpio(self):
        self.bridge.values['resac_command_notify'] = ''
        self.bridge.dispatch_command(self.frame, 110)
        self.assertFalse(self.gpio.opened)
        self.assertIn(self.frame.encode(), self.bridge.outgoing)

    def test_real_socket_confirmation_wakes_without_a_completion_timer(self):
        with bridge_server() as (bridge, requests, broadcast, pump):
            bridge.command_gpio = self.gpio
            broadcast(event('resac_command_notify', agent.COMMAND_GPIO_PROTOCOL))
            pump(lambda: bridge.values.get('resac_command_notify') == agent.COMMAND_GPIO_PROTOCOL)
            bridge.dispatch_command(self.frame, 110)
            self.assertNotIn(True, self.gpio.levels)
            pump(lambda: bridge.notification['signaled'])
            self.assertEqual(self.gpio.levels, [False, True])
            self.assertEqual([r['command'] for r in requests], ['get', 'put'])

    def test_signal_write_failure_disconnects_without_replaying_the_command(self):
        with bridge_server() as (bridge, requests, broadcast, pump):
            bridge.command_gpio = self.gpio
            broadcast(event('resac_command_notify', agent.COMMAND_GPIO_PROTOCOL))
            pump(lambda: bridge.values.get('resac_command_notify') == agent.COMMAND_GPIO_PROTOCOL)
            original = self.gpio.set

            def fail_assertion(pending):
                if pending:
                    raise IOError('GPIO write failed')
                original(pending)

            with patch.object(self.gpio, 'set', side_effect=fail_assertion):
                bridge.dispatch_command(self.frame, 110)
                pump(lambda: not bridge.ready)
            self.assertIsNone(bridge.notification)
            self.assertEqual(bridge.outgoing, b'')
            self.assertFalse(self.gpio.opened)
            bridge.retry_at = 0
            pump(lambda: bridge.ready)
            self.assertEqual([r['command'] for r in requests], ['get', 'put', 'get'])


class SysfsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / 'export').touch(); (self.root / 'unexport').touch()
        self.writes = []
        outer = self

        class EmulatedGPIO(agent.CommandGPIO):
            def _write(self, path, value):
                outer.writes.append((path, value))
                if path == 'export':
                    pin = outer.root / ('gpio' + value)
                    if pin.exists():
                        raise OSError(errno.EBUSY, 'reserved')
                    pin.mkdir()
                    (pin / 'value').write_text('0')
                    (pin / 'direction').write_text('in')
                elif path == 'unexport':
                    shutil.rmtree(outer.root / ('gpio' + value))
                else:
                    (outer.root / path).write_text('out')
                    (outer.root / path).with_name('value').write_text('1' if value == 'high' else '0')

        self.cls = EmulatedGPIO
        self.gpio = self.make()
        self.addCleanup(self.gpio.close)

    def make(self):
        return self.cls(str(self.root), str(self.root / 'ownership.lock'))

    def test_persistent_descriptor_and_exclusive_ownership(self):
        self.gpio.open()
        fd = self.gpio.value_fd
        self.assertEqual(self.writes[:4], [('export', '21'), ('gpio21/direction', 'high'),
                                           ('export', '19'), ('gpio19/direction', 'high')])
        self.gpio.set(True); self.gpio.open()
        self.assertEqual(self.gpio.value_fd, fd)
        self.assertEqual((self.root / 'gpio21/value').read_text(), '0')
        other = self.make()
        with self.assertRaises(IOError):
            other.open()
        self.gpio.close()
        self.assertFalse((self.root / 'gpio21').exists())
        self.assertFalse((self.root / 'gpio19').exists())

    def test_restart_recovers_only_recorded_pins_and_releases_stale_assertion(self):
        self.gpio.open(); self.gpio.set(True)
        os.close(self.gpio.value_fd); os.close(self.gpio.lock_fd)
        self.gpio.value_fd = self.gpio.lock_fd = None  # Simulated SIGKILL, no cleanup.
        replacement = self.make()
        self.addCleanup(replacement.close)
        replacement.open()
        self.assertEqual((self.root / 'gpio21/value').read_text(), '1')
        self.assertEqual(replacement.owned, [21, 19])

    def test_foreign_export_is_never_stolen(self):
        self.gpio._write('export', '21')
        with self.assertRaises(IOError):
            self.gpio.open()
        self.assertTrue((self.root / 'gpio21').exists())
        self.assertNotIn(('unexport', '21'), self.writes)

    def test_existing_enabled_translator_is_preserved_and_disabled_one_rejected(self):
        self.gpio._write('export', '19')
        self.gpio._write('gpio19/direction', 'high')
        self.gpio.open(); self.gpio.close()
        self.assertTrue((self.root / 'gpio19').exists())
        self.assertNotIn(('unexport', '19'), self.writes)
        self.gpio._write('gpio19/direction', 'low')
        with self.assertRaises(IOError):
            self.gpio.open()
        self.assertFalse((self.root / 'gpio21').exists())
        self.assertEqual((self.root / 'gpio19/value').read_text(), '0')

    def test_preflight_is_read_only_and_detects_kernel_reservations(self):
        debug = self.root / 'debug'
        debug.write_text('gpio-21 (spi_gpio) out lo\n')
        with self.assertRaises(IOError):
            self.gpio.preflight(str(debug))
        debug.write_text('')
        self.gpio.preflight(str(debug))
        self.assertEqual(self.writes, [])
        self.assertFalse((self.root / 'ownership.lock').exists())
