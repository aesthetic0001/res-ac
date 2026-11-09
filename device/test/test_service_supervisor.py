import importlib.util
import errno
import os
from pathlib import Path
import signal
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT)] + sys.path):
    spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'service_supervisor.py')
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
agent = sys.modules['res_ac_agent']


class HealthTests(unittest.TestCase):
    def test_heartbeat_is_private_atomic_pid_bound_and_uses_monotonic_time(self):
        with tempfile.TemporaryDirectory() as directory:
            health = agent.HealthHeartbeat('agent', directory)
            with patch.object(agent, 'monotonic', return_value=10):
                health.tick()
            self.assertEqual(os.stat(health.path).st_mode & 0o777, 0o600)
            self.assertEqual(supervisor.read_health(health.path, 12, 5, os.getpid()),
                             dict(pid=os.getpid(), updated=10, failed_since=None))
            self.assertIsNone(supervisor.read_health(health.path, 16, 5))
            self.assertIsNone(supervisor.read_health(health.path, 9, 5))
            self.assertIsNotNone(supervisor.read_health(health.path, 9.95, 5))
            self.assertIsNone(supervisor.read_health(health.path, 12, 5, os.getpid() + 1))
            self.assertFalse(Path(health.path + '.new').exists())
            for content in ['bad', '1 nan -1', '1 10 nan', '1 10 11', '1 10 -2', '1 10 -1 extra']:
                Path(health.path).write_text(content)
                self.assertIsNone(supervisor.read_health(health.path, 12, 5))

    def test_only_sustained_transport_failure_is_published(self):
        with tempfile.TemporaryDirectory() as directory:
            health = agent.HealthHeartbeat('agent', directory)
            with patch.object(agent, 'monotonic', return_value=10):
                health.http_result(0, 28)
            with patch.object(agent, 'monotonic', return_value=30):
                health.http_result(0, 7)
                health.tick()
            self.assertEqual(supervisor.read_health(health.path, 30, 5)['failed_since'], 10)
            for status, code in [(200, 0), (204, 0), (401, 0), (503, 0), (0, 60), (0, 35)]:
                health.transport_failure_since = 10
                health.http_result(status, code)
                self.assertIsNone(health.transport_failure_since)


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Mock()
        with patch.object(supervisor, 'monotonic', return_value=0):
            self.health = supervisor.FirmwareHealth(self.bridge)

    def test_startup_grace_then_detects_missing_or_frozen_sketch(self):
        self.bridge.get.return_value = None
        self.assertFalse(self.health.failed(0))
        self.assertFalse(self.health.failed(179))
        self.assertTrue(self.health.failed(190))
        self.bridge.get.return_value = 'v1,1234'
        self.assertFalse(self.health.failed(200))
        self.assertFalse(self.health.failed(280))
        self.assertTrue(self.health.failed(290))
        self.bridge.get.assert_called_with('resac_health')

    def test_sensor_failure_and_millis_rollover_do_not_reset_healthy_firmware(self):
        for now, value in [(0, 'v1,4294967290'), (10, 'v1,5'), (90, 'v1,90000'), (300, 'v1,300000')]:
            self.bridge.get.return_value = value
            self.assertFalse(self.health.failed(now))
        self.assertEqual({call.args[0] for call in self.bridge.get.call_args_list}, {'resac_health'})

    def test_invalid_or_failed_bridge_response_never_counts_as_progress(self):
        for now, value in [(0, ''), (10, 'v1,-1'), (20, 'v2,1'), (30, 'v1,4294967296')]:
            self.bridge.get.return_value = value
            self.assertFalse(self.health.failed(now))
        self.bridge.get.side_effect = IOError('unavailable')
        self.assertTrue(self.health.failed(180))

    def test_reset_backoff_survives_brief_recovery(self):
        self.health.reserve_reset(180)
        self.assertEqual(self.health.next_reset, 780)
        self.bridge.get.return_value = None
        self.assertFalse(self.health.failed(500))
        self.bridge.get.return_value = 'v1,1'
        self.assertFalse(self.health.failed(510))
        self.assertTrue(self.health.recovered)
        self.assertFalse(self.health.failed(600))
        self.assertTrue(self.health.failed(780))
        self.health.reserve_reset(780)
        self.assertEqual(self.health.next_reset, 1980)


