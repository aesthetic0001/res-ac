#!/usr/bin/env python
"""Linux-side telemetry and IR command agent for Python 2.7/3 and legacy Yun JSON.

Private configuration: /etc/res-ac/agent.json. No deployment addresses or tokens
are included here. --probe validates HTTPS and device authentication without
inserting a synthetic reading. Sampling and upload retries run independently.
"""
from __future__ import print_function
import binascii
import errno
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time

CONFIG_PATH = '/etc/res-ac/agent.json'
LOG_PATH = '/tmp/res-ac-agent.log'
COMMAND_STATE_PATH = '/etc/res-ac/command-result.json'
CURVE_PATH = '/etc/res-ac/fan-curve.json'
AUTO_STATE_PATH = '/tmp/res-ac-auto-command-result.json'
COMMAND_ID = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
monotonic = getattr(time, 'monotonic', lambda: os.times()[4])
try:
    text_type = unicode
except NameError:
    text_type = str
try:
    integer_types = (int, long)
except NameError:
    integer_types = (int,)

# Power/speed codes corrected for PCF-SC15T-CT. Other codes remain provisional.
# 0x16 is speed up, so the old profile's vertical alias is not supported.
FAN_ADDRESS = 0xDE80
FAN_BUTTONS = dict(power=0x00, speed_up=0x16, speed_down=0x14, breeze=0x09,
                   horizontal=0x15, timer_1h=0x12,
                   timer_2h=0x0e, timer_4h=0x0a)


def valid_hysteresis(value):
    if isinstance(value, bool) or not isinstance(value, integer_types + (float,)):
        return False
    try:
        return value >= 0 and float(value) < float('inf')
    except OverflowError:
        return False


def command_payload_attributes(command_type, payload):
    fields = {'device.ping': [], 'fan.command': ['button', 'sessionId'], 'fan.level': ['level', 'sessionId'],
              'fan.calibrate': ['confirmOff', 'sessionId'],
              'fan.curve': ['enabled', 'thresholdsC', 'hysteresisC', 'sessionId'],
              'ir.send': ['protocol', 'address', 'command', 'repeats', 'sessionId']}
    if command_type not in fields or not isinstance(payload, dict):
        return {}
    def integer(value, limit):
        return not isinstance(value, bool) and isinstance(value, integer_types) and 0 <= value <= limit
    def number(value, low, high):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and low <= value <= high
    rules = dict(button=lambda value: isinstance(value, (str, text_type)) and value in FAN_BUTTONS,
                 sessionId=lambda value: isinstance(value, (str, text_type)) and bool(re.match(r'^[a-f0-9]{32}$', value)),
                 level=lambda value: integer(value, 5), confirmOff=lambda value: isinstance(value, bool),
                 enabled=lambda value: isinstance(value, bool),
                 thresholdsC=lambda value: isinstance(value, list) and len(value) == 5 and all(number(v, -20, 60) for v in value),
                 hysteresisC=valid_hysteresis, protocol=lambda value: value == 'NEC',
                 address=lambda value: integer(value, 65535), command=lambda value: integer(value, 255),
                 repeats=lambda value: integer(value, 3))
    safe = dict((key, payload[key]) for key in fields[command_type] if key in payload and rules[key](payload[key]))
    return {'command.payload': encode_json(safe)}


def validate_curve(payload):
    if (not isinstance(payload, dict) or set(payload) - set(['enabled', 'thresholdsC', 'hysteresisC', 'sessionId']) or
            not isinstance(payload.get('enabled'), bool)):
        raise ValueError('Invalid automatic curve settings')
    thresholds = payload.get('thresholdsC')
    if (not isinstance(thresholds, list) or len(thresholds) != 5 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not -20 <= v <= 60 for v in thresholds) or
            any(b < a for a, b in zip(thresholds, thresholds[1:]))):
        raise ValueError('Use five nondecreasing temperatures from -20 to 60 C; equal thresholds are allowed')
    hysteresis = payload.get('hysteresisC', 1)
    if not valid_hysteresis(hysteresis):
        raise ValueError('Hysteresis must be a finite, nonnegative number in C')
    return dict(enabled=payload['enabled'], thresholdsC=thresholds, hysteresisC=hysteresis)


def curve_target(temperature, thresholds, hysteresis, previous=None):
    target = sum(temperature >= threshold for threshold in thresholds)
    if previous is not None and target < previous:
        while previous > target and temperature <= thresholds[previous - 1] - hysteresis:
            previous -= 1
        return previous
    return target


def ir_payload(command):
    payload = command.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('IR payload must be an object')
    if command['type'] == 'fan.command':
        button = payload.get('button')
        if not isinstance(button, (str, text_type)) or button not in FAN_BUTTONS:
            raise ValueError('Unknown fan button')
        if set(payload) - set(['button', 'sessionId']):
            raise ValueError('fan.command accepts button and sessionId')
        return FAN_ADDRESS, FAN_BUTTONS[button], 0
    if payload.get('protocol') != 'NEC' or set(payload) - set(['protocol', 'address', 'command', 'repeats', 'sessionId']):
        raise ValueError('ir.send requires NEC protocol and numeric address/command')
    values = (payload.get('address'), payload.get('command'), payload.get('repeats', 0))
    for value, limit in zip(values, (65535, 255, 3)):
        if isinstance(value, bool) or not isinstance(value, integer_types) or not 0 <= value <= limit:
            raise ValueError('Invalid NEC address, command or repeats')
    return values


