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
import signal
import socket
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

# SC15T captures: https://randomblock1.com/blog/smartifying-dumb-fan
# Treat as a starting profile, pending verification on PCF-SC15T-CT.
FAN_ADDRESS = 0xDE80
FAN_BUTTONS = dict(power=0x00, speed_up=0x08, speed_down=0x10, breeze=0x09,
                   horizontal=0x15, vertical=0x16, timer_1h=0x12,
                   timer_2h=0x0e, timer_4h=0x0a)


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
    if isinstance(hysteresis, bool) or not isinstance(hysteresis, (int, float)) or not 0.5 <= hysteresis <= 5:
        raise ValueError('Hysteresis must be 0.5 to 5 C')
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
        self.bridge = bridge or BridgeStore()
        self.session = binascii.hexlify(os.urandom(16)).decode('ascii')
        self.events = []
        self.dropped = 0
        self.request = None
        self.batch = None
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

    def sensor(self, parent=None, sample_id=None):
        try:
            fields = (self.bridge.get('resac_sensor_diagnostic') or '').split(',')
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
            self.next_due = monotonic() + self.backoff
            self.backoff = min(self.backoff * 2, 60)

    def _tick(self, pending):
        if self.request is not None:
            if self.request.process.poll() is None:
                return
            try:
                status, unused = self.request.finish()
                if status != 200:
                    raise UploadError(status=status)
                self.batch = None
                self.backoff = 5
                self.next_due = monotonic() + 15
            finally:
                self.request.close()
                self.request = None
        if monotonic() < self.next_due:
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
        self.request = CurlUpload(self.config, payload, route='telemetry', timeout=10)

    def close(self):
        if self.request is not None:
            self.request.close()


