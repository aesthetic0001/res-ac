#!/usr/bin/env python
"""Independent on-device process and AVR supervision for Python 2.7/3."""
from __future__ import print_function
import errno
import os
import signal
import socket
import struct
import subprocess
import sys
import time

from res_ac_agent import BridgeStore, monotonic, text_type, decode_json


def read_health(path, now, max_age, pid=None):
    try:
        with open(path) as stream:
            fields = stream.read(128).split()
        owner, updated, failed = int(fields[0]), float(fields[1]), float(fields[2])
        if len(fields) != 3 or owner <= 0 or (pid is not None and owner != pid):
            return None
        # A child can publish between the parent's tick timestamp and this
        # read. Allow that small scheduling race without accepting an unrelated
        # clock/session or treating a healthy worker as stalled.
        if not -.25 <= now - updated <= max_age or not (failed == -1 or 0 <= failed <= updated):
            return None
        return dict(pid=owner, updated=updated, failed_since=None if failed == -1 else failed)
    except (IOError, OSError, ValueError, IndexError):
        return None


def bounded_log(path, message):
    try:
        if os.path.exists(path) and os.path.getsize(path) > 32768:
            os.rename(path, path + '.previous')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + message + '\n')
    except (IOError, OSError):
        pass


class NativeFirmwareBridge(object):
    """Read a bounded snapshot from the newer local binary Bridge protocol."""
    def get(self, key):
        connection = socket.create_connection(('127.0.0.1', 5701), 2)
        deadline = monotonic() + 2
        received = [0]
        def read_exact(size):
            data = b''
            while len(data) < size:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise IOError('Native Bridge health check timed out')
                connection.settimeout(remaining)
                block = connection.recv(size - len(data))
                received[0] += len(block)
                if not block or received[0] > 65536:
                    raise IOError('Native Bridge health response unavailable')
                data += block
            return data
        try:
            connection.sendall(b'\0\0\0\1s')
            while True:
                size = struct.unpack('!I', read_exact(4))[0]
                if not 1 <= size <= 32764:
                    raise ValueError('Invalid native Bridge health frame')
                frame = read_exact(size)
                if frame[:1] != b'S':
                    continue
                values, data = {}, frame[1:]
                while data:
                    if len(data) < 3:
                        raise ValueError('Truncated native Bridge health entry')
                    kn, vn = struct.unpack('!BH', data[:3])
                    if not 1 <= kn <= 64 or vn > 512 or len(data) < 3 + kn + vn or len(values) >= 32:
                        raise ValueError('Invalid native Bridge health entry')
                    name = data[3:3+kn].decode('ascii')
                    if name in values:
                        raise ValueError('Duplicate native Bridge health entry')
                    values[name] = data[3+kn:3+kn+vn].decode('utf-8')
                    data = data[3+kn+vn:]
                return values.get(key)
        finally:
            connection.close()


class FirmwareHealth(object):
    """Observe a dedicated loop heartbeat; sensor failures are not MCU hangs."""
    def __init__(self, bridge=None):
        self.bridge = bridge or BridgeStore()
        self.last_value = None
        self.last_progress = None
        self.started = monotonic()
        self.next_check = 0
        self.next_reset = 0
        self.cooldown = 600
        self.good_since = None
        self.recovering = False
        self.recovered = False

    def failed(self, now):
        if now < self.next_check:
            return False
        self.next_check = now + 10
        try:
            value = self.bridge.get('resac_health')
            fields = value.split(',') if isinstance(value, (str, text_type)) else []
            if len(fields) != 2 or fields[0] != 'v1' or not fields[1].isdigit() or not 0 <= int(fields[1]) <= 0xffffffff:
                value = None
        except (IOError, OSError, ValueError):
            value = None
        if value is not None and value != self.last_value:
            self.last_value, self.last_progress = value, now
            if self.recovering:
                self.recovered, self.recovering = True, False
            if self.good_since is None:
                self.good_since = now
            if now - self.good_since >= 300:
                self.cooldown = 600
            return False
        if self.last_progress is not None and now - self.last_progress < 90:
            return False
        self.good_since = None
        return now - self.started >= 180 and now >= self.next_reset

    def reserve_reset(self, now):
        self.next_reset = now + self.cooldown
        self.cooldown = min(self.cooldown * 2, 3600)
        self.started, self.last_progress, self.last_value = now, None, None
        self.recovering = True