class NativeFirmwareTests(unittest.TestCase):
    def connection(self, payload):
        buffer = [struct.pack('!I', len(payload)) + payload]
        connection = Mock()
        def recv(size):
            size = min(size, 2)
            result, buffer[0] = buffer[0][:size], buffer[0][size:]
            return result
        connection.recv.side_effect = recv
        return connection

    def test_fragmented_binary_snapshot_is_read_without_writing_bridge_values(self):
        name, value = b'resac_health', b'v1,1234'
        connection = self.connection(b'S' + struct.pack('!BH', len(name), len(value)) + name + value)
        with patch.object(supervisor.socket, 'create_connection', return_value=connection) as connect:
            self.assertEqual(supervisor.NativeFirmwareBridge().get('resac_health'), 'v1,1234')
        connect.assert_called_once_with(('127.0.0.1', 5701), 2)
        connection.sendall.assert_called_once_with(b'\0\0\0\1s')
        connection.close.assert_called_once()

    def test_invalid_or_truncated_snapshot_cannot_count_as_firmware_progress(self):
        for payload in [b'S\x01', b'S' + struct.pack('!BH', 65, 0), b'S' + struct.pack('!BH', 1, 513)]:
            connection = self.connection(payload)
            with patch.object(supervisor.socket, 'create_connection', return_value=connection):
                with self.assertRaises(ValueError):
                    supervisor.NativeFirmwareBridge().get('resac_health')
            connection.close.assert_called_once()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.child = Mock(pid=12345)
        self.child.poll.return_value = None
        self.service = supervisor.Supervisor('agent', self.directory.name)
        self.spawn = patch.object(supervisor.subprocess, 'Popen', return_value=self.child).start()
        self.signals = patch.object(self.service, 'signal_group').start()
        self.addCleanup(patch.stopall)

    def heartbeat(self, now):
        Path(self.service.health_path).write_text('12345 %s -1\n' % now)

    def test_frozen_loop_gets_term_then_kill_before_respawn(self):
        self.service.tick(0)
        self.service.tick(119)
        self.signals.assert_not_called()
        self.service.tick(120)
        self.signals.assert_called_with(self.child, signal.SIGTERM)
        self.service.tick(124)
        self.assertEqual(self.spawn.call_count, 1)
        self.service.tick(125)
        self.signals.assert_called_with(self.child, signal.SIGKILL)
        self.assertEqual(self.spawn.call_count, 1)
        self.child.poll.return_value = -9
        self.service.tick(126)
        self.service.tick(127)
        self.assertEqual(self.spawn.call_count, 2)
        self.assertTrue(Path(self.service.output_path + '.last-failure').exists())

    def test_old_heartbeat_cannot_mask_dead_child_and_restart_has_backoff(self):
        Path(self.service.health_path).write_text('99 0 -1')
        self.service.tick(0)
        self.assertFalse(Path(self.service.health_path).exists())
        self.child.poll.return_value = 1
        self.service.tick(1)
        self.service.tick(2)
        self.service.tick(5)
        self.assertEqual(self.spawn.call_count, 1)
        self.service.tick(6)
        self.assertEqual(self.spawn.call_count, 2)

    def test_failed_process_start_is_retried_with_bounded_backoff(self):
        self.spawn.side_effect = OSError('unavailable')
        self.service.tick(0)
        self.assertEqual(self.service.next_start, 5)
        self.service.tick(4)
        self.assertEqual(self.spawn.call_count, 1)
        self.service.tick(5)
        self.assertEqual(self.service.next_start, 15)
        for now in range(1000, 15000, 1000):
            self.service.tick(now)
        self.assertEqual(self.service.backoff, 300)

    def test_healthy_loop_never_restarts_for_backend_outage(self):
        self.service.tick(0)
        for now in range(10, 1000, 10):
            Path(self.service.health_path).write_text('12345 %d 0\n' % now)
            self.service.tick(now)
        self.assertEqual(self.spawn.call_count, 1)
        self.signals.assert_not_called()
        self.assertEqual(self.service.backoff, 5)

    def test_manual_stop_disables_respawn_and_does_not_reset_avr(self):
        self.service.firmware = Mock()
        self.service.tick(0)
        self.child.poll.return_value = 0
        self.service.close()
        self.service.tick(1000)
        self.assertEqual(self.spawn.call_count, 1)
        self.service.firmware.reserve_reset.assert_not_called()

    def test_firmware_reset_waits_until_agent_is_stopped(self):
        firmware = Mock()
        firmware.failed.return_value = True
        self.service.firmware = firmware
        self.service.tick(0)
        self.heartbeat(180)
        self.service.tick(180)
        firmware.reserve_reset.assert_not_called()
        self.child.poll.return_value = 0
        self.service.tick(181)
        firmware.reserve_reset.assert_called_once_with(181)
        self.assertEqual(self.spawn.call_args.args[0], ['/usr/bin/reset-mcu'])
        self.assertIsNotNone(self.service.reset)

    def test_actual_process_group_cleanup_and_shutdown_without_respawn(self):
        # Exercise real OS processes, including an orphaned subprocess that
        # ignores SIGTERM. A new agent must not inherit the old curl/helper.
        patch.stopall()
        source = ('import subprocess,sys,time,signal\n'
                  'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                  'p=subprocess.Popen([sys.executable,"-c","import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"])\n'
                  'print(p.pid,flush=True)\n'
                  'time.sleep(60)\n')
        service = supervisor.Supervisor('agent', self.directory.name,
                                        command=[sys.executable, '-u', '-c', source])
        child = None
        try:
            service.tick(time.monotonic())
            child = service.process
            deadline = time.monotonic() + 3
            while (not Path(service.output_path).read_text().strip()) and time.monotonic() < deadline:
                time.sleep(.01)
            descendant = int(Path(service.output_path).read_text().strip())
            service.stop_child('main_loop_stalled', time.monotonic() - 6)
            service.tick(time.monotonic())
            child.wait(timeout=3)
            service.tick(time.monotonic())
            self.assertIsNone(service.process)
            status = Path('/proc/%d/stat' % descendant)
            deadline = time.monotonic() + 3
            while status.exists() and status.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
            service.close()
            service.tick(time.monotonic() + 1000)
            self.assertIsNone(service.process)
        finally:
            service.close()
            if child is not None:
                child.wait(timeout=3)


