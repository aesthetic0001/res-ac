"""Cooperative RAC1 request values for Python 2.7/3, without JSON formatting."""
import struct
from cooperative_json import Encoding as JsonEncoding, strings, integers, SURROGATE_UTF8

MAGIC = b'RAC1'


class Encoding(JsonEncoding):
    def __init__(self, value, limit=32768):
        self.remaining = 8192
        JsonEncoding.__init__(self, value, limit=limit)

    def _string(self, value, key=False):
        if len(value) > 32768:
            raise ValueError('Binary request string too large')
        cache_key = (type(value), value) if key and len(value) <= 128 else None
        if cache_key is not None and cache_key in self.keys:
            return self.keys[cache_key]
        if isinstance(value, bytes):
            value.decode('utf-8')
            encoded = value
        else:
            encoded = value.encode('utf-8')
        if len(encoded) > 65535 or SURROGATE_UTF8.search(encoded):
            raise ValueError('Invalid binary request string')
        result = b's' + struct.pack('!H', len(encoded)) + encoded
        if cache_key is not None and len(self.keys) < 64:
            self.keys[cache_key] = result
        return result

    def _tokens(self, value, depth):
        if depth == 0:
            yield MAGIC
        self.remaining -= 1
        if depth > 16 or self.remaining < 0:
            raise ValueError('Binary request too complex')
        if value is None: yield b'n'
        elif value is True: yield b't'
        elif value is False: yield b'f'
        elif isinstance(value, strings): yield self._string(value)
        elif isinstance(value, integers) and -2147483648 <= value <= 2147483647:
            yield b'i' + struct.pack('!i', value)
        elif isinstance(value, integers + (float,)):
            try:
                number = float(value)
            except OverflowError:
                raise ValueError('Nonfinite binary request number')
            if number != number or number in (float('inf'), -float('inf')):
                raise ValueError('Nonfinite binary request number')
            yield b'd' + struct.pack('!d', number)
        elif isinstance(value, (list, tuple)):
            if len(value) > self.remaining:
                raise ValueError('Binary request array too large')
            yield b'a' + struct.pack('!H', len(value))
            for child in value:
                for part in self._tokens(child, depth + 1): yield part
        elif isinstance(value, dict):
            if len(value) * 2 > self.remaining:
                raise ValueError('Binary request object too large')
            yield b'o' + struct.pack('!H', len(value))
            seen = set()
            for key, child in value.items():
                if not isinstance(key, strings) or depth >= 16:
                    raise ValueError('Invalid binary request key')
                self.remaining -= 1
                encoded = self._string(key, key=True)
                if encoded in seen:
                    raise ValueError('Duplicate binary request key')
                seen.add(encoded)
                yield encoded
                for part in self._tokens(child, depth + 1): yield part
        else:
            raise ValueError('Unsupported binary request value')


def encode(value):
    encoding = Encoding(value)
    result = None
    while result is None:
        result = encoding.step()
    return result