class Supervisor(object):
    def __init__(self, service, directory='/tmp', command=None, firmware=None):
        self.service = service
        self.directory = directory
        self.command = command or [sys.executable, '-u', '/usr/lib/res-ac/' +
                                  ('res_ac_agent.py' if service == 'agent' else 'network_watchdog.py')]
        self.health_path = directory + '/res-ac-' + service + '.health'
        self.log_path = directory + '/res-ac-' + service + '-supervisor.log'
        self.output_path = directory + '/res-ac-' + service + '.log'
        self.timeout = 120 if service == 'agent' else 180
        self.firmware = firmware
        self.process = None
        self.reset = None
        self.reset_started = None
        self.started = None
        self.good_since = None
        self.next_start = 0
        self.backoff = 5
        self.stopping = False
        self.kill_at = 0
        self.reason = None
        self.closed = False

    def report(self, message):
        bounded_log(self.log_path, message)

    def signal_group(self, process, signum):
        try:
            os.killpg(process.pid, signum)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise

    def start(self, now):
        # Remove any heartbeat left by an older PID/session before spawning.
        try:
            os.unlink(self.health_path)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
        fd = os.open(self.output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            with os.fdopen(fd, 'a') as output, open('/dev/null') as quiet:
                env = dict(os.environ, RESAC_SUPERVISED='1')
                self.process = subprocess.Popen(self.command, stdin=quiet, stdout=output,
                    stderr=subprocess.STDOUT, env=env, preexec_fn=os.setsid)
        except (IOError, OSError):
            self.next_start = now + self.backoff
            self.backoff = min(self.backoff * 2, 300)
            self.report('Child start failed; retry scheduled')
            return
        self.started, self.good_since = now, None
        self.report('Child started pid=%d' % self.process.pid)

    def stop_child(self, reason, now):
        if self.stopping:
            return
        self.reason, self.stopping = reason, True
        self.kill_at = now + 5
        self.next_start = now + self.backoff
        self.backoff = min(self.backoff * 2, 300)
        self.good_since = None
        self.report('Recovery reason=%s; stopping child pid=%d' % (reason, self.process.pid))
        # Keep the last bounded tail before restarting; never copy configuration.
        try:
            with open(self.output_path, 'rb') as source:
                source.seek(0, 2)
                source.seek(max(0, source.tell() - 32768))
                tail = source.read(32768)
            fd = os.open(self.output_path + '.last-failure', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as target:
                target.write(tail)
        except (IOError, OSError):
            pass
        self.signal_group(self.process, signal.SIGTERM)

    def reset_firmware(self, now):
        self.firmware.reserve_reset(now)
        try:
            with open('/dev/null', 'r+') as quiet:
                self.reset = subprocess.Popen(['/usr/bin/reset-mcu'], stdin=quiet,
                    stdout=quiet, stderr=quiet, preexec_fn=os.setsid)
            self.reset_started = now
            self.report('Resetting AVR after stale local firmware heartbeat')
        except (IOError, OSError):
            self.report('AVR reset unavailable; verify platform reset-mcu command')

    def tick(self, now):
        if self.reset is not None:
            result = self.reset.poll()
            if result is None and now - self.reset_started >= 10:
                self.signal_group(self.reset, signal.SIGKILL)
                self.report('AVR reset command timed out')
            if result is not None:
                self.signal_group(self.reset, signal.SIGKILL)
                self.report('AVR reset command finished status=%d' % result)
                self.reset = None
            return
        if self.process is not None:
            result = self.process.poll()
            if self.stopping:
                if result is not None or now >= self.kill_at:
                    # Kill descendants too: orphan curl/helper processes must
                    # not survive a restart and duplicate work or leak resources.
                    self.signal_group(self.process, signal.SIGKILL)
                if result is not None:
                    self.process = None
                    self.stopping = False
                    if self.reason == 'firmware_stalled' and not self.closed:
                        self.reset_firmware(now)
                    self.reason = None
                return
            if result is not None:
                self.stop_child('process_exit', now)
                return
            health = read_health(self.health_path, now, self.timeout, self.process.pid)
            if health is not None:
                if self.good_since is None:
                    self.good_since = now
                    self.report('Main-loop progress verified pid=%d' % self.process.pid)
                if now - self.good_since >= 300:
                    self.backoff = 5
            else:
                self.good_since = None
                if now - self.started >= self.timeout:
                    self.stop_child('main_loop_stalled', now)
                    return
            if self.firmware is not None and self.firmware.failed(now):
                self.stop_child('firmware_stalled', now)
            elif self.firmware is not None and self.firmware.recovered:
                self.report('Advancing AVR heartbeat verified firmware recovery')
                self.firmware.recovered = False
            return
        if not self.closed and now >= self.next_start:
            self.start(now)

    def close(self):
        self.closed = True
        if self.process is not None:
            self.stop_child('service_stopped', monotonic())
        deadline = monotonic() + 7
        while (self.process is not None or self.reset is not None) and monotonic() < deadline:
            self.tick(monotonic())
            time.sleep(0.05)
        for child in [self.process, self.reset]:
            if child is not None:
                self.signal_group(child, signal.SIGKILL)
                child.poll()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('agent', 'network'):
        raise SystemExit('Usage: service_supervisor.py agent|network')
    os.umask(0o077)
    service = sys.argv[1]
    firmware = None
    if service == 'agent':
        try:
            with open('/etc/res-ac/agent.json') as stream:
                native = decode_json(stream.read()).get('native_bridge', False)
            firmware = FirmwareHealth(NativeFirmwareBridge() if native else BridgeStore())
        except (IOError, OSError, ValueError):
            bounded_log('/tmp/res-ac-agent-supervisor.log',
                        'Firmware watchdog configuration unreadable; MCU resets disabled')
    supervisor = Supervisor(service, firmware=firmware)
    running = [True]
    def stop(signum, frame):
        running[0] = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while running[0]:
            try:
                supervisor.tick(monotonic())
            except Exception as error:
                supervisor.report('Watchdog check failed: ' + type(error).__name__)
            time.sleep(1)
    finally:
        supervisor.close()


if __name__ == '__main__':
    main()
