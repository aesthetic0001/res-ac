"""Opt-in wrappers for the known legacy Yun Bridge; packet bytes are untouched."""
from timing_trace import command_id


class BridgeTiming(object):
    def __init__(self, trace):
        self.trace = trace
        self.restores = []
        self.sequence = 0
        self.first_byte = False
        self.command = None
        self.sent = {}
        self.pending = {}

    def wrap(self, obj, name, wrapper):
        original = getattr(obj, name)
        self.restores.append((obj, name, original))
        setattr(obj, name, wrapper(original))

    def record(self, event, **values):
        values.setdefault('packet', self.sequence)
        values.setdefault('command', self.command)
        self.trace.record(event, **values)

    def install(self, packet, server, mailbox):
        owner = self
        def reader_init(original):
            def init(reader, processor):
                original(reader, processor)
                owner.wrap(processor, 'run', runners)
                owner.wrap(processor, 'process', dispatch)
            return init

        def process(original):
            def run(reader):
                if not owner.trace.active:
                    return original(reader)
                owner.sequence += 1
                owner.first_byte = False
                owner.record('uart.wait_begin')
                try:
                    return original(reader)
                finally:
                    owner.record('uart.frame_end')
            return run

        def read(original):
            def byte(reader):
                result = original(reader)
                if not owner.first_byte and result in ('\xff', b'\xff'):
                    owner.first_byte = True
                    owner.record('uart.frame_start')
                return result
            return byte

        def dispatch(original):
            def run(data):
                if isinstance(data, bytes):
                    text = data.decode('latin-1')
                else:
                    text = data
                opcode = text[0:1]
                key = text[1:].split('\xfe', 1)[0] if opcode in ('d', 'D') else ''
                if opcode == 'M':
                    fields = text[1:].split(',', 2)
                    key = fields[1] if len(fields) == 3 and fields[0] == 'v1' else ''
                keycode = {'resac_command': 1, 'resac_fan_control': 2, 'resac_command_result': 3,
                           'resac_command_diagnostic': 4, 'resac_fan_invalidate': 5,
                           'resac_fan_heartbeat': 6, 'resac_fan_session': 7,
                           'resac_interval_ms': 8, 'resac_sample': 9}.get(key, 0)
                tag = {'d': 16, 'D': 32, 'M': 48}.get(opcode, 0) + keycode
                owner.record('uart.frame_parsed', tag=tag, value=len(data))
                try:
                    return original(data)
                finally:
                    owner.record('uart.handler_end', tag=tag)
            return run

        def runners(original):
            def run():
                owner.record('bridge.runners_begin')
                try:
                    return original()
                finally:
                    owner.record('bridge.runners_end')
            return run

        def send(original):
            def ack(index, message):
                owner.record('uart.ack_begin', tag=index, value=len(message))
                try:
                    return original(index, message)
                finally:
                    owner.record('uart.ack_end', tag=index)
            return ack

        def crc_check(original):
            def check(crc, expected):
                result = original(crc, expected)
                if not result:
                    owner.record('uart.crc_failed')
                return result
            return check

        def stored(original):
            def put(key, value):
                result = original(key, value)
                if owner.trace.active and key == 'resac_command':
                    ident = command_id(value)
                    if ident:
                        owner.command = ident
                        owner.record('bridge.command_stored')
                return result
            return put

        def write(original):
            def enqueue(message):
                if not owner.trace.active:
                    return original(message)
                ident, kind = None, 0
                if message.get('response') == 'put' and message.get('key') == 'resac_command':
                    ident, kind = command_id(message.get('value', '')), 1
                elif message.get('request') == 'raw':
                    data = message.get('data', '')
                    if isinstance(data, bytes):
                        data = data.decode('ascii', 'ignore')
                    if hasattr(data, 'startswith') and data.startswith('v1,resac_command_result,'):
                        ident, kind = command_id(data), 2
                owner.record('bridge.json_begin', command=ident or owner.command)
                try:
                    result = original(message)
                finally:
                    owner.record('bridge.json_end', command=ident or owner.command)
                if ident:
                    owner.record('bridge.put_confirm_queued' if kind == 1 else 'bridge.completion_queued', command=ident)
                    for client in server.clients[:16]:
                        target = owner.sent.get(client, 0) + len(server.clients_sendbuffer[client])
                        pending = owner.pending.setdefault(client, [])
                        if len(pending) >= 32:
                            pending.pop(0)
                            owner.trace.dropped += 1
                        pending.append((target, ident, kind))
                    owner.trace.dropped += max(0, len(server.clients) - 16)
                return result
            return enqueue

        def receive(original):
            def decode(data):
                owner.record('bridge.input_begin', value=len(data))
                try:
                    return original(data)
                finally:
                    owner.record('bridge.input_end')
            return decode

        def flush(original):
            def run():
                if not owner.trace.active:
                    return original()
                for client in set(owner.sent) | set(owner.pending):
                    if client not in server.clients:
                        owner.sent.pop(client, None)
                        if owner.pending.pop(client, None):
                            owner.record('bridge.client_closed')
                before = {client: len(server.clients_sendbuffer[client]) for client in server.clients[:16]}
                if any(before.values()):
                    owner.record('bridge.socket_flush_begin')
                try:
                    return original()
                finally:
                    for client, size in before.items():
                        if client not in server.clients_sendbuffer:
                            if owner.pending.pop(client, None):
                                owner.record('bridge.client_closed')
                            owner.sent.pop(client, None)
                            continue
                        sent = max(0, size - len(server.clients_sendbuffer[client]))
                        owner.sent[client] = owner.sent.get(client, 0) + sent
                        pending = owner.pending.get(client, [])
                        while pending and pending[0][0] <= owner.sent[client]:
                            _, ident, kind = pending.pop(0)
                            owner.record('bridge.socket_flushed', command=ident, tag=kind)
                    if any(before.values()):
                        owner.record('bridge.socket_flush_end')
            return run

        self.wrap(packet.PacketReader, '__init__', reader_init)
        self.wrap(packet.PacketReader, 'process', process)
        self.wrap(packet.PacketReader, 't_read', read)
        self.wrap(packet.CRC, 'check', crc_check)
        self.wrap(packet, 'send', send)
        self.wrap(server, 'run', flush)
        self.wrap(server, 'write', write)
        if hasattr(server, 'recv'):
            self.wrap(server, 'recv', receive)
        self.wrap(mailbox, 'data_store_put', stored)
        return self

    def uninstall(self):
        for obj, name, original in reversed(self.restores):
            setattr(obj, name, original)
        self.restores = []