class CurlUpload(object):
    """A bounded curl subprocess; polling completion never waits for the network."""
    def __init__(self, config, payload=None, route='readings', method='POST', timeout=25):
        self.process = None
        self.files = []
        self.telemetry = config.get('_telemetry') if route != 'telemetry' else None
        self.started = monotonic()
        self.reported = False
        self.kind = 'result' if route.endswith('/result') else route
        supplied = (payload or {}).get('traceparent')
        self.root_request = not (isinstance(supplied, (str, text_type)) and re.match(TRACEPARENT, supplied))
        self.parent = traceparent() if self.root_request else supplied
        # Allocate a fresh CLIENT span for each attempt, before writing headers.
        # Keep the collection/command context as its parent across retries.
        self.request_traceparent = self.parent if self.root_request else traceparent(self.parent)
        try:
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
                         '--output', self.response_path, '--write-out', '%{http_code}']
            if payload is not None:
                payload_fd, payload_path = temporary('res-ac-payload-')
                self.files.append(payload_path)
                with os.fdopen(payload_fd, 'w') as stream:
                    stream.write(encode_json(payload))
                arguments += ['--data-binary', '@' + payload_path]
            self.process = subprocess.Popen(arguments,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Read request bodies from private RAM files: a large diagnostic batch
            # must not block the control loop while writing a subprocess pipe.
            self.process.stdin.close()
            self.process.stdin = None
        except BaseException:
            self._report(0, 0)
            self.close()
            raise

    def _report(self, status, code):
        if self.reported or self.telemetry is None:
            return
        self.reported = True
        outcome = ('ok' if 200 <= status < 300 else 'failed') if not code else {6: 'dns', 7: 'connect', 28: 'timeout', 35: 'tls', 60: 'tls'}.get(code, 'other')
        self.telemetry.event('http.request' if self.root_request else 'http.device', self.parent, monotonic() - self.started,
                             {'outcome': outcome, 'curl.code': code, 'http.status_code': status, 'request.kind': self.kind}, span_id=self.request_traceparent[36:52])

    def finish(self):
        output, errors = self.process.communicate()
        if self.process.returncode:
            self._report(0, self.process.returncode)
            raise UploadError(curl_code=self.process.returncode)
        try:
            status = int(output.strip())
        except ValueError:
            self._report(0, 0)
            raise UploadError()
        self._report(status, 0)
        with open(self.response_path, 'rb') as stream:
            return status, stream.read(32768)

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.communicate()
        for path in self.files:
            if os.path.exists(path):
                os.unlink(path)


def post(config, payload):
    upload = CurlUpload(config, payload)
    try:
        return upload.finish()
    finally:
        upload.close()


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
        self.bridge = bridge or BridgeStore()
        self.initialized = False
        self.last_status = None
        self.next_due = 0

    def tick(self):
        if monotonic() < self.next_due:
            return
        self.next_due = monotonic() + 0.5
        started = monotonic()
        try:
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
            frame = self.bridge.get('resac_sample')
            if frame:
                sample = parse_sample(frame)
                if sample['readingId'] != self.pending.last_id:
                    sample['traceparent'] = traceparent()
                    telemetry = self.config.get('_telemetry')
                    if telemetry:
                        telemetry.last_sample_at = monotonic()
                        telemetry.event('reading.collect', sample['traceparent'], attributes={'outcome': 'ok', 'reading.id': sample['readingId']})
                        telemetry.sensor(sample['traceparent'], sample['readingId'])
                        telemetry.event('bridge.collect', sample['traceparent'], monotonic() - started, attributes={'outcome': 'ok', 'reading.id': sample['readingId']})
                if time.gmtime().tm_year >= 2025:
                    sample['recordedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                self.pending.add(sample)
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
        self.bridge = bridge or BridgeStore()
        self.request = None
        self.initialized = False
        self.next_due = 0
        self.sent_at = 0
        self.sent_state = None
        self.heartbeat = 0
        self.automation = automation

    def tick(self):
        if self.request is not None and self.request.process.poll() is not None:
            try:
                status, body = self.request.finish()
                if status not in (200, 201):
                    self.sent_state = None
            except Exception:
                self.sent_state = None
            finally:
                self.request.close()
                self.request = None
        if monotonic() < self.next_due:
            return
        self.next_due = monotonic() + 1
        try:
            ready = self.bridge.get('resac_command_ready')
            if ready not in ('v3', 'v4'):
                self.initialized = False
                return
            if not self.initialized:
                self.bridge.put('resac_fan_invalidate', '1')
                self.initialized = True
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
        self.bridge = bridge or BridgeStore()
        self.state_path = state_path
        self.request = None
        self.request_kind = None
        self.current = None
        self.reply = None
        self.cached = None
        self.persisted = False
        self.retry_at = 0
        self.backoff = 1
        self.bridge_due = 0
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

    def _complete(self, status, result):
        parent = self.current.get('traceparent') or traceparent()
        telemetry = self.config.get('_telemetry')
        if telemetry:
            telemetry.event('command.execute', parent, monotonic() - self.execution_started,
                            {'command.id': self.current['id'], 'outcome': status,
                             'error.kind': 'avr_timeout' if 'timed out' in result.get('error', '') else 'command_rejected' if status == 'failed' else 'none'})
            try:
                fields = (self.bridge.get('resac_command_diagnostic') or '').split(',')
                if (len(fields) == 4 and fields[:2] == ['v1', self.current['id']] and
                        re.match(r'^\d{1,10}$', fields[2]) and fields[3] in ('ok', 'failed')):
                    telemetry.event('avr.execute', parent, int(fields[2]) / 1000.0,
                                    {'command.id': self.current['id'], 'outcome': fields[3]})
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
        self._write_state(self.reply)
        self.cached = self.reply
        self.persisted = True
        try:
            self.bridge.put('resac_command', '')
        except Exception:
            pass

    def _accept(self, command):
        if not isinstance(command, dict) or not re.match(COMMAND_ID, command.get('id', '')):
            raise ValueError('Invalid command ID')
        self.backoff = 1
        if self.cached and self.cached['id'] == command['id']:
            self.reply = self.cached
            self.persisted = True
            return
        self.current = command
        self.execution_started = monotonic()
        self.bridge_failed_reported = False
        if not re.match(TRACEPARENT, command.get('traceparent', '')):
            command['traceparent'] = traceparent()
            if self.config.get('_telemetry'):
                self.config['_telemetry'].event('command.local', command['traceparent'], attributes={'command.id': command['id'], 'outcome': 'ok'})
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
                        raise ValueError('Confirm the fan was restarted and left off')
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
                ready = self.bridge.get('resac_command_ready')
                if ready not in ('v3', 'v4'):
                    raise ValueError('Arduino firmware does not support calibration; upgrade sketch')
                if command['type'] in ('fan.level', 'fan.curve') and ready != 'v4':
                    raise ValueError('Upgrade firmware for cyclic levels and automatic curves')
                self.session_id = command['payload'].get('sessionId')
                state = parse_fan_state(self.bridge.get('resac_fan_state'))
                if (not isinstance(self.session_id, (str, text_type)) or
                        not re.match(r'^[a-f0-9]{32}$', self.session_id) or state['sessionId'] != self.session_id):
                    raise ValueError('Fan session changed; restart fan to factory position: off and recalibrate')
                disabling = command['type'] == 'fan.curve' and command['payload']['enabled'] is False
                if not self.calibrating and not disabling and not state['calibrated']:
                    raise ValueError('Calibration required: restart fan to factory position: off')
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
        self._status('command received; waiting for Arduino')

    def _dispatch(self, frame):
        started = monotonic()
        self.bridge.put('resac_command', frame)
        telemetry = self.config.get('_telemetry')
        if telemetry:
            telemetry.event('bridge.dispatch', self.current.get('traceparent'), monotonic() - started,
                            {'command.id': self.current['id'], 'outcome': 'ok'})

    def _arduino(self):
        if monotonic() >= self.deadline:
            if (self.ir or self.target_level is not None) and self.prepared:
                try:
                    self.bridge.put('resac_fan_invalidate', '1')
                except Exception:
                    pass
            self._complete('failed', dict(error='Arduino IR timed out; transmission uncertain' if self.ir or self.target_level is not None else
                                         'Arduino calibration timed out; confirm off again' if self.calibrating else 'Arduino ping timed out'))
            return
        if monotonic() < self.bridge_due:
            return
        self.bridge_due = monotonic() + 0.5
        try:
            frame = self.bridge.get('resac_command_result') or ''
            fields = frame.split(',')
            if (not self.ir and self.target_level is None and not self.calibrating and len(fields) == 4 and fields[:3] == ['v1', self.current['id'], 'pong'] and
                    re.match(r'^\d{1,10}$', fields[3]) and int(fields[3]) <= 0xffffffff):
                self._complete('succeeded', dict(reply='pong', uptimeMs=int(fields[3])))
                return
            if self.ir or self.calibrating or self.target_level is not None:
                if fields == ['v3', self.current['id'], 'rejected', self.session_id]:
                    self._complete('failed', dict(error='Calibration required or session changed; restart fan to factory position: off'))
                    return
                if self.calibrating and fields == ['v3', self.current['id'], 'calibrated', self.session_id]:
                    self._complete('succeeded', dict(reply='calibrated', sessionId=self.session_id, fanStateConfirmed=False))
                    return
                if (self.target_level is not None and len(fields) == 7 and
                        fields[:5] == ['v4', self.current['id'], 'level_set', self.session_id, str(self.target_level)] and
                        re.match(r'^[0-3]$', fields[5]) and re.match(r'^\d{1,10}$', fields[6]) and int(fields[6]) <= 0xffffffff):
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
                    self._write_state(dict(id=self.current['id'], traceparent=self.current.get('traceparent'), status='failed', result=dict(
                        error='IR execution interrupted; transmission uncertain; verify fan before retrying')))
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
            else:
                self._dispatch('v1,%s,ping' % self.current['id'])
        except Exception as error:
            telemetry = self.config.get('_telemetry')
            if telemetry and self.current and not self.bridge_failed_reported:
                telemetry.event('bridge.error', self.current.get('traceparent'), attributes={
                    'command.id': self.current['id'], 'outcome': 'failed', 'error.kind': type(error).__name__})
                self.bridge_failed_reported = True
            # Wait for Bridge/AVR within the bounded execution timeout.

    def _failed_request(self, error):
        self.retry_at = monotonic() + self.backoff
        detail = str(error) if isinstance(error, UploadError) else type(error).__name__
        log('Command request failed (%s); retrying in %ds' % (detail, self.backoff))
        self.backoff = min(self.backoff * 2, 60)

    def tick(self, allow_execution=True):
        if self.request is not None and self.request.process.poll() is not None:
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
                    value = decode_json(body.decode('utf-8'))
                    commands = value.get('commands')
                    if not isinstance(commands, list) or len(commands) != 1:
                        raise ValueError('Expected one command')
                    self._accept(commands[0])
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
                                          route='commands/%s/result' % self.reply['id'])
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
        self.bridge = bridge or BridgeStore()
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
        return dict(self.settings, status=self.status, targetLevel=self.target, minAdjustmentSeconds=30)

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
        if monotonic() < self.next_due:
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
                if self.last_frame is not None and sensor_ok:
                    self.temperature = sample['temperatureC']; self.sample_at = monotonic()
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


def run(config):
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
            collector.tick()
            telemetry.tick(pending)
            commands.tick(allow_execution=not automation.busy)
            automation.tick(commands)
            fan_reporter.tick()
            if upload is not None and upload.process.poll() is not None:
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
                    upload = CurlUpload(config, sample)
            time.sleep(0.1)
    finally:
        telemetry.close()
        commands.close()
        fan_reporter.close()
        if upload is not None:
            upload.close()


def main():
    path = CONFIG_PATH
    if '--config' in sys.argv:
        path = sys.argv[sys.argv.index('--config') + 1]
    if '--help' in sys.argv:
        print(__doc__)
        return 0
    config = load_config(path)
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
