"""Socket wake-ups for the legacy Yun PacketReader (Python 2.7/3).

Keep the original 50 ms UART byte deadline and background-runner cadence, but
service Mailbox TCP readiness while waiting. Never discard a partial UART packet
just because a TCP client became ready.
"""
import os
import select
import time

UART_CHUNK = 64

monotonic = getattr(time, 'monotonic', lambda: os.times()[4])


def _ready_byte(uart, processor):
    # Raw UART mode has VMIN=1: read what is available, up to one bounded chunk.
    # Leave Python's buffered stdin unused; PacketReader remains the sole reader.
    block = os.read(uart, UART_CHUNK)
    processor._resac_uart_pending = block[1:]
    return block[:1] or None


def read_byte(stream, processor):
    # Parsing a frame must not select(), read(), and query the clock per byte.
    # Keep read-ahead with this processor and stream, including across frames.
    if getattr(processor, '_resac_uart_stream', None) is not stream:
        processor._resac_uart_stream = stream
        processor._resac_uart_pending = b''
    pending = processor._resac_uart_pending
    if pending:
        processor._resac_uart_pending = pending[1:]
        return pending[:1]
    # Imported lazily: packet and mailbox are initialized by the same service.
    from mailbox import json_server
    deadline = monotonic() + 0.050
    uart = stream.fileno()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return None
        # TCP recv can decode several commands at once. Drain that already-read
        # queue without waiting for another socket edge or a UART timeout.
        if json_server.available():
            if select.select([uart], [], [], 0)[0]:
                return _ready_byte(uart, processor)
            processor.run()
            continue
        writers = [client for client in json_server.clients
                   if json_server.clients_sendbuffer.get(client)]
        readable, writable, _ = select.select([uart] + list(json_server.sockets),
                                              writers, [], remaining)
        if uart in readable:
            # The vendor launcher does not use python -u. A buffered read(1)
            # can read ahead, leaving the next byte in Python while select()
            # waits on an empty kernel buffer and falsely times out the frame.
            return _ready_byte(uart, processor)
        if readable or writable:
            processor.run()
        else:
            return None