class HttpDeadlineTests(unittest.TestCase):
    def test_process_exit_racing_with_deadline_does_not_crash_the_agent(self):
        request = agent.CurlUpload.__new__(agent.CurlUpload)
        request.deadline, request.expired = 0, False
        request.result = None
        request.process = Mock()
        request.process.poll.side_effect = [None, 0]
        request.process.kill.side_effect = OSError(errno.ESRCH, 'already exited')
        self.assertTrue(agent.request_completed(request))

    def test_nonresponsive_http_process_is_killed_and_reported_as_retryable_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / 'curl'
            fake.write_text('#!/bin/sh\nexec sleep 60\n')
            fake.chmod(0o700)
            config = dict(server_url='https://example.invalid', device_id='test', device_token='SECRET',
                          ca_file='/unused', curl_binary=str(fake), _health=agent.HealthHeartbeat('agent', directory))
            request = agent.CurlUpload(config, timeout=1)
            try:
                with patch.object(agent, 'monotonic', return_value=request.deadline + 1):
                    agent.request_completed(request)
                request.process.wait(timeout=3)
                self.assertTrue(agent.request_completed(request))
                with self.assertRaises(agent.UploadError) as error:
                    request.finish()
                self.assertEqual(error.exception.curl_code, 28)
                self.assertIsNotNone(config['_health'].transport_failure_since)
            finally:
                request.close()
            self.assertTrue(all(not Path(path).exists() for path in request.files))


if __name__ == '__main__':
    unittest.main()