def command_expiry(command):
    value = command.get('expiresAt', '')
    if not isinstance(value, (str, text_type)) or not re.match(
            r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$', value):
        raise ValueError('IR command requires UTC expiresAt')
    # Avoid datetime/calendar/_strptime: the board has a minimal Python 2 stdlib.
    year, month, day, hour, minute, second = [int(v) for v in re.findall(r'\d+', value)[:6]]
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    if not (1970 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= months[month - 1]
            and hour < 24 and minute < 60 and second < 60):
        raise ValueError('Invalid command expiry')
    prior = year - 1
    days = 365 * prior + prior // 4 - prior // 100 + prior // 400 - 719162
    days += sum(months[:month - 1]) + day - 1
    return days * 86400 + hour * 3600 + minute * 60 + second



def log(message):
    # Bound the RAM-backed startup log; never log URL/token or HTTP bodies.
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 131072:
            with open(LOG_PATH, 'w'):
                pass
    except OSError:
        pass
    print(message)
    sys.stdout.flush()


def encode_json(value):
    if hasattr(json, 'dumps'):
        return json.dumps(value, separators=(',', ':'), allow_nan=False)
    return json.write(value)


def decode_json(text):
    if hasattr(json, 'loads'):
        return json.loads(text)
    # The firmware's site-packages decoder accepts byte strings only.
    if sys.version_info[0] == 2 and isinstance(text, text_type):
        text = text.encode('utf-8')
    decoded = json.read(text)
    # Firmware ships two legacy variants: site-packages returns the object,
    # while Bridge's bundled stream parser returns (object, consumed_count).
    if isinstance(decoded, tuple) and len(decoded) == 2:
        value, used = decoded
        if text[used:].strip():
            raise ValueError('Trailing JSON content')
        return value
    return decoded


def load_config(path):
    if os.stat(path).st_mode & 0o077:
        raise ValueError('Private configuration must have mode 600')
    with open(path) as stream:
        config = decode_json(stream.read())
    if any(ord(char) < 33 for char in config.get('server_url', '')):
        raise ValueError('Invalid server URL characters')
    if not re.match(r'^https://(?:\[[a-fA-F0-9:]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?/?$', config.get('server_url', '')):
        raise ValueError('Configuration requires an HTTPS origin')
    if not re.match(r'^[A-Za-z0-9_-]{1,64}$', config.get('device_id', '')):
        raise ValueError('Invalid configured device ID')
    token = config.get('device_token', '')
    if len(token) < 32 or not re.match(r'^[A-Za-z0-9_-]+$', token):
        raise ValueError('Invalid configured device token')
    interval = config.get('interval_seconds', 10)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 2 <= interval <= 3600:
        raise ValueError('Invalid reporting interval')
    if not isinstance(config.get('native_bridge', False), bool):
        raise ValueError('native_bridge must be a boolean')
    limit = config.get('queue_limit', 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('Invalid queue limit')
    if not os.path.isfile(config.get('ca_file', '')):
        raise ValueError('CA trust file is missing')
    config['server_url'] = config['server_url'].rstrip('/')
    return config


def parse_sample(frame):
    if not isinstance(frame, (str, text_type)) or len(frame) > 120:
        raise ValueError('Invalid sensor frame')
    fields = frame.split(',')
    if len(fields) != 6 or fields[0] != 'v1' or not re.match(r'^[a-f0-9]{32}$', fields[1]):
        raise ValueError('Invalid sensor frame format')
    if not all(re.match(r'^-?\d+$', field) for field in fields[2:]):
        raise ValueError('Invalid sensor numeric field')
    sequence, temperature, humidity, uptime = [int(field) for field in fields[2:]]
    if not 1 <= sequence <= 0xffffffff or not 0 <= uptime <= 0xffffffff:
        raise ValueError('Invalid sensor sequence or uptime')
    if not -80000 <= temperature <= 150000 or not 0 <= humidity <= 100000:
        raise ValueError('Sensor reading outside accepted range')
    return dict(readingId=fields[1] + '-' + str(sequence),
                temperatureC=temperature / 1000.0, humidity=humidity / 1000.0)


COMMAND_GPIO_PROTOCOL = 'gpio21-pb2-v1'


class CommandGPIO(object):
    """Exclusive, persistent sysfs GPIO outputs for Seeeduino Cloud v1.0.

    GPIO21 carries an active-low command-pending level; GPIO19 enables the board's SPI
    voltage translators. Never steal exported pins or unbind a kernel driver.
    A locked RAM-only ownership record permits cleanup after an agent crash.
    """
    def __init__(self, root='/sys/class/gpio', lock_path='/var/run/res-ac-command-gpio.lock',
                 debug_path='/sys/kernel/debug/gpio'):
        self.root, self.lock_path = root, lock_path
        self.debug_path = debug_path
        self.lock_fd = self.value_fd = None
        self.owned = []
        self.enable_restore = None

    def _read(self, pin, name):
        with open('%s/gpio%d/%s' % (self.root, pin, name)) as stream:
            return stream.read().strip()

    @staticmethod
    def _ownership(record):
        fields = record.split(';')
        prefix = COMMAND_GPIO_PROTOCOL + ':'
        pins = fields[0][len(prefix):].split(',') if fields[0].startswith(prefix) else []
        restore = fields[1].split('=')[1] if (len(fields) == 2 and fields[0].startswith(prefix) and
                                            fields[1] in ('restore19=0', 'restore19=1')) else None
        return [int(pin) for pin in pins if pin in ('19', '21')], restore

    def _board_enable(self, debug_path=None):
        # The installed Linino kernel deliberately exports OE-1 with a fixed
        # output direction. Its writable value is the board's translator gate;
        # it is not an export that this agent may unexport or reconfigure.
        try:
            with open(debug_path or self.debug_path) as stream:
                return any(re.match(r'^\s*gpio-19\s+\(\s*OE-1\s*\)\s+out\s+(?:lo|hi)\b', line)
                           for line in stream)
        except IOError:
            return False

    def preflight(self, debug_path='/sys/kernel/debug/gpio'):
        """Read-only prerequisites; actual exclusive export happens at dispatch."""
        import fcntl
        if not hasattr(fcntl, 'flock'):
            raise IOError('GPIO notifications require flock support')
        for name in ('export', 'unexport'):
            if not os.access(self.root + '/' + name, os.W_OK):
                raise IOError('Writable GPIO sysfs is required')
        if not os.access(os.path.dirname(self.lock_path), os.W_OK):
            raise IOError('GPIO ownership directory is not writable')
        try:
            with open(self.lock_path) as stream:
                record = stream.read(128)
        except IOError as error:
            if error.errno != errno.ENOENT:
                raise
            record = ''
        recovered, _ = self._ownership(record)
        for pin in (21, 19):
            exists = os.path.isdir('%s/gpio%d' % (self.root, pin))
            if exists and pin not in recovered:
                if pin == 19 and self._board_enable(debug_path):
                    if not os.access(self.root + '/gpio19/value', os.W_OK):
                        raise IOError('Board translator enable is not writable')
                    continue
                if pin != 19 or self._read(pin, 'direction') != 'out' or self._read(pin, 'value') != '1':
                    raise IOError('GPIO is already exported by another owner')
        if os.path.isfile(debug_path):
            with open(debug_path) as stream:
                for line in stream:
                    match = re.match(r'^\s*gpio-(19|21)\s', line)
                    if match and not os.path.isdir('%s/gpio%s' % (self.root, match.group(1))):
                        raise IOError('GPIO is reserved by a kernel driver')

    def _write(self, path, value):
        fd = os.open(self.root + '/' + path, os.O_WRONLY)
        try:
            if os.write(fd, value.encode('ascii')) != len(value):
                raise IOError('Incomplete GPIO write')
        finally:
            os.close(fd)

    def _record(self):
        os.lseek(self.lock_fd, 0, os.SEEK_SET)
        os.ftruncate(self.lock_fd, 0)
        record = ('%s:%s' % (COMMAND_GPIO_PROTOCOL,
                            ','.join(str(pin) for pin in self.owned)))
        if self.enable_restore is not None:
            record += ';restore19=' + self.enable_restore
        record = record.encode('ascii')
        if os.write(self.lock_fd, record) != len(record):
            raise IOError('Incomplete GPIO ownership record')

    def open(self):
        if self.value_fd is not None:
            return
        # Lazy import preserves compatibility with old sketches and --probe on
        # minimal Python installs. GPIO preflight checks this dependency.
        import fcntl
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd)
            raise IOError('Command GPIO is already owned by another agent')
        self.lock_fd = fd
        try:
            os.fchmod(fd, 0o600)
            record = os.read(fd, 128).decode('ascii')
            recovered, self.enable_restore = self._ownership(record)
            self.owned = [pin for pin in recovered if os.path.isdir('%s/gpio%d' % (self.root, pin))]
            for pin in (21, 19):
                exists = os.path.isdir('%s/gpio%d' % (self.root, pin))
                if pin == 19 and exists and self._board_enable():
                    if self.enable_restore is None:
                        self.enable_restore = self._read(19, 'value')
                    if self.enable_restore not in ('0', '1'):
                        raise IOError('Invalid board translator enable state')
                    self._record()  # Persist the original value before changing it.
                    self._write('gpio19/value', '1')
                    continue
                if exists and pin not in recovered:
                    # GPIO19 may be deliberately enabled by board startup. Reuse
                    # only that existing high output, without taking ownership.
                    if pin == 19 and self._read(pin, 'direction') == 'out' and self._read(pin, 'value') == '1':
                        continue
                    raise IOError('Command GPIO is reserved; stop its owner before enabling notifications')
                if not exists:
                    self._write('export', str(pin))  # EBUSY also protects kernel-owned pins.
                if pin not in self.owned:
                    self.owned.append(pin)
                self._record()
                # Initialize idle high before enabling the translator; no low glitch.
                self._write('gpio%d/direction' % pin, 'high')
            self.value_fd = os.open(self.root + '/gpio21/value', os.O_WRONLY)
        except Exception:
            self.close()
            raise

    def set(self, pending):
        if self.value_fd is None:
            if not pending:
                return
            raise IOError('Command GPIO is not open')
        os.lseek(self.value_fd, 0, os.SEEK_SET)
        if os.write(self.value_fd, b'0' if pending else b'1') != 1:
            raise IOError('Incomplete command notification')

    def close(self):
        failed = False
        if self.value_fd is not None:
            try:
                self.set(False)
            except (IOError, OSError):
                failed = True
            os.close(self.value_fd)
            self.value_fd = None
        if self.lock_fd is not None:
            if self.enable_restore is not None:
                try:
                    if not self._board_enable():
                        raise IOError('Board translator ownership changed')
                    self._write('gpio19/value', self.enable_restore)
                    self.enable_restore = None
                except (IOError, OSError):
                    failed = True
            for pin in list(self.owned):
                try:
                    if pin == 21:
                        self._write('gpio21/direction', 'high')
                    self._write('unexport', str(pin))
                    self.owned.remove(pin)
                except (IOError, OSError):
                    failed = True
            try:
                self._record()
            except (IOError, OSError):
                failed = True
            finally:
                os.close(self.lock_fd)
                self.lock_fd = None
        if failed:
            log('Command GPIO cleanup incomplete; check recorded pin ownership before reuse')


class BridgeStore(object):
    """Short local TCP transactions; decode fragmented JSON without Yun client bugs."""
    def __init__(self, host='127.0.0.1', port=5700):
        self.host = host
        self.port = port

    def request(self, command, key, value=None):
        message = dict(command=command, key=key)
        if command == 'put':
            message['value'] = value
        connection = socket.create_connection((self.host, self.port), 2)
        connection.settimeout(2)
        try:
            connection.sendall(encode_json(message).encode('utf-8'))
            data = b''
            while len(data) < 16384:
                block = connection.recv(2048)
                if not block:
                    raise IOError('Bridge closed before returning a value')
                data += block
                try:
                    response = decode_json(data.decode('utf-8'))
                except Exception:
                    continue
                if not isinstance(response, dict) or response.get('key') != key:
                    raise ValueError('Unexpected Bridge response')
                return response.get('value')
            raise ValueError('Bridge response too large')
        finally:
            connection.close()

    def get(self, key):
        return self.request('get', key)

    def put(self, key, value):
        return self.request('put', key, value)


class BridgeEvents(object):
    """One nonblocking Mailbox stream; reads use a snapshot plus pushed changes.

    The Yun server broadcasts replies to all clients, so only the initial get-all
    response populates the snapshot. Later replies cannot overwrite newer events.
    Pending writes are discarded on disconnect, especially physical commands.
    """
    keys = frozenset(['resac_events', 'resac_epoch', 'resac_interval_ms', 'resac_sample',
                      'resac_sensor_status', 'resac_sensor_diagnostic', 'resac_command_ready',
                      'resac_command_result', 'resac_command_diagnostic', 'resac_fan_state',
                      'resac_fan_session', 'resac_fan_heartbeat', 'resac_fan_invalidate',
                      'resac_command', 'resac_command_status', 'resac_upload_status',
                      'resac_command_notify', 'resac_command_protocol', 'resac_fan_control',
                      'resac_timing_arm', 'resac_timing_dump', 'resac_native_bridge'])

    def __init__(self, host='127.0.0.1', port=None, command_gpio=None, timing=None, native=False):
        self.host, self.port = host, (5701 if native else 5700) if port is None else port
        self.native = native
        self.connection = None
        self.connecting = False
        self.ready = False
        self.snapshot_pending = False
        self.deadline = 0
        self.retry_at = 0
        self.backoff = 1
        self.generation = 0
        self.revisions = {}
        self.values = {}
        self.samples = []
        self.dropped = 0
        self.sample_live = False
        self.sample_received = None
        self.firmware_warning = False
        self.outgoing = b''
        self.incoming = bytearray()
        self.depth = 0
        self.quoted = False
        self.escaped = False
        self.command_gpio = command_gpio
        self.timing = timing
        self.receive_sequence = 0
        self.notification = None
        self.control_session = None
        self.control_confirmed = False
        self.control_heartbeat = 1

    def version(self, *keys):
        return (self.generation,) + tuple(self.revisions.get(key, 0) for key in keys)

    def get(self, key):
        if not self.ready:
            raise IOError('Bridge event stream is not ready')
        return self.values.get(key)

    def put(self, key, value):
        if not self.ready:
            raise IOError('Bridge event stream is not ready')
        self._queue(dict(command='put', key=key, value=value))
        self._update(key, value)

    def fan_control(self, heartbeat=None, invalidate=False):
        if self.get('resac_command_protocol') not in ('batched-v1', 'batched-v2'):
            raise IOError('Batched fan context is not supported')
        if invalidate or self.control_session is None:
            self.control_session = binascii.hexlify(os.urandom(16)).decode('ascii')
            self.control_confirmed = False
        if heartbeat is not None:
            self.control_heartbeat = heartbeat
        self.put('resac_fan_control', 'v1,%s,%d' % (self.control_session, self.control_heartbeat))

    def invalidate_fan(self):
        if self.get('resac_command_protocol') in ('batched-v1', 'batched-v2'):
            self.fan_control(invalidate=True)
        else:
            self.put('resac_fan_invalidate', '1')

    def dispatch_command(self, frame, deadline, on_signal=None):
        capability = self.get('resac_command_notify')
        if capability and capability != COMMAND_GPIO_PROTOCOL:
            raise IOError('Unsupported command GPIO wiring profile')
        if not capability:
            self.put('resac_command', frame)  # Older firmware retains its polling handler.
            return
        if self.command_gpio is None:
            raise IOError('GPIO command firmware requires GPIO support in the agent')
        if self.notification is not None:
            raise IOError('An AVR command notification is already pending')
        if monotonic() >= deadline:
            raise IOError('Command expired before Bridge dispatch')
        queued_at = monotonic()
        self.command_gpio.open()
        self.command_gpio.set(False)
        self.put('resac_command', frame)
        if self.timing:
            self.timing.record('agent.command_queue', command=frame.split(',')[1])
        # Wait for this exact put confirmation before asserting the wire. TCP
        # send completion alone does not establish that the datastore was updated.
        self.notification = dict(frame=frame, deadline=deadline, signaled=False, queued_at=queued_at, on_signal=on_signal)

    def cancel_command(self, command_id=None):
        pending = self.notification
        if pending is not None and (command_id is None or pending['frame'].split(',')[1] == command_id):
            self.notification = None
            try:
                self.command_gpio.set(False)
            except (IOError, OSError):
                self.command_gpio.close()
                raise

    def _close_command_gpio(self):
        self.notification = None
        if self.command_gpio is not None:
            self.command_gpio.close()

    def _expire_notification(self):
        if self.notification is not None and monotonic() >= self.notification['deadline']:
            self.cancel_command()

    def _queue(self, value):
        if self.native:
            from native_bridge_protocol import encode
            encoded = encode(value)
        else:
            encoded = encode_json(value).encode('utf-8')
        if len(self.outgoing) + len(encoded) > 8192:
            self._disconnect()
            raise IOError('Bridge write queue is full')
        self.outgoing += encoded

    def _update(self, key, value):
        if key not in self.keys or not isinstance(value, (str, text_type)):
            return
        if self.values.get(key) != value:
            self.values[key] = value
            self.revisions[key] = self.revisions.get(key, 0) + 1

    def _sample(self, live):
        frame = self.values.get('resac_sample')
        if not frame:
            return
        self.sample_live = live
        self.sample_received = monotonic()
        if len(self.samples) >= 128:
            self.samples.pop(0)
            self.dropped += 1
        self.samples.append((frame, self.values.get('resac_sensor_diagnostic'), self.sample_received))

    def drain_samples(self):
        samples, self.samples = self.samples, []
        return samples

    def _snapshot(self):
        self.control_session = None
        self.control_confirmed = False
        self._close_command_gpio()
        self.ready = False
        self.snapshot_pending = True
        self.deadline = monotonic() + 2
        self._queue(dict(command='get'))

    def _message(self, message):
        if not isinstance(message, dict):
            return
        pending = self.notification
        if (self.ready and pending is not None and message.get('response') == 'put' and
                message.get('key') == 'resac_command' and message.get('value') == pending['frame']):
            self._expire_notification()
            if self.notification is not None and not pending['signaled']:
                if self.timing:
                    ident = pending['frame'].split(',')[1]
                    self.timing.record('agent.put_confirm', command=ident)
                    self.timing.record('agent.gpio_begin', command=ident)
                self.command_gpio.set(True)
                if self.timing:
                    self.timing.record('agent.gpio_assert', command=ident)
                pending['signaled'] = True
                pending['signaled_at'] = monotonic()
                if pending.get('on_signal'):
                    pending['on_signal'](pending['signaled_at'] - pending['queued_at'])
            return
        if message.get('response') == 'get' and 'key' not in message and self.snapshot_pending:
            values = message.get('value')
            if not isinstance(values, dict) or values.get('resac_events') != 'v1':
                if not self.firmware_warning:
                    log('Bridge event firmware is required; waiting for matching firmware')
                    self.firmware_warning = True
                raise ValueError('Matching event firmware is required')
            if self.native and values.get('resac_native_bridge') != 'v1':
                raise ValueError('Native Bridge capability is required')
            self.values = {}
            for key, value in values.items():
                self._update(key, value)
            self.snapshot_pending = False
            self.ready = True
            self.backoff = 1
            self.firmware_warning = False
            self.generation += 1
            self.sample_live = False
            self._sample(False)
            return
        if message.get('request') != 'raw':
            return
        data = message.get('data')
        if not isinstance(data, (str, text_type)):
            return
        fields = data.split(',', 2)
        if len(fields) != 3 or fields[0] != 'v1' or fields[1] not in self.keys:
            return
        key, value = fields[1:]
        if self.timing and value == '1' and key in ('resac_timing_arm', 'resac_timing_dump'):
            if key == 'resac_timing_arm':
                self.timing.arm()
            else:
                self.timing.request_dump()
            return
        if self.timing and key == 'resac_command_result':
            from timing_trace import command_id
            self.timing.record('agent.completion_received', packet=self.receive_sequence,
                               command=command_id(value))
        if key == 'resac_events':
            if value == '':
                # AVR restart: discard old state and unsent actions immediately.
                self.ready = False
                self.snapshot_pending = False
                self.values = {}
                self.outgoing = b''
                self._close_command_gpio()
                self.sample_live = False
                self.generation += 1
                self.deadline = monotonic() + 2
            elif value == 'v1':
                self._snapshot()
            return
        if self.ready:
            self._update(key, value)
            if key == 'resac_fan_session' and value == self.control_session:
                self.control_confirmed = True
            if key == 'resac_sample':
                self._sample(True)

    def _receive(self, block):
        if self.native:
            from native_bridge_protocol import decode, MAX_FRAME
            self.incoming.extend(block)
            if len(self.incoming) > MAX_FRAME + 4 + 4096:
                raise ValueError('Native Bridge receive queue too large')
            while len(self.incoming) >= 4:
                length = struct.unpack('!I', bytes(self.incoming[:4]))[0]
                if not 0 < length <= MAX_FRAME:
                    raise ValueError('Native Bridge frame too large')
                if len(self.incoming) < 4 + length:
                    break
                frame = bytes(self.incoming[4:4+length])
                del self.incoming[:4+length]
                self._message(decode(frame))
            return
        # Frame concatenated JSON objects without relying on JSONDecoder, which
        # is missing from the board's legacy JSON module. Decode only full UTF-8.
        for char in block:
            number = char if isinstance(char, int) else ord(char)
            if not self.incoming and number in (9, 10, 13, 32):
                continue
            if not self.incoming and number != 123:
                raise ValueError('Expected a Bridge JSON object')
            self.incoming.append(number)
            if len(self.incoming) > 16384:
                raise ValueError('Bridge message too large')
            if self.quoted:
                if self.escaped:
                    self.escaped = False
                elif number == 92:
                    self.escaped = True
                elif number == 34:
                    self.quoted = False
            elif number == 34:
                self.quoted = True
            elif number in (123, 91):
                self.depth += 1
            elif number in (125, 93):
                self.depth -= 1
            if self.depth == 0:
                if self.timing:
                    self.timing.record('agent.json_begin', packet=self.receive_sequence)
                message = decode_json(self.incoming.decode('utf-8'))
                if self.timing:
                    self.timing.record('agent.json_end', packet=self.receive_sequence)
                self.incoming = bytearray()
                self._message(message)

    def _disconnect(self):
        self._close_command_gpio()
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        self.connecting = False
        self.ready = False
        self.snapshot_pending = False
        self.values = {}
        self.sample_live = False
        self.outgoing = b''
        self.incoming = bytearray()
        self.depth = 0
        self.quoted = self.escaped = False
        self.generation += 1
        self.retry_at = monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, 30)

    def poll(self, timeout=0):
        try:
            self._expire_notification()
            if self.connection is None:
                if monotonic() < self.retry_at:
                    if timeout:
                        select.select([], [], [], max(0, min(timeout, self.retry_at - monotonic())))
                    return
                self.connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.connection.setblocking(False)
                code = self.connection.connect_ex((self.host, self.port))
                if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                    raise IOError('Bridge connection failed')
                self.connecting = True
                self.deadline = monotonic() + 2
            if not self.ready and monotonic() >= self.deadline:
                raise IOError('Bridge handshake timed out')
            writable = [self.connection] if self.connecting or self.outgoing else []
            reads, writes, errors = select.select([self.connection], writable, [self.connection], timeout)
            if errors:
                raise IOError('Bridge connection failed')
            if self.connection in writes:
                if self.connecting:
                    if self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                        raise IOError('Bridge connection failed')
                    self.connecting = False
                    self._snapshot()
                if self.outgoing:
                    sent = self.connection.send(self.outgoing)
                    if sent == 0:
                        raise IOError('Bridge closed while sending')
                    self.outgoing = self.outgoing[sent:]
            if self.connection in reads:
                self.receive_sequence += 1
                if self.timing:
                    self.timing.record('agent.recv_begin', packet=self.receive_sequence)
                block = self.connection.recv(4096)
                if self.timing:
                    self.timing.record('agent.recv_end', packet=self.receive_sequence, value=len(block))
                if not block:
                    raise IOError('Bridge closed')
                self._receive(block)
        except (IOError, OSError, ValueError, select.error) as error:
            if getattr(error, 'errno', None) in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return
            self._disconnect()

    def close(self):
        self._disconnect()


