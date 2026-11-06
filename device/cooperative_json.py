"""Serialize bounded JSON in small steps so the event loop can service I/O."""
try:
    strings, integers = (str, unicode), (int, long)
except NameError:
    strings, integers = (str,), (int,)


class Encoding(object):
    def __init__(self, value, scalar_encode, limit=32768):
        self.scalar_encode = scalar_encode
        self.limit, self.size, self.parts = limit, 0, []
        self.pending, self.result = b'', None
        self.tokens = self._tokens(value, 0)

    def _scalar(self, value):
        encoded = self.scalar_encode(value)
        return encoded.encode('utf-8') if not isinstance(encoded, bytes) else encoded

    def _tokens(self, value, depth):
        if depth > 16:
            raise ValueError('JSON nesting too deep')
        if isinstance(value, dict):
            yield b'{'
            for index, (key, item) in enumerate(value.items()):
                if not isinstance(key, strings):
                    raise ValueError('JSON keys must be strings')
                if index: yield b','
                yield self._scalar(key)
                yield b':'
                for part in self._tokens(item, depth + 1): yield part
            yield b'}'
        elif isinstance(value, (list, tuple)):
            yield b'['
            for index, item in enumerate(value):
                if index: yield b','
                for part in self._tokens(item, depth + 1): yield part
            yield b']'
        elif value is None:
            yield b'null'
        elif value is True:
            yield b'true'
        elif value is False:
            yield b'false'
        elif isinstance(value, strings + integers + (float,)):
            if isinstance(value, float) and (value != value or value in (float('inf'), -float('inf'))):
                raise ValueError('JSON numbers must be finite')
            yield self._scalar(value)
        else:
            raise ValueError('Unsupported JSON value')

    def step(self, budget=512):
        if not 1 <= budget <= 32768:
            raise ValueError('Invalid serialization budget')
        if self.result is not None:
            return self.result
        while budget:
            if not self.pending:
                try:
                    self.pending = next(self.tokens)
                except StopIteration:
                    self.result = b''.join(self.parts)
                    self.parts = []
                    return self.result
            count = min(budget, len(self.pending))
            if self.size + count > self.limit:
                raise ValueError('JSON request body too large')
            self.parts.append(self.pending[:count])
            self.pending = self.pending[count:]
            self.size += count
            budget -= count
        return None
