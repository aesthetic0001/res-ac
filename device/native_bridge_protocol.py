"""Bounded binary Bridge frames; compatible with the board's Python 2.7."""
import struct

MAX_FRAME = 32764
KEY_MAX, VALUE_MAX = 64, 512


def _bytes(value):
    if not isinstance(value, bytes):
        value = value.encode('utf-8')
    return value


def entry(key, value):
    key, value = _bytes(key), _bytes(value)
    if (not 0 < len(key) <= KEY_MAX or len(value) > VALUE_MAX or b'\0' in value or
            any(not (byte == 95 or 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122)
                for byte in bytearray(key))):
        raise ValueError('Invalid native Bridge entry')
    return struct.pack('!BH', len(key), len(value)) + key + value


def encode(message):
    if message.get('command') == 'get' and 'key' not in message:
        payload = b's'
    elif message.get('command') == 'put':
        payload = b'p' + entry(message['key'], message['value'])
    else:
        raise ValueError('Unsupported native Bridge request')
    return struct.pack('!I', len(payload)) + payload


def decode(payload):
    if not payload or len(payload) > MAX_FRAME:
        raise ValueError('Invalid native Bridge frame')
    op, data = payload[:1], payload[1:]
    values = []
    while data:
        if len(data) < 3:
            raise ValueError('Truncated native Bridge entry')
        kn, vn = struct.unpack('!BH', data[:3])
        if not 0 < kn <= KEY_MAX or vn > VALUE_MAX or len(data) < 3 + kn + vn:
            raise ValueError('Invalid native Bridge entry length')
        key, value = data[3:3+kn], data[3+kn:3+kn+vn]
        entry(key, value)  # Apply the same bounded field validation both ways.
        values.append((key.decode('utf-8'), value.decode('utf-8')))
        if len(values) > 32:
            raise ValueError('Native Bridge snapshot too large')
        data = data[3+kn+vn:]
    if op == b'S':
        if len(dict(values)) != len(values):
            raise ValueError('Duplicate native Bridge snapshot key')
        return dict(response='get', value=dict(values))
    if len(values) != 1:
        raise ValueError('Expected one native Bridge entry')
    key, value = values[0]
    if op == b'P':
        return dict(response='put', key=key, value=value)
    if op == b'V':
        return dict(request='raw', data='v1,%s,%s' % (key, value))
    raise ValueError('Unknown native Bridge response')