class UploadError(Exception):
    def __init__(self, status=0, curl_code=0):
        self.status = status
        self.curl_code = curl_code
        Exception.__init__(self, 'HTTP status %d; curl exit %d' % (status, curl_code))


def temporary(prefix):
    for attempt in range(10):
        path = '/tmp/' + prefix + binascii.hexlify(os.urandom(12)).decode('ascii')
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), path
        except OSError:
            pass
    raise IOError('Could not create private request file')


TRACEPARENT = r'^00-(?!0{32})[a-f0-9]{32}-(?!0{16})[a-f0-9]{16}-01$'


def traceparent(parent=None):
    return '00-%s-%s-01' % (parent[3:35] if parent and re.match(TRACEPARENT, parent) else binascii.hexlify(os.urandom(16)).decode('ascii'),
                             binascii.hexlify(os.urandom(8)).decode('ascii'))


class Telemetry(object):
    """Bounded, RAM-only diagnostics; never waits for HTTP in tick()."""
    def __init__(self, config, bridge=None):
        self.config = config
        self.bridge = bridge or config.get('_bridge') or BridgeStore()
        self.session = binascii.hexlify(os.urandom(16)).decode('ascii')
        self.events = []
        self.dropped = 0
        self.request = None
        self.batch = None
        self.encoding = self.prepared_payload = None
        self.next_due = 0
        self.backoff = 5
        self.previous_cpu = None
        self.sensor_attempt = None
        self.last_sample_at = None
        self.sensor_errors = 0

    def event(self, name, parent=None, duration=0, attributes=None, span_id=None):
        try:
            if len(self.events) >= 128:
                self.events.pop(0)
                self.dropped += 1
            self.events.append(dict(name=name, traceparent=parent if parent and re.match(TRACEPARENT, parent) else traceparent(),
                                    durationMs=max(0, min(duration * 1000, 86400000)),
                                    attributes=attributes or dict(outcome='ok'), ended=monotonic()))
            if span_id is not None:
                self.events[-1]['spanId'] = span_id
            # A full batch is work, not a reason to wait for the idle heartbeat.
            # Failed batches retain their retry deadline and identity.
            if self.request is None and self.batch is None and len(self.events) >= 32:
                self.next_due = min(self.next_due, monotonic())
        except Exception:
            self.dropped += 1

    def resources(self):
        values = {}
        try:
            with open('/proc/stat') as stream:
                fields = [int(v) for v in stream.readline().split()[1:9]]
            total, idle = sum(fields), fields[3] + fields[4]
            if self.previous_cpu and total > self.previous_cpu[0]:
                values['cpu_ratio'] = max(0, min(1, 1 - float(idle - self.previous_cpu[1]) / (total - self.previous_cpu[0])))
            self.previous_cpu = (total, idle)
            with open('/proc/meminfo') as stream:
                memory = dict((line.split(':')[0], int(line.split()[1]) * 1024) for line in stream if ':' in line)
            values['memory_total_bytes'] = memory['MemTotal']
            values['memory_available_bytes'] = memory.get('MemAvailable', memory.get('MemFree', 0) + memory.get('Buffers', 0) + memory.get('Cached', 0))
            with open('/proc/self/statm') as stream:
                values['agent_rss_bytes'] = int(stream.read().split()[1]) * (os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096)
            with open('/proc/uptime') as stream:
                values['uptime_seconds'] = float(stream.read().split()[0])
            storage = os.statvfs('/')
            values['disk_free_bytes'] = storage.f_bavail * storage.f_frsize
        except (IOError, OSError, ValueError, KeyError, AttributeError):
            pass
        return values

    def sensor(self, parent=None, sample_id=None, diagnostic=None):
        try:
            fields = (diagnostic if diagnostic is not None else self.bridge.get('resac_sensor_diagnostic') or '').split(',')
            if (len(fields) != 8 or fields[0] != 'v1' or not re.match(r'^[a-f0-9]{32}$', fields[1]) or
                    any(not re.match(r'^\d{1,10}$', fields[i]) for i in (2, 3, 4, 6, 7)) or fields[5] not in ('ok', 'failed')):
                return
            identity = (fields[1], fields[3])
            if identity == self.sensor_attempt:
                return
            reading_id = fields[1] + '-' + fields[2]
            # A successful diagnostic can precede publication of its sample.
            # Wait for the matching sample instead of consuming it under an old ID.
            if fields[5] == 'ok' and sample_id != reading_id:
                return
            self.sensor_attempt = identity
            self.sensor_errors = int(fields[7])
            attributes = dict(outcome=fields[5])
            attributes['avr.uptime_ms'] = int(fields[6])
            if fields[5] == 'ok':
                attributes['reading.id'] = reading_id
            if fields[5] == 'failed':
                parent = traceparent()
                self.event('reading.collect', parent, attributes={'outcome': 'failed'})
            self.event('sensor.read', parent if sample_id == reading_id or fields[5] == 'failed' else None,
                       int(fields[4]) / 1000.0, attributes)
        except Exception:
            pass

    def tick(self, pending):
        try:
            self._tick(pending)
        except Exception:
            # Diagnostics must never terminate the agent or delay control retries.
            if self.request is not None:
                self.request.close()
            self.request = None
            self.encoding = self.prepared_payload = None
            self.next_due = monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, 60)

    def _tick(self, pending):
        if self.request is not None:
            if not request_completed(self.request):
                return
            try:
                status, unused = self.request.finish()
                if status != 200:
                    raise UploadError(status=status)
                self.batch = None
                self.backoff = 5
                self.next_due = monotonic() + (0 if len(self.events) >= 32 else 15)
            finally:
                self.request.close()
                self.request = None
        if monotonic() < self.next_due:
            return
        if self.encoding is not None:
            encoded = self.encoding.step()
            if encoded is not None:
                payload = self.prepared_payload
                self.encoding = self.prepared_payload = None
                self.request = CurlUpload(self.config, payload, route='telemetry', timeout=10, encoded_body=encoded)
            return
        if self.batch is None:
            self.sensor()
            events, self.events = self.events[:32], self.events[32:]
            self.batch = dict(session=self.session, batchId=binascii.hexlify(os.urandom(16)).decode('ascii'),
                              metrics={}, events=events)
        now = monotonic()
        # Keep batch identity for retry deduplication, but advance elapsed ages.
        events = [dict((key, value) for key, value in event.items() if key != 'ended') for event in self.batch['events']]
        for event, original in zip(events, self.batch['events']):
            event['ageMs'] = max(0, (now - original['ended']) * 1000)
        # Diagnostics older than the receiver's seven-day window are discarded.
        retained = [event for event in self.batch['events'] if max(0, (now - event['ended']) * 1000) <= 604800000]
        self.dropped += len(self.batch['events']) - len(retained)
        self.batch['events'] = retained
        metrics = self.resources()
        metrics.update(buffer_depth=len(pending.queue), readings_dropped_total=pending.dropped,
                       telemetry_dropped_total=self.dropped, telemetry_queue_depth=len(self.events), sensor_errors_total=self.sensor_errors)
        if self.last_sample_at is not None:
            metrics['sensor_sample_age_seconds'] = max(0, now - self.last_sample_at)
        payload = dict(self.batch, metrics=metrics, events=[event for event in events if event['ageMs'] <= 604800000])
        if self.config.get('native_bridge', False):
            from cooperative_json import Encoding
            self.encoding, self.prepared_payload = Encoding(payload, encode_json), payload
            self.next_due = monotonic()  # Ready work on the existing selector, no added timer.
        else:
            self.request = CurlUpload(self.config, payload, route='telemetry', timeout=10)

    def close(self):
        self.encoding = self.prepared_payload = None
        if self.request is not None:
            self.request.close()


