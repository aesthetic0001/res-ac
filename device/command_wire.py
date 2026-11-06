"""Bounded command-response decoding for Python 2.7 and 3; no action execution."""
import struct

MAGIC = b'RAC1'
LIMIT = 32768


def decode(body):
    if not isinstance(body, bytes) or not body.startswith(MAGIC) or len(body) > LIMIT:
        raise ValueError('Invalid binary command response')
    position = [len(MAGIC)]
    remaining = [1024]

    def take(size):
        start = position[0]
        if size > len(body) - start:
            raise ValueError('Truncated binary command response')
        position[0] += size
        return body[start:position[0]]

    def count():
        return struct.unpack('!H', take(2))[0]

    def value(depth):
        remaining[0] -= 1
        if depth > 16 or remaining[0] < 0:
            raise ValueError('Binary command response too complex')
        kind = take(1)
        if kind == b'n': return None
        if kind == b't': return True
        if kind == b'f': return False
        if kind == b's': return take(count()).decode('utf-8')
        if kind == b'i': return struct.unpack('!i', take(4))[0]
        if kind == b'd':
            number = struct.unpack('!d', take(8))[0]
            if number != number or number in (float('inf'), -float('inf')):
                raise ValueError('Nonfinite command number')
            return number
        if kind == b'a':
            size = count()
            if size > remaining[0]:
                raise ValueError('Binary command array too large')
            return [value(depth + 1) for _ in range(size)]
        if kind == b'o':
            size = count()
            if size * 2 > remaining[0]:
                raise ValueError('Binary command object too large')
            result = {}
            for _ in range(size):
                key = value(depth + 1)
                if not isinstance(key, type(u'')) or key in result:
                    raise ValueError('Invalid binary command key')
                result[key] = value(depth + 1)
            return result
        raise ValueError('Unknown binary command type')

    result = value(0)
    if position[0] != len(body):
        raise ValueError('Trailing binary command content')
    return result
