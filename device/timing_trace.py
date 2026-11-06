"""Opt-in, bounded RAM diagnostics shared by the agent and legacy Bridge."""
import collections
import os
import re
import time

try:
    text_types = (str, unicode)
    integer_types = (int, long)
except NameError:
    text_types = (str,)
    integer_types = (int,)

EVENTS = frozenset('''uart.wait_begin uart.frame_start uart.frame_parsed uart.handler_end
uart.ack_begin uart.ack_end uart.frame_end uart.crc_failed bridge.runners_begin bridge.runners_end
bridge.input_begin bridge.input_end bridge.command_stored bridge.put_confirm_queued bridge.json_begin bridge.json_end bridge.completion_queued
bridge.socket_flush_begin bridge.socket_flush_end bridge.socket_flushed bridge.client_closed
agent.command_queue agent.put_confirm agent.gpio_begin agent.gpio_assert agent.recv_begin
agent.recv_end agent.json_begin agent.json_end agent.completion_received agent.completion_consumed'''.split())
UUID = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')


def clock():
    if hasattr(time, 'monotonic'):
        info = time.get_clock_info('monotonic')
        return time.monotonic, 'monotonic', max(1, int(info.resolution * 1000000))
    try:
        import ctypes
        class Timespec(ctypes.Structure):
            _fields_ = [('seconds', ctypes.c_long), ('nanoseconds', ctypes.c_long)]
        for library in (None, 'librt.so.0', 'librt.so.1'):
            try:
                lib = ctypes.CDLL(library)
                gettime = lib.clock_gettime
                gettime.argtypes = [ctypes.c_int, ctypes.POINTER(Timespec)]
                gettime.restype = ctypes.c_int
                sample = Timespec()
                if gettime(1, ctypes.byref(sample)) != 0:
                    continue
                def read(fn=gettime, value=sample):
                    if fn(1, ctypes.byref(value)) != 0:
                        raise OSError('Monotonic clock failed')
                    return value.seconds + value.nanoseconds / 1000000000.0
                return read, 'clock_gettime_monotonic', 1
            except (AttributeError, OSError):
                pass
    except ImportError:
        pass
    # Explicitly label the old board's coarse fallback; never imply microsecond precision.
    return lambda: os.times()[4], 'elapsed_ticks', 10000


def command_id(frame):
    """Extract only a correlation UUID, never save command bodies or sessions."""
    if not isinstance(frame, text_types):
        try:
            frame = frame.decode('ascii')
        except (AttributeError, UnicodeError):
            return None
    if frame.startswith('v1,resac_command_result,'):
        frame = frame.split(',', 2)[2]
    if frame.startswith('v5,'):
        fields = frame.split(',', 4)
        frame = fields[-1] if len(fields) == 5 else ''
    elif frame.startswith('v6,'):
        fields = frame.split(',', 8)
        frame = fields[-1] if len(fields) == 9 else ''
    fields = frame.split(',', 2)
    return str(fields[1]) if len(fields) >= 2 and fields[0] in ('v1', 'v3', 'v4') and UUID.match(fields[1]) else None


class TimingTrace(object):
    def __init__(self, limit=1024, timer=None):
        if not 16 <= limit <= 4096:
            raise ValueError('Timing trace limit must be 16..4096')
        self.timer, self.clock_name, self.resolution_us = clock() if timer is None else (timer, 'test', 1)
        self.events = collections.deque(maxlen=limit)
        self.limit, self.dropped, self.active, self.dump_requested = limit, 0, True, False
        self.exported, self.clock_errors = False, 0

    def record(self, event, packet=0, tag=0, value=0, command=None):
        if not self.active:
            return
        if event not in EVENTS or any(not isinstance(x, integer_types) or x < 0 for x in (packet, tag, value)):
            raise ValueError('Unknown timing event or nonnumeric metadata')
        if command is not None and (not isinstance(command, text_types) or not UUID.match(command)):
            raise ValueError('Invalid timing correlation ID')
        if len(self.events) == self.limit:
            self.dropped += 1
        try:
            stamp = int(self.timer() * 1000000)
        except Exception:
            self.clock_errors += 1
            self.request_dump()
            return
        self.events.append((stamp, event, packet, tag, value, command))

    def request_dump(self):
        # Signal handlers only freeze the buffer; no I/O or serialization here.
        self.active = False
        if not self.exported:
            self.dump_requested = True

    @staticmethod
    def validate_output(path):
        parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        if parent != '/tmp' and not parent.startswith('/tmp/'):
            raise ValueError('Timing traces must stay in RAM under /tmp')
        if os.path.lexists(path):
            raise ValueError('Timing output must be a new file')

    def write(self, path):
        if self.active:
            raise ValueError('Freeze tracing before exporting')
        self.validate_output(path)
        payload = dict(version=1, clock=self.clock_name, resolutionUs=self.resolution_us,
                       limit=self.limit, dropped=self.dropped, clockErrors=self.clock_errors, pid=os.getpid(),
                       columns=['monotonicUs', 'event', 'packet', 'tag', 'value', 'commandId'],
                       events=list(self.events))
        # Do not import json before the vendor Bridge chooses its local module.
        import json
        encode = getattr(json, 'dumps', None) or json.write
        data = encode(payload).encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        self.dump_requested = False
        self.exported = True