def network_snapshot():
    """Small local reads only: no probes, subprocesses, addresses or credentials."""
    result = {'ipv4_default_route': 'unknown', 'interface_state': 'unknown'}
    interface = None
    try:
        with open('/proc/net/route') as stream:
            lines = stream.read(16384).splitlines()
        if lines and lines[0].split()[:2] == ['Iface', 'Destination']:
            result['ipv4_default_route'] = 'absent'
            for line in lines[1:]:
                fields = line.split()
                if len(fields) >= 4 and fields[1] == '00000000' and int(fields[3], 16) & 1:
                    interface = fields[0]
                    result['ipv4_default_route'] = 'present'
                    break
    except (IOError, OSError, ValueError):
        result['ipv4_default_route'] = 'unknown'
    try:
        with open('/proc/net/wireless') as stream:
            lines = stream.read(4096).splitlines()[2:]
        wireless = [line.split(':', 1) for line in lines if ':' in line]
        selected = [fields for fields in wireless if fields[0].strip() == interface]
        if not selected and interface is None and len(wireless) == 1:
            selected = wireless
            interface = selected[0][0].strip()
        if selected:
            fields = selected[0][1].split()
            quality, signal_dbm = float(fields[1]), float(fields[2])
            if 0 <= quality <= 255:
                result['wifi_quality'] = quality
            # Some drivers report unsigned/relative values; do not call those dBm.
            if -130 <= signal_dbm < 0:
                result['wifi_signal_dbm'] = signal_dbm
    except (IOError, OSError, ValueError, IndexError):
        pass
    if interface and re.match(r'^[A-Za-z0-9_.:-]{1,15}$', interface):
        try:
            with open('/sys/class/net/' + interface + '/operstate') as stream:
                state = stream.read(32).strip()
            if state in ('up', 'down', 'dormant', 'notpresent', 'lowerlayerdown', 'testing', 'unknown'):
                result['interface_state'] = state
        except (IOError, OSError):
            pass
    return result


def curl_diagnostics(output, errors, code):
    """Export only fixed categories and validated numbers, never curl's raw text."""
    attributes = {}
    try:
        fields = output.decode('ascii', 'replace').strip().split()
        status = int(fields[0])
        if not 0 <= status <= 599:
            status = 0
    except (ValueError, IndexError, AttributeError):
        fields, status = [], 0
    timings = ['dns', 'connect', 'tls', 'first_byte', 'total']
    if len(fields) == 6:
        for name, field in zip(timings, fields[1:]):
            try:
                seconds = float(field)
                if 0 <= seconds <= 86400:
                    attributes['http.timing.' + name + '_ms'] = seconds * 1000
            except ValueError:
                pass
    return curl_error_status(status, attributes, errors, code)


def curl_error_status(status, attributes, errors, code):
    if code or not status or status >= 400:
        message = errors[:4096].decode('utf-8', 'replace').lower()
        # Only transport errors use these phrases: HTTP response text is never read.
        reason = {6: 'dns_resolution', 7: 'connect_failed', 28: 'timeout',
                  35: 'tls_handshake', 60: 'tls_certificate'}.get(code, 'transport_other') if code else ('http_error' if status else 'invalid_response')
        if code:
            for phrase, category in [('connection refused', 'connection_refused'),
                                     ('network is unreachable', 'network_unreachable'),
                                     ('no route to host', 'host_unreachable'),
                                     ('connection reset', 'connection_reset'),
                                     ('broken pipe', 'broken_pipe')]:
                if phrase in message:
                    reason = category
                    break
        stage = {6: 'dns', 7: 'connect', 35: 'tls', 60: 'tls'}.get(code, 'unknown')
        if not code:
            stage = 'http' if status else 'local'
        elif code == 28:
            # Completed phase times are evidence; a zero does not distinguish
            # an unfinished DNS lookup from an unfinished TCP connection.
            stage = 'response' if attributes.get('http.timing.tls_ms', 0) > 0 else (
                'tls' if attributes.get('http.timing.connect_ms', 0) > 0 else 'connect_or_dns')
        attributes.update({'error.reason': reason, 'error.stage': stage})
    return status, attributes


class HttpResult(object):
    def __init__(self, request_id):
        self.id = request_id
        self.completed = False
        self.code = 0
        self.status = 0
        self.connections = 0
        self.output = b''
        self.timings = (0, 0, 0, 0, 0)
        self.errors = b''
        self.body = b''
        self.received_at = None
        self.local_error = False


class HttpTransport(object):
    """A persistent libcurl helper; completion messages wake the shared selector."""
    def __init__(self, config):
        self.config = config
        self.connection = None
        self.process = None
        self.pending = {}
        self.serial = 0
        self.outgoing = b''
        self.incoming = b''

    @staticmethod
    def field(value):
        value = value.encode('utf-8') if isinstance(value, text_type) else value
        return struct.pack('!I', len(value)) + value

    def _queue(self, message):
        if len(self.outgoing) + len(message) + 4 > 150000:
            raise IOError('HTTP transport queue is full')
        self.outgoing += struct.pack('!I', len(message)) + message

    def _start(self):
        parent, child = socket.socketpair()
        try:
            parent.setblocking(False)
            with open(os.devnull, 'wb') as quiet:
                process = subprocess.Popen([self.config.get('http_helper', '/tmp/res-ac-http')],
                                           stdin=child, stdout=child, stderr=quiet, close_fds=True)
            self.connection, self.process = parent, process
            base = self.config['server_url'] + '/api/devices/' + self.config['device_id'] + '/'
            self._queue(b'I' + self.field(base) + self.field(self.config['ca_file']) +
                        self.field(self.config['device_token']))
        except BaseException:
            parent.close()
            self._failed()
            raise
        finally:
            child.close()

    def submit(self, route, parent, payload, method, timeout, encoded_body=None):
        if len(self.pending) >= 4:
            raise IOError('All HTTP request slots are occupied')
        if self.connection is None:
            self._start()
        self.serial = self.serial % 0xffffffff + 1
        result = HttpResult(self.serial)
        body = encoded_body if encoded_body is not None else (encode_json(payload) if payload is not None else b'')
        if isinstance(body, text_type):
            body = body.encode('utf-8')
        if len(body) > 32768 or method not in ('GET', 'POST') or not 1 <= timeout <= 60:
            raise ValueError('Invalid HTTP request')
        message = b'Q' + struct.pack('!II', result.id, timeout) + (b'G' if method == 'GET' else b'P')
        self._queue(message + self.field(route) + self.field(parent) + self.field(body))
        self.pending[result.id] = result
        return result

    def cancel(self, result):
        if result.id not in self.pending:
            return
        if not result.completed and self.connection is not None:
            try:
                self._queue(b'C' + struct.pack('!I', result.id))
            except IOError:
                self._failed()
        del self.pending[result.id]

    def _receive(self, block):
        self.incoming += block
        while len(self.incoming) >= 4:
            size = struct.unpack('!I', self.incoming[:4])[0]
            if size < 44 or size > 32812:
                raise ValueError('Invalid HTTP completion frame')
            if len(self.incoming) < size + 4:
                break
            frame, self.incoming = self.incoming[4:size + 4], self.incoming[size + 4:]
            fields = struct.unpack('!11I', frame[:44])
            request_id, code, status, connections, reason = fields[:5]
            if fields[10] != len(frame) - 44 or status > 599:
                raise ValueError('Invalid HTTP completion metadata')
            result = self.pending.get(request_id)
            if result is None:
                continue  # Completion raced with a cancellation.
            if result.completed:
                raise ValueError('Duplicate HTTP completion')
            result.code, result.status, result.connections = code, status, connections
            result.timings = fields[5:10]
            result.errors = {1: b'Connection refused', 2: b'Network is unreachable', 3: b'No route to host',
                             4: b'Connection reset', 5: b'Broken pipe'}.get(reason, b'')
            result.body = frame[44:]
            result.received_at = monotonic()
            result.completed = True

    def _failed(self):
        if self.connection is not None:
            self.connection.close()
        self.connection = None
        if self.process is not None:
            try:
                self.process.kill()
            except OSError:
                pass
            self.process.wait()
        self.process = None
        self.incoming = self.outgoing = b''
        for result in self.pending.values():
            if not result.completed:
                result.code, result.local_error, result.completed = 2, True, True

    def wait(self, bridge=None, timeout=None):
        if bridge is not None:
            before = (bridge.version(*bridge.keys), len(bridge.samples)) if isinstance(bridge, BridgeEvents) else None
            bridge.poll()  # Establish/recover Bridge without blocking.
            if before is not None and before != (bridge.version(*bridge.keys), len(bridge.samples)):
                # Do not consume a just-arrived event and then sleep until a
                # timer: let the application process that event immediately.
                timeout = 0
        connection = self.connection
        bridge_socket = getattr(bridge, 'connection', None)
        reads = [sock for sock in (connection, bridge_socket) if sock is not None]
        writes = []
        if connection is not None and self.outgoing:
            writes.append(connection)
        if bridge_socket is not None and (bridge.connecting or bridge.outgoing):
            writes.append(bridge_socket)
        try:
            readable, writable, errors = select.select(reads, writes, reads, timeout)
            if connection is not None:
                if connection in errors:
                    raise IOError('HTTP helper connection failed')
                if connection in writable:
                    sent = connection.send(self.outgoing)
                    if sent == 0:
                        raise IOError('HTTP helper closed')
                    self.outgoing = self.outgoing[sent:]
                if connection in readable:
                    block = connection.recv(65536)
                    if not block:
                        raise IOError('HTTP helper exited')
                    self._receive(block)
        except (IOError, OSError, ValueError, select.error) as error:
            if getattr(error, 'errno', None) not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                self._failed()
        if bridge is not None:
            bridge.poll()

    def close(self):
        self._failed()


