#!/usr/bin/env python
"""Linux-side DHT telemetry poster for Python 2.7/3, including legacy Yun JSON.

Private configuration: /etc/res-ac/agent.json. No deployment addresses or tokens
are included here. --probe validates HTTPS and device authentication without
inserting a synthetic reading. Sampling and upload retries run independently.
"""
from __future__ import print_function
import binascii
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
monotonic = getattr(time, 'monotonic', lambda: os.times()[4])
try:
    text_type = unicode
except NameError:
    text_type = str



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


class CurlUpload(object):
    """A bounded curl subprocess; polling completion never waits for the network."""
    def __init__(self, config, payload):
        self.process = None
        self.files = []
        try:
            header_fd, header_path = temporary('res-ac-curl-')
            self.files.append(header_path)
            response_fd, self.response_path = temporary('res-ac-response-')
            self.files.append(self.response_path)
            os.close(response_fd)
            url = config['server_url'] + '/api/devices/' + config['device_id'] + '/readings'
            def quote(value):
                return value.replace('\\', '\\\\').replace('"', '\\"')
            with os.fdopen(header_fd, 'w') as stream:
                stream.write('url = "%s"\n' % quote(url))
                stream.write('header = "Authorization: Bearer %s"\n' % quote(config['device_token']))
            self.process = subprocess.Popen([config.get('curl_binary', 'curl'), '--config', header_path, '--cacert', config['ca_file'],
                                            '--silent', '--show-error', '--connect-timeout', '10', '--max-time', '25',
                                            '--request', 'POST', '--header', 'Content-Type: application/json',
                                            '--data-binary', '@-', '--output', self.response_path, '--write-out', '%{http_code}'],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Sample bodies are small, well below the pipe buffer size.
            self.process.stdin.write(encode_json(payload).encode('utf-8'))
            self.process.stdin.close()
            self.process.stdin = None
        except BaseException:
            self.close()
            raise

    def finish(self):
        output, errors = self.process.communicate()
        if self.process.returncode:
            raise UploadError(curl_code=self.process.returncode)
        try:
            status = int(output.strip())
        except ValueError:
            raise UploadError()
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
    def __init__(self, limit):
        self.queue = []
        self.limit = limit
        self.last_id = None
        self.dropped = 0

    def add(self, sample):
        if sample['readingId'] == self.last_id:
            return False
        self.last_id = sample['readingId']
        if len(self.queue) >= self.limit:
            self.queue.pop(0)
            self.dropped += 1
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
                if time.gmtime().tm_year >= 2025:
                    sample['recordedAt'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                self.pending.add(sample)
        except Exception:
            if self.initialized:
                log('Sensor Bridge unavailable; waiting for reconnect')
            self.initialized = False
            self.next_due = monotonic() + 2



    def report_upload(self, value):
        try:
            self.bridge.put('resac_upload_status', value)
        except Exception:
            pass


def run(config):
    pending = Buffer(config.get('queue_limit', 100))
    collector = Collector(config, pending)
    backoff = 1
    retry_at = 0
    upload = None
    sample = None
    try:
        while True:
            collector.tick()
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
                    upload = CurlUpload(config, sample)
            time.sleep(0.1)
    finally:
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