def request_completed(request):
    # The running agent uses an in-memory flag set by a completion event.
    # Standalone CLI probes and older test adapters retain their process API.
    if hasattr(request, 'completed'):
        return request.completed
    return request.process.poll() is not None


class CurlUpload(object):
    """HTTP attempt backed by the shared helper, or standalone curl for probes."""
    def __init__(self, config, payload=None, route='readings', method='POST', timeout=25, encoded_body=None, journal_ms=None):
        self.process = None
        self.files = []
        self.transport = config.get('_http')
        self.result = None
        self.telemetry = config.get('_telemetry')
        self.started = monotonic()
        self.prepared_at, self.journal_ms = None, journal_ms
        self.reported = False
        self.kind = 'result' if route.endswith('/result') else route
        self.network_start = {}
        if self.telemetry is not None:
            try:
                self.network_start = network_snapshot()
            except Exception:
                pass
        supplied = (payload or {}).get('traceparent')
        self.root_request = not (isinstance(supplied, (str, text_type)) and re.match(TRACEPARENT, supplied))
        self.parent = traceparent() if self.root_request else supplied
        # Allocate a fresh CLIENT span for each attempt, before writing headers.
        # Keep the collection/command context as its parent across retries.
        self.request_traceparent = self.parent if self.root_request else traceparent(self.parent)
        try:
            if self.transport is not None:
                self.result = self.transport.submit(route, self.request_traceparent, payload, method, timeout, encoded_body=encoded_body)
                self.prepared_at = monotonic()
                return
            header_fd, header_path = temporary('res-ac-curl-')
            self.files.append(header_path)
            response_fd, self.response_path = temporary('res-ac-response-')
            self.files.append(self.response_path)
            os.close(response_fd)
            url = config['server_url'] + '/api/devices/' + config['device_id'] + '/' + route
            def quote(value):
                return value.replace('\\', '\\\\').replace('"', '\\"')
            with os.fdopen(header_fd, 'w') as stream:
                stream.write('url = "%s"\n' % quote(url))
                stream.write('header = "Authorization: Bearer %s"\n' % quote(config['device_token']))
                stream.write('header = "traceparent: %s"\n' % self.request_traceparent)
            arguments = [config.get('curl_binary', 'curl'), '--config', header_path, '--cacert', config['ca_file'],
                         '--silent', '--show-error', '--connect-timeout', '10', '--max-time', str(timeout),
                         '--max-filesize', '32768', '--request', method, '--header', 'Content-Type: application/json',
                         '--output', self.response_path, '--write-out',
                         '%{http_code} %{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total}']
            if payload is not None:
                payload_fd, payload_path = temporary('res-ac-payload-')
                self.files.append(payload_path)
                with os.fdopen(payload_fd, 'wb') as stream:
                    body = encoded_body if encoded_body is not None else encode_json(payload)
                    if isinstance(body, text_type): body = body.encode('utf-8')
                    stream.write(body)
                arguments += ['--data-binary', '@' + payload_path]
            self.process = subprocess.Popen(arguments,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Read request bodies from private RAM files: a large diagnostic batch
            # must not block the control loop while writing a subprocess pipe.
            self.process.stdin.close()
            self.process.stdin = None
            self.prepared_at = monotonic()
        except BaseException:
            self._report(0, 0, {'error.reason': 'request_setup', 'error.stage': 'local'})
            self.close()
            raise

    @property
    def completed(self):
        return self.result.completed if self.result is not None else self.process.poll() is not None

    def _report(self, status, code, diagnostics=None):
        if self.reported or self.telemetry is None:
            return
        self.reported = True
        # Telemetry attempts join the next ordinary scheduled batch. Reporting
        # an event never starts an upload or changes the heartbeat/backoff.
        outcome = ('ok' if 200 <= status < 300 else 'failed') if not code else {6: 'dns', 7: 'connect', 28: 'timeout', 35: 'tls', 60: 'tls'}.get(code, 'other')
        attributes = {'outcome': outcome, 'curl.code': code, 'http.status_code': status, 'request.kind': self.kind}
        attributes.update(diagnostics or {})
        ended = monotonic()
        prepared = getattr(self, 'prepared_at', None)
        received = getattr(getattr(self, 'result', None), 'received_at', None)
        if prepared is not None:
            attributes['http.agent.prepare_ms'] = max(0, (prepared - self.started) * 1000)
        if received is not None:
            attributes['http.agent.consume_ms'] = max(0, (ended - received) * 1000)
            if prepared is not None and 'http.timing.total_ms' in attributes:
                attributes['http.agent.ipc_wait_ms'] = max(0, (received - prepared) * 1000 - attributes['http.timing.total_ms'])
        if getattr(self, 'journal_ms', None) is not None:
            attributes['command.final_journal_ms'] = self.journal_ms
        if outcome != 'ok':
            try:
                attributes.update(('network.start.' + key, value) for key, value in getattr(self, 'network_start', {}).items())
                attributes.update(('network.' + key, value) for key, value in network_snapshot().items())
            except Exception:
                # Losing optional context must not mask the request failure.
                pass
        self.telemetry.event('http.request' if self.root_request else 'http.device', self.parent, monotonic() - self.started,
                             attributes, span_id=self.request_traceparent[36:52])

    def finish(self):
        if self.result is not None:
            if not self.result.completed:
                raise RuntimeError('HTTP request has not completed')
            code = self.result.code
            diagnostics = dict(('http.timing.' + name + '_ms', value / 1000.0)
                               for name, value in zip(['dns', 'connect', 'tls', 'first_byte', 'total'], self.result.timings))
            status, diagnostics = curl_error_status(self.result.status, diagnostics, self.result.errors, code)
        else:
            output, errors = self.process.communicate()
            code = self.process.returncode
            status, diagnostics = curl_diagnostics(output, errors, code)
        if (self.result is not None and code == 28 and not self.result.connections and
                not diagnostics.get('http.timing.connect_ms') and not diagnostics.get('http.timing.tls_ms')):
            # Reused connections have zero connect/TLS milestones. Those zeros
            # cannot establish whether a timeout happened during connection setup.
            diagnostics['error.stage'] = 'unknown'
        if self.result is not None and self.result.local_error:
            diagnostics.update({'error.reason': 'request_setup', 'error.stage': 'local'})
        if code:
            self._report(status, code, diagnostics)
            raise UploadError(curl_code=code)
        if not status:
            self._report(0, 0, diagnostics)
            raise UploadError()
        self._report(status, 0, diagnostics)
        if self.result is not None:
            return status, self.result.body
        with open(self.response_path, 'rb') as stream:
            return status, stream.read(32768)

    def close(self):
        if self.result is not None:
            self.transport.cancel(self.result)
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.communicate()
        for path in self.files:
            if os.path.exists(path):
                os.unlink(path)


def post(config, payload):
    owned = config.get('_http') is None and bool(config.get('http_helper'))
    transport = HttpTransport(config) if owned else config.get('_http')
    request_config = dict(config, _http=transport) if transport is not None else config
    upload = None
    try:
        upload = CurlUpload(request_config, payload)
        if transport is not None:
            # Standalone preflight is deliberately synchronous, but still waits
            # on completion events and uses the same verified HTTPS helper.
            deadline = monotonic() + 30
            while not upload.completed:
                if monotonic() >= deadline:
                    raise UploadError(curl_code=28)
                transport.wait(timeout=max(0, deadline - monotonic()))
        return upload.finish()
    finally:
        if upload is not None:
            upload.close()
        if owned:
            transport.close()


class Buffer(object):
    def __init__(self, limit, telemetry=None):
        self.telemetry = telemetry
        self.queue = []
        self.limit = limit
        self.last_id = None
        self.dropped = 0

    def add(self, sample):
        if sample['readingId'] == self.last_id:
            return False
        self.last_id = sample['readingId']
        if len(self.queue) >= self.limit:
            removed = self.queue.pop(0)
            self.dropped += 1
            if self.telemetry:
                self.telemetry.event('reading.drop', removed.get('traceparent'), attributes={'outcome': 'dropped', 'reading.id': removed['readingId']})
        if self.telemetry:
            sample['_queued_at'] = monotonic()
        self.queue.append(sample)
        return True

    def first(self):
        return self.queue[0] if self.queue else None

    def remove(self, sample):
        if self.queue and self.queue[0]['readingId'] == sample['readingId']:
            self.queue.pop(0)


class Collector(object):
    def __init__(self, config, pending, bridge=None):
        self.config = config
        self.pending = pending
        self.bridge = bridge or config.get('_bridge') or BridgeStore()
        self.initialized = False
        self.last_status = None
        self.next_due = 0
        self.bridge_version = None

    def _collect(self, frame, started, diagnostic=None):
        sample = parse_sample(frame)
        if sample['readingId'] != self.pending.last_id:
            sample['traceparent'] = traceparent()
            telemetry = self.config.get('_telemetry')
            if telemetry:
                telemetry.last_sample_at = monotonic()
                telemetry.event('reading.collect', sample['traceparent'], attributes={'outcome': 'ok', 'reading.id': sample['readingId']})
                telemetry.sensor(sample['traceparent'], sample['readingId'], diagnostic)
                telemetry.event('bridge.collect', sample['traceparent'], monotonic() - started,
                                attributes={'outcome': 'ok', 'reading.id': sample['readingId']})
        if time.gmtime().tm_year >= 2025:
            sample['recordedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        self.pending.add(sample)

    def tick(self):
        streaming = isinstance(self.bridge, BridgeEvents)
        if streaming:
            version = self.bridge.version('resac_epoch', 'resac_sensor_status', 'resac_sensor_diagnostic', 'resac_sample')
            if version == self.bridge_version and not self.bridge.samples:
                return
            self.bridge_version = version
        else:
            if monotonic() < self.next_due:
                return
            self.next_due = monotonic() + 0.5
        started = monotonic()
        try:
            if streaming:
                # Completed events stay useful even if the socket subsequently
                # closes. Move them to the upload buffer before checking health.
                self.pending.dropped += self.bridge.dropped
                self.bridge.dropped = 0
                for frame, diagnostic, received in self.bridge.drain_samples():
                    try:
                        self._collect(frame, received, diagnostic)
                    except ValueError:
                        continue
            if not self.initialized or not self.bridge.get('resac_epoch'):
                self.bridge.put('resac_epoch', binascii.hexlify(os.urandom(16)).decode('ascii'))
                self.bridge.put('resac_interval_ms', str(int(self.config.get('interval_seconds', 10) * 1000)))
                self.initialized = True
                log('Sensor Bridge connected')
            status = self.bridge.get('resac_sensor_status')
            if status != self.last_status:
                if status == 'read_error':
                    log('DHT11 read failed; invalid samples are not uploaded')
                self.last_status = status
            if not streaming:
                frame = self.bridge.get('resac_sample')
                if frame:
                    self._collect(frame, started)
            if self.config.get('_telemetry'):
                self.config['_telemetry'].sensor()
        except Exception:
            if self.initialized:
                log('Sensor Bridge unavailable; waiting for reconnect')
                if self.config.get('_telemetry'):
                    self.config['_telemetry'].event('bridge.error', attributes={'outcome': 'failed'})
            self.initialized = False
            self.next_due = monotonic() + 2



    def report_upload(self, value):
        try:
            self.bridge.put('resac_upload_status', value)
        except Exception:
            pass


def parse_fan_state(frame):
    fields = frame.split(',') if isinstance(frame, (str, text_type)) else []
    source = None
    if len(fields) == 7 and fields[0] == 'v2':
        source = fields.pop()
        if source not in ('reset', 'manual', 'automatic', 'calibration'):
            raise ValueError('Invalid fan control source')
        fields[0] = 'v1'
    if (len(fields) != 6 or fields[0] != 'v1' or
            not re.match(r'^(?:[a-f0-9]{32}|none)$', fields[1]) or
            fields[2] not in ('0', '1') or fields[3] not in ('0', '1') or
            not re.match(r'^[0-5]$', fields[4]) or not re.match(r'^\d{1,10}$', fields[5])):
        raise ValueError('Invalid fan state')
    calibrated, power, speed, revision = fields[2] == '1', fields[3] == '1', int(fields[4]), int(fields[5])
    if (revision > 0xffffffff or (calibrated and fields[1] == 'none') or
            (not calibrated and (power or speed)) or (power and not speed) or (not power and speed)):
        raise ValueError('Inconsistent fan state')
    result = dict(sessionId=None if fields[1] == 'none' else fields[1], calibrated=calibrated,
                power=('on' if power else 'off') if calibrated else 'unknown',
                speed=speed if calibrated else None, revision=revision)
    if source is not None:
        result['controlSource'] = source
    return result


class FanReporter(object):
    """Live AVR estimate, independent of sensor samples and their offline buffer."""
    def __init__(self, config, bridge=None, automation=None):
        self.config = config
        self.bridge = bridge or config.get('_bridge') or BridgeStore()
        self.request = None
        self.initialized = False
        self.next_due = 0
        self.sent_at = 0
        self.sent_state = None
        self.heartbeat = 0
        self.automation = automation
        self.bridge_version = None

    def tick(self):
        finished = False
        if self.request is not None and request_completed(self.request):
            finished = True
            try:
                status, body = self.request.finish()
                if status not in (200, 201):
                    self.sent_state = None
            except Exception:
                self.sent_state = None
            finally:
                self.request.close()
                self.request = None
        heartbeat_due = monotonic() >= self.next_due
        if isinstance(self.bridge, BridgeEvents):
            version = (self.bridge.version('resac_command_ready', 'resac_fan_session', 'resac_fan_state'),
                       self.automation.snapshot() if self.automation else None)
            changed = finished or version != self.bridge_version
            self.bridge_version = version
        else:
            changed = False
        if not heartbeat_due and not changed:
            return
        if heartbeat_due:
            self.next_due = monotonic() + 1
        try:
            ready = self.bridge.get('resac_command_ready')
            if ready not in ('v3', 'v4'):
                self.initialized = False
                return
            if isinstance(self.bridge, BridgeEvents) and self.bridge.get('resac_command_protocol') in ('batched-v1', 'batched-v2'):
                if heartbeat_due:
                    self.heartbeat = self.heartbeat % 0xffffffff + 1
                invalidate = (not self.initialized or self.bridge.control_session is None or
                              (self.bridge.control_confirmed and
                               self.bridge.get('resac_fan_session') != self.bridge.control_session))
                if heartbeat_due or invalidate:
                    self.bridge.fan_control(heartbeat=max(1, self.heartbeat), invalidate=invalidate)
                self.initialized = True
            else:
                if not self.initialized:
                    if isinstance(self.bridge, BridgeEvents):
                        self.bridge.invalidate_fan()
                    else:
                        self.bridge.put('resac_fan_invalidate', '1')
                    self.initialized = True
                if heartbeat_due:
                    self.heartbeat += 1
                    self.bridge.put('resac_fan_heartbeat', str(self.heartbeat))
                session = self.bridge.get('resac_fan_session') or ''
                if not re.match(r'^[a-f0-9]{32}$', session):
                    self.bridge.put('resac_fan_session', binascii.hexlify(os.urandom(16)).decode('ascii'))
            state = parse_fan_state(self.bridge.get('resac_fan_state'))
            state['firmwareVersion'] = ready
            if self.automation is not None:
                state['automatic'] = self.automation.snapshot()
            if self.request is None and (state != self.sent_state or monotonic() - self.sent_at >= 5):
                self.request = CurlUpload(self.config, state, route='fan-state')
                self.sent_state = state
                self.sent_at = monotonic()
        except Exception:
            self.initialized = False
            self.sent_state = None

    def close(self):
        if self.request is not None:
            self.request.close()


class CommandProcessor(object):
    """One 30-second poll or result request alongside the independent poster.

    Require a matching AVR acknowledgment. Journal an uncertain IR result before
    dispatch so a Linux restart never automatically repeats an interrupted toggle.
    """
    def __init__(self, config, bridge=None, state_path=COMMAND_STATE_PATH, on_manual=None, curve_handler=None, automatic=False):
        self.config = config
        self.bridge = bridge or config.get('_bridge') or BridgeStore()
        self.state_path = state_path
        self.request = None
        self.request_kind = None
        self.current = None
        self.reply = None
        self.cached = None
        self.persisted = False
        self.final_journal_ms = 0
        self.retry_at = 0
        self.backoff = 1
        self.bridge_due = 0
        self.ping_dispatched = False
        self.deadline = 0
        self.ir = None
        self.prepared = False
        self.expires_at = None
        self.calibrating = False
        self.session_id = None
        self.target_level = None
        self.on_manual = on_manual
        self.curve_handler = curve_handler
        self.automatic = automatic
        try:
            with open(state_path) as stream:
                saved = decode_json(stream.read())
            if (not isinstance(saved, dict) or not re.match(COMMAND_ID, saved.get('id', '')) or
                    saved.get('status') not in ('succeeded', 'failed') or not isinstance(saved.get('result'), dict)):
                raise ValueError('Invalid saved result')
            self.cached = self.reply = saved
            self.persisted = True
        except IOError as error:
            if error.errno != errno.ENOENT:
                raise ValueError('Saved command result could not be read')
        except Exception:
            # A corrupt journal cannot establish whether a physical action ran.
            raise ValueError('Saved command result could not be read')

    def _status(self, value):
        try:
            self.bridge.put('resac_command_status', value)
        except Exception:
            pass

    def _command_attributes(self, outcome, error_kind=None):
        command_type = self.current.get('type')
        if (not isinstance(command_type, (str, text_type)) or
                not re.match(r'^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$', command_type)):
            command_type = 'unknown'
        attributes = {'command.id': self.current['id'], 'command.type': command_type,
                      'command.source': 'automatic' if self.automatic else 'manual', 'outcome': outcome}
        attributes.update(command_payload_attributes(command_type, self.current.get('payload')))
        if error_kind is not None:
            attributes['error.kind'] = error_kind
        return attributes

    def _complete(self, status, result):
        timing = self.config.get('_timing_trace')
        if timing:
            timing.record('agent.completion_consumed', command=self.current['id'])
        signaled_at = None
        if isinstance(self.bridge, BridgeEvents) and self.bridge.notification:
            signaled_at = self.bridge.notification.get('signaled_at')
        if isinstance(self.bridge, BridgeEvents):
            try:
                self.bridge.cancel_command(self.current['id'])
            except (IOError, OSError):
                log('Command notification could not be cleared; GPIO will be reopened before reuse')
        if (status == 'succeeded' and getattr(self, 'avr_state', None) is not None and self.session_id and
                isinstance(self.bridge, BridgeEvents) and self.bridge.control_session == self.session_id and
                self.bridge.get('resac_fan_session') == self.session_id):
            revision, flags, speed, source = self.avr_state
            self.bridge._update('resac_fan_state', 'v2,%s,%d,%d,%d,%d,%s' %
                                (self.session_id, flags & 1, (flags >> 1) & 1, speed, revision, source))
        parent = self.current.get('traceparent') or traceparent()
        telemetry = self.config.get('_telemetry')
        if telemetry:
            attributes = self._command_attributes(status, 'avr_timeout' if 'timed out' in result.get('error', '') else 'command_rejected' if status == 'failed' else 'none')
            attributes.update(self.phase_timings)
            if signaled_at is not None:
                attributes['command.timing.roundtrip_ms'] = max(0, (monotonic() - signaled_at) * 1000)
            if getattr(self, 'avr_phases', None) is not None:
                attributes['command.timing.fetch_ms'] = self.avr_phases[0] * 1000
                attributes['command.timing.safety_ms'] = self.avr_phases[1] * 1000
            # Carry compact fields on the existing event. The backend expands
            # phases so bursts do not multiply the board's bounded event queue.
            telemetry.event('command.execute', parent, monotonic() - self.execution_started, attributes)
            if getattr(self, 'avr_phases', None) is not None:
                telemetry.event('avr.execute', parent, self.avr_phases[2], self._command_attributes(status))
            try:
                fields = (self.bridge.get('resac_command_diagnostic') or '').split(',')
                if (getattr(self, 'avr_phases', None) is None and len(fields) == 4 and fields[:2] == ['v1', self.current['id']] and
                        re.match(r'^\d{1,10}$', fields[2]) and fields[3] in ('ok', 'failed')):
                    telemetry.event('avr.execute', parent, int(fields[2]) / 1000.0,
                                    self._command_attributes(fields[3]))
            except Exception:
                pass
        self.reply = dict(id=self.current['id'], status=status, result=result, traceparent=parent)
        self.current = None
        self.persisted = False

    def _write_state(self, value):
        # The enclosing /etc/res-ac directory is private. IR writes before and
        # after dispatch; sensor readings and empty long polls never write here.
        path = self.state_path + '.tmp'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encode_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(path, self.state_path)
        # Make the rename durable as well as the file contents.
        directory = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _save(self):
        started = monotonic()
        self._write_state(self.reply)
        self.final_journal_ms = max(0, (monotonic() - started) * 1000)
        self.cached = self.reply
        self.persisted = True
        try:
            self.bridge.put('resac_command', '')
        except Exception:
            pass

    def _accept(self, command, started_at=None):
        if not isinstance(command, dict) or not re.match(COMMAND_ID, command.get('id', '')):
            raise ValueError('Invalid command ID')
        self.backoff = 1
        self.final_journal_ms = 0
        if self.cached and self.cached['id'] == command['id']:
            self.reply = self.cached
            self.persisted = True
            return
        self.current = command
        self.avr_phases = None
        self.avr_state = None
        self.phase_timings = {}
        self.execution_started = monotonic() if started_at is None else started_at
        self.bridge_failed_reported = False
        if not re.match(TRACEPARENT, command.get('traceparent', '')):
            command['traceparent'] = traceparent()
            if self.config.get('_telemetry'):
                self.config['_telemetry'].event('command.local', command['traceparent'], attributes=self._command_attributes('ok'))
        self.ir = None
        self.prepared = False
        self.expires_at = None
        self.calibrating = command.get('type') == 'fan.calibrate'
        self.session_id = None
        self.target_level = None
        if command.get('type') in ('fan.command', 'ir.send', 'fan.calibrate', 'fan.level', 'fan.curve'):
            try:
                if self.calibrating:
                    payload = command.get('payload')
                    if (not isinstance(payload, dict) or payload.get('confirmOff') is not True or
                            set(payload) != set(['confirmOff', 'sessionId'])):
                        raise ValueError('Confirm the fan is off with saved speed 1')
                elif command['type'] == 'fan.level':
                    payload = command.get('payload')
                    if (not isinstance(payload, dict) or set(payload) != set(['level', 'sessionId']) or
                            isinstance(payload.get('level'), bool) or not isinstance(payload.get('level'), integer_types) or
                            not 0 <= payload['level'] <= 5):
                        raise ValueError('Fan level must be an integer from 0 (off) to 5')
                    self.target_level = payload['level']
                elif command['type'] == 'fan.curve':
                    validate_curve(command.get('payload'))
                else:
                    self.ir = ir_payload(command)
                self.expires_at = command_expiry(command)
                if time.gmtime().tm_year < 2025 or time.time() >= self.expires_at:
                    raise ValueError('IR command expired or device clock invalid')
                if (isinstance(self.bridge, BridgeEvents) and
                        self.bridge.get('resac_command_protocol') not in (None, '', 'batched-v1', 'batched-v2')):
                    raise ValueError('Unsupported command safety protocol')
                ready = self.bridge.get('resac_command_ready')
                if ready not in ('v3', 'v4'):
                    raise ValueError('Arduino firmware does not support calibration; upgrade sketch')
                if command['type'] in ('fan.level', 'fan.curve') and ready != 'v4':
                    raise ValueError('Upgrade firmware for target levels and automatic curves')
                self.session_id = command['payload'].get('sessionId')
                if (isinstance(self.bridge, BridgeEvents) and self.bridge.get('resac_command_protocol') in ('batched-v1', 'batched-v2') and
                        (not self.bridge.control_confirmed or self.session_id != self.bridge.control_session)):
                    raise ValueError('Fan control epoch changed; wait for fresh status and recalibrate')
                state = parse_fan_state(self.bridge.get('resac_fan_state'))
                if (not isinstance(self.session_id, (str, text_type)) or
                        not re.match(r'^[a-f0-9]{32}$', self.session_id) or state['sessionId'] != self.session_id):
                    raise ValueError('Fan session changed; set speed to 1, turn fan off and recalibrate')
                disabling = command['type'] == 'fan.curve' and command['payload']['enabled'] is False
                if not self.calibrating and not disabling and not state['calibrated']:
                    raise ValueError('Calibration required: set speed to 1, then turn fan off')
                if self.ir and self.ir[0] == FAN_ADDRESS and self.ir[1] in (0x16, 0x14) and state['power'] != 'on':
                    raise ValueError('Turn the fan on before changing speed')
                if command['type'] == 'fan.curve':
                    if self.curve_handler is None:
                        raise ValueError('Automatic controller unavailable')
                    saved = self.curve_handler(command['payload'])
                    self._complete('succeeded', dict(reply='curve_saved', automatic=saved))
                    return
                if self.on_manual is not None and not self.calibrating:
                    self.on_manual()
            except (ValueError, IOError, OSError) as error:
                self._complete('failed', dict(error=str(error)))
                return
        elif command.get('type') != 'device.ping':
            self._complete('failed', dict(error='Unsupported command type'))
            return
        self.deadline = monotonic() + 10
        self.bridge_due = 0
        self.ping_dispatched = False
        if not isinstance(self.bridge, BridgeEvents):
            self._status('command received; waiting for Arduino')

    def _dispatch(self, frame):
        started = monotonic()
        if isinstance(self.bridge, BridgeEvents):
            timings = self.phase_timings
            def signaled(elapsed):
                timings['command.timing.notify_ms'] = max(0, elapsed * 1000)
                self._status('command sent; waiting for Arduino')
            self.bridge.dispatch_command(frame, self.deadline, on_signal=signaled)
        else:
            self.bridge.put('resac_command', frame)
        telemetry = self.config.get('_telemetry')
        if telemetry:
            telemetry.event('bridge.dispatch', self.current.get('traceparent'), monotonic() - started,
                            self._command_attributes('ok'))

    def _arduino(self):
        if monotonic() >= self.deadline:
            if (self.ir or self.target_level is not None) and self.prepared:
                try:
                    if isinstance(self.bridge, BridgeEvents):
                        self.bridge.invalidate_fan()
                    else:
                        self.bridge.put('resac_fan_invalidate', '1')
                except Exception:
                    pass
            self._complete('failed', dict(error='Arduino IR timed out; transmission uncertain' if self.ir or self.target_level is not None else
                                         'Arduino calibration timed out; confirm off again' if self.calibrating else 'Arduino ping timed out'))
            return
        streaming = isinstance(self.bridge, BridgeEvents)
        if not streaming and monotonic() < self.bridge_due:
            return
        self.bridge_due = monotonic() + 0.5
        try:
            frame = self.bridge.get('resac_command_result') or ''
            if isinstance(self.bridge, BridgeEvents) and self.bridge.get('resac_command_protocol') in ('batched-v1', 'batched-v2'):
                modern = self.bridge.get('resac_command_protocol') == 'batched-v2'
                envelope = frame.split(',', 8 if modern else 4)
                valid = (len(envelope) == (9 if modern else 5) and envelope[0] == ('v6' if modern else 'v5') and
                         all(re.match(r'^\d{1,10}$', item) and int(item) <= 0xffffffff for item in envelope[1:4]))
                self.avr_phases = self.avr_state = None
                if valid and modern:
                    valid = (re.match(r'^\d{1,10}$', envelope[4]) and int(envelope[4]) <= 0xffffffff and
                             envelope[5] in ('0', '1', '3') and envelope[6] in ('0', '1', '2', '3', '4', '5') and
                             envelope[7] in ('r', 'c', 'm', 'a') and
                             ((envelope[5] == '3' and envelope[6] != '0') or
                              (envelope[5] != '3' and envelope[6] == '0')))
                if valid and envelope[-1].split(',')[1:2] == [self.current['id']]:
                    self.avr_phases = tuple(int(item) / 1000.0 for item in envelope[1:4])
                    if modern:
                        self.avr_state = (int(envelope[4]), int(envelope[5]), int(envelope[6]),
                                          dict(r='reset', c='calibration', m='manual', a='automatic')[envelope[7]])
                    frame = envelope[-1]
                else:
                    frame = ''
            fields = frame.split(',')
            if (not self.ir and self.target_level is None and not self.calibrating and len(fields) == 4 and fields[:3] == ['v1', self.current['id'], 'pong'] and
                    re.match(r'^\d{1,10}$', fields[3]) and int(fields[3]) <= 0xffffffff):
                self._complete('succeeded', dict(reply='pong', uptimeMs=int(fields[3])))
                return
            if self.ir or self.calibrating or self.target_level is not None:
                if fields == ['v3', self.current['id'], 'rejected', self.session_id]:
                    self._complete('failed', dict(error='Fan command rejected; check power, session and calibration (off with saved speed 1)'))
                    return
                if self.calibrating and fields == ['v3', self.current['id'], 'calibrated', self.session_id]:
                    self._complete('succeeded', dict(reply='calibrated', sessionId=self.session_id, fanStateConfirmed=False))
                    return
                if (self.target_level is not None and len(fields) == 7 and
                        fields[:5] == ['v4', self.current['id'], 'level_set', self.session_id, str(self.target_level)] and
                        re.match(r'^[0-5]$', fields[5]) and re.match(r'^\d{1,10}$', fields[6]) and int(fields[6]) <= 0xffffffff):
                    self._complete('succeeded', dict(reply='level_set', level=self.target_level, presses=int(fields[5]),
                                                   uptimeMs=int(fields[6]), fanStateConfirmed=False))
                    return
                expected = ['v3', self.current['id'], 'ir_sent', self.session_id] + [str(v) for v in (self.ir or ())]
                if (self.ir and len(fields) == 8 and fields[:7] == expected and
                        re.match(r'^\d{1,10}$', fields[7]) and int(fields[7]) <= 0xffffffff):
                    address, command, repeats = self.ir
                    self._complete('succeeded', dict(reply='ir_sent', protocol='NEC', address=address,
                                                   command=command, repeats=repeats, uptimeMs=int(fields[7]),
                                                   fanStateConfirmed=False))
                    return
                if not self.prepared:
                    if time.time() >= self.expires_at:
                        self._complete('failed', dict(error='IR command expired before dispatch'))
                        return
                    journal_started = monotonic()
                    self._write_state(dict(id=self.current['id'], traceparent=self.current.get('traceparent'), status='failed', result=dict(
                        error='IR execution interrupted; transmission uncertain; verify fan before retrying')))
                    self.phase_timings['command.timing.journal_ms'] = max(0, (monotonic() - journal_started) * 1000)
                    self.prepared = True
                    if self.calibrating:
                        self._dispatch('v3,%s,calibrate,%s' % (self.current['id'], self.session_id))
                    elif self.target_level is not None:
                        self._dispatch('v4,%s,%s,%s,%d' % (self.current['id'],
                                        'auto_level' if self.automatic else 'level', self.session_id, self.target_level))
                    else:
                        self._dispatch('v3,%s,nec,%s,%d,%d,%d' % ((self.current['id'], self.session_id) + self.ir))
                # Do not rewrite the IR mailbox: AVR reset clears it, preventing
                # a reboot from replaying a toggle whose acknowledgment was lost.
            elif not streaming or not self.ping_dispatched:
                self._dispatch('v1,%s,ping' % self.current['id'])
                self.ping_dispatched = True
        except Exception as error:
            telemetry = self.config.get('_telemetry')
            if telemetry and self.current and not self.bridge_failed_reported:
                telemetry.event('bridge.error', self.current.get('traceparent'),
                                attributes=self._command_attributes('failed', type(error).__name__))
                self.bridge_failed_reported = True
            # Wait for Bridge/AVR within the bounded execution timeout.

    def _failed_request(self, error):
        self.retry_at = monotonic() + self.backoff
        detail = str(error) if isinstance(error, UploadError) else type(error).__name__
        log('Command request failed (%s); retrying in %ds' % (detail, self.backoff))
        self.backoff = min(self.backoff * 2, 60)

    def tick(self, allow_execution=True):
        if self.request is not None and request_completed(self.request):
            try:
                status, body = self.request.finish()
                if self.request_kind == 'result':
                    if status not in (200, 404, 409):
                        raise UploadError(status=status)
                    self.reply = None
                    self.backoff = 1
                    if status == 200:
                        log('Command result accepted by backend')
                        self._status('result accepted HTTP=200')
                    else:
                        log('Command result no longer accepted (HTTP %d)' % status)
                        self._status('result rejected HTTP=%d' % status)
                elif status == 200:
                    accepted_at = monotonic()
                    value = decode_json(body.decode('utf-8'))
                    commands = value.get('commands')
                    if not isinstance(commands, list) or len(commands) != 1:
                        raise ValueError('Expected one command')
                    self._accept(commands[0], started_at=accepted_at)
                elif status == 204:
                    self.backoff = 1
                else:
                    raise UploadError(status=status)
            except Exception as error:
                self._failed_request(error)
            finally:
                self.request.close()
                self.request = None
        if self.current is not None and allow_execution:
            self._arduino()
        if self.request is not None or self.current is not None or monotonic() < self.retry_at or not allow_execution:
            return
        try:
            if self.reply is not None:
                if not self.persisted:
                    self._save()
                payload = dict(status=self.reply['status'], result=self.reply['result'], traceparent=self.reply.get('traceparent') or traceparent())
                self.request = CurlUpload(self.config, payload,
                                          route='commands/%s/result' % self.reply['id'], journal_ms=self.final_journal_ms)
                self.request_kind = 'result'
            else:
                if self.bridge.get('resac_command_ready') not in ('v1', 'v2', 'v3', 'v4'):
                    self.retry_at = monotonic() + 2
                    return
                # Server waits 30 seconds; leave 5 seconds for transport overhead.
                self.request = CurlUpload(self.config, route='commands', method='GET', timeout=35)
                self.request_kind = 'poll'
        except Exception as error:
            self._failed_request(error)

    def close(self):
        if self.request is not None:
            self.request.close()


class AutoController(object):
    """Local step curve; fresh samples, 30-second dwell and one physical mailbox."""
    def __init__(self, config, bridge=None, path=CURVE_PATH, state_path=AUTO_STATE_PATH):
        self.config = config
        self.bridge = bridge or config.get('_bridge') or BridgeStore()
        self.path, self.state_path = path, state_path
        self.settings = dict(enabled=False, thresholdsC=[22, 24, 26, 28, 30], hysteresisC=1)
        self.status = 'disabled'
        self.target = None
        self.runner = None
        self.next_due = 0
        self.last_adjusted = None
        self.last_frame = None
        self.sample_at = None
        self.temperature = None
        self.last_revision = None
        self.interval_auto = None
        self.bridge_version = None
        try:
            with open(path) as stream:
                self.settings = validate_curve(decode_json(stream.read()))
            self.status = 'waiting_for_sample' if self.settings['enabled'] else 'disabled'
        except IOError as error:
            if error.errno != errno.ENOENT:
                self.status = 'config_error'
        except Exception:
            self.status = 'config_error'

    @property
    def busy(self):
        return self.runner is not None

    def snapshot(self):
        return dict(self.settings, thresholdsC=list(self.settings['thresholdsC']),
                    status=self.status, targetLevel=self.target, minAdjustmentSeconds=30)

    def configure(self, payload):
        settings = validate_curve(payload)
        fd = os.open(self.path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encode_json(settings)); stream.flush(); os.fsync(stream.fileno())
        os.rename(self.path + '.tmp', self.path)
        directory = os.open(os.path.dirname(self.path) or '.', os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self.settings = settings
        self.target = None
        self.last_adjusted = None
        self.status = 'waiting_for_sample' if settings['enabled'] else 'disabled'
        try:
            state = parse_fan_state(self.bridge.get('resac_fan_state'))
            self.last_revision = (state['sessionId'], state['revision'])
        except Exception:
            pass
        return dict(settings)

    def manual_override(self):
        if self.settings['enabled']:
            self.configure(dict(self.settings, enabled=False))
            self.status = 'manual_override'

    def tick(self, commands):
        if self.runner is not None:
            if self.runner.current is not None:
                self.runner._arduino()
            if self.runner.current is None:
                self.runner._save()
                failed = self.runner.reply['status'] != 'succeeded'
                self.runner = None
                self.last_adjusted = monotonic()
                if failed:
                    self.configure(dict(self.settings, enabled=False))
                    self.status = 'command_error'
                elif self.settings['enabled']:
                    self.status = 'active'
            return
        streaming = isinstance(self.bridge, BridgeEvents)
        if streaming:
            version = self.bridge.version('resac_fan_state', 'resac_sample', 'resac_sensor_status')
            changed = version != self.bridge_version
            if self.bridge_version is not None and version[0] != self.bridge_version[0]:
                self.sample_at = self.temperature = None
                self.interval_auto = None
            self.bridge_version = version
        else:
            changed = False
        if not changed and monotonic() < self.next_due:
            return
        self.next_due = monotonic() + 1
        try:
            state = parse_fan_state(self.bridge.get('resac_fan_state'))
            revision = (state['sessionId'], state['revision'])
            if self.last_revision is not None and revision != self.last_revision and state.get('controlSource') == 'manual':
                self.manual_override()
            self.last_revision = revision
            if self.interval_auto != self.settings['enabled']:
                interval = self.config.get('interval_seconds', 10)
                self.bridge.put('resac_interval_ms', str(int((min(interval, 10) if self.settings['enabled'] else interval) * 1000)))
                self.interval_auto = self.settings['enabled']
            frame = self.bridge.get('resac_sample')
            sensor_ok = self.bridge.get('resac_sensor_status') == 'ok'
            if frame and frame != self.last_frame:
                sample = parse_sample(frame)
                # A retained frame on startup is not evidence of a fresh sample.
                fresh = self.bridge.sample_live if streaming else self.last_frame is not None
                if fresh and sensor_ok:
                    self.temperature = sample['temperatureC']
                    self.sample_at = self.bridge.sample_received if streaming else monotonic()
                self.last_frame = frame
            if not self.settings['enabled']:
                return
            if not state['calibrated']:
                self.target = None; self.status = 'calibration_required'; return
            if not sensor_ok or self.sample_at is None or monotonic() - self.sample_at > 30:
                self.status = 'sensor_unavailable' if self.sample_at is not None else 'waiting_for_sample'; return
            self.target = curve_target(self.temperature, self.settings['thresholdsC'], self.settings['hysteresisC'], self.target)
            if commands.current is not None or (commands.reply is not None and not getattr(commands, 'persisted', True)):
                self.status = 'waiting_for_device'; return
            if state['speed'] == self.target:
                self.status = 'active'; return
            if self.last_adjusted is not None and monotonic() - self.last_adjusted < 30:
                self.status = 'cooldown'; return
            if self.bridge.get('resac_command_ready') != 'v4':
                self.status = 'waiting_for_device'; return
            raw = binascii.hexlify(os.urandom(16)).decode('ascii')
            command_id = '%s-%s-%s-%s-%s' % (raw[:8], raw[8:12], raw[12:16], raw[16:20], raw[20:])
            runner = CommandProcessor(self.config, self.bridge, self.state_path, automatic=True)
            runner._accept(dict(id=command_id, type='fan.level', payload=dict(level=self.target, sessionId=state['sessionId']),
                                expiresAt=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()+30))))
            self.runner = runner
            self.status = 'adjusting'
        except Exception:
            if self.settings['enabled']:
                self.status = 'waiting_for_device'


def next_wakeup(bridge, telemetry, commands, automation, fan_reporter, pending, upload, retry_at):
    """Wait until real scheduled work; HTTP completions and Bridge changes wake IO."""
    now = monotonic()
    deadlines = []
    def due(value):
        if value is not None:
            deadlines.append(value)
    due(getattr(fan_reporter, 'next_due', None))  # AVR watchdog heartbeat.
    runner = getattr(automation, 'runner', None)
    if runner is None:
        due(getattr(automation, 'next_due', None))  # Freshness and adjustment dwell.
    else:
        current = getattr(runner, 'current', None)
        if current is None:
            due(now)  # Persist a just-completed local command.
        else:
            due(runner.deadline)
            if getattr(bridge, 'ready', False) and not runner.prepared:
                due(now)  # Dispatch a newly accepted automatic command.
    current = getattr(commands, 'current', None)
    if current is not None:
        due(commands.deadline)
    elif getattr(commands, 'request', None) is None and not automation.busy:
        due(getattr(commands, 'retry_at', None))
    if getattr(telemetry, 'request', None) is None:
        due(getattr(telemetry, 'next_due', None))
    if upload is None and pending.first() is not None:
        due(retry_at)
    if getattr(bridge, 'connection', None) is None:
        due(getattr(bridge, 'retry_at', None))
    elif not bridge.ready:
        due(bridge.deadline)
    notification = getattr(bridge, 'notification', None)
    if notification is not None:
        due(notification['deadline'])
    return max(0, min(deadlines or [now + 60]) - now)


def run(config):
    native = config.get('native_bridge', False)
    bridge = config.get('_bridge') or BridgeEvents(port=5701 if native else 5700, native=native,
                                                   command_gpio=CommandGPIO(), timing=config.get('_timing_trace'))
    config['_bridge'] = bridge
    http = config.get('_http') or HttpTransport(config)
    config['_http'] = http
    telemetry = Telemetry(config)
    config['_telemetry'] = telemetry
    pending = Buffer(config.get('queue_limit', 100), telemetry)
    collector = Collector(config, pending)
    automation = AutoController(config)
    commands = CommandProcessor(config, on_manual=automation.manual_override, curve_handler=automation.configure)
    fan_reporter = FanReporter(config, automation=automation)
    backoff = 1
    retry_at = 0
    upload = None
    sample = None
    try:
        while True:
            timing = config.get('_timing_trace')
            if timing and timing.dump_requested and commands.current is None and not automation.busy:
                timing.write(config['_timing_path'])
            http.wait(bridge, 0)
            collector.tick()
            commands.tick(allow_execution=not automation.busy)
            automation.tick(commands)
            if upload is not None and request_completed(upload):
                try:
                    status, response = upload.finish()
                    if status not in (200, 201):
                        raise UploadError(status=status)
                    pending.remove(sample)
                    backoff = 1
                    collector.report_upload('ok sample=%s HTTP=%d' % (sample['readingId'].rsplit('-', 1)[-1], status))
                    log('Reading uploaded; buffered=%d; dropped=%d' % (len(pending.queue), pending.dropped))
                except UploadError as error:
                    collector.report_upload('HTTP=%d curl=%d' % (error.status, error.curl_code))
                    retry_at = monotonic() + backoff
                    log('Upload failed (%s); retrying in %ds' % (error, backoff))
                    backoff = min(backoff * 2, 60)
                finally:
                    upload.close()
                    upload = None
            if upload is None and monotonic() >= retry_at:
                sample = pending.first()
                if sample is not None:
                    queued_at = sample.pop('_queued_at', None)
                    if queued_at is not None:
                        telemetry.event('reading.buffer', sample.get('traceparent'), monotonic() - queued_at,
                                        {'reading.id': sample['readingId'], 'outcome': 'ok'})
                    try:
                        upload = CurlUpload(config, sample)
                    except (UploadError, IOError, OSError):
                        retry_at = monotonic() + backoff
                        backoff = min(backoff * 2, 60)
                        log('Reading request setup failed; retaining sample for retry')
            fan_reporter.tick()
            telemetry.tick(pending)
            http.wait(bridge, next_wakeup(bridge, telemetry, commands, automation,
                                          fan_reporter, pending, upload, retry_at))
    finally:
        telemetry.close()
        commands.close()
        fan_reporter.close()
        if upload is not None:
            upload.close()
        bridge.close()
        http.close()
        timing = config.get('_timing_trace')
        if timing and not timing.exported:
            timing.request_dump()
            timing.write(config['_timing_path'])


def main():
    if '--gpio-preflight' in sys.argv:
        CommandGPIO().preflight()
        print('PASS: GPIO notification prerequisites; no GPIO outputs changed')
        return 0
    path = CONFIG_PATH
    if '--config' in sys.argv:
        path = sys.argv[sys.argv.index('--config') + 1]
    if '--help' in sys.argv:
        print(__doc__)
        return 0
    config = load_config(path)
    if '--timing-trace' in sys.argv:
        from timing_trace import TimingTrace
        output = sys.argv[sys.argv.index('--timing-trace') + 1]
        TimingTrace.validate_output(output)
        timing = TimingTrace()
        config['_timing_trace'], config['_timing_path'] = timing, output
        signal.signal(signal.SIGUSR1, lambda *_: timing.request_dump())
    if '--probe' in sys.argv:
        status, response = post(config, {})
        # An authenticated, intentionally invalid sample verifies the route without
        # polluting the readings table. A wrong token produces 401 instead.
        decoded = decode_json(response.decode('utf-8'))
        if status == 400 and decoded.get('error') == 'Invalid temperatureC':
            print('PASS: verified HTTPS, trusted certificate, and device authentication; no test reading inserted')
            return 0
        print('FAIL: backend probe returned HTTP %d; check private configuration' % status)
        return 1
    run(config)
    return 0


def shutdown(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, shutdown)
    os.umask(0o077)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
    except UploadError as error:
        print('HTTPS probe failed (%s)' % error)
        sys.exit(1)
    except Exception as error:
        # Do not print exceptions that might include private configuration/URL/token.
        print('Agent failed: ' + type(error).__name__)
        sys.exit(1)
