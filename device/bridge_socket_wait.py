"""Socket wake-ups for the legacy Yun PacketReader (Python 2.7/3).

Keep the original 50 ms UART byte deadline and background-runner cadence, but
service Mailbox TCP readiness while waiting. Never discard a partial UART packet
just because a TCP client became ready.
"""
import os
import select
import time

monotonic = getattr(time, 'monotonic', lambda: os.times()[4])


def read_byte(stream, processor):
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
                return stream.read(1)
            processor.run()
            continue
        writers = [client for client in json_server.clients
                   if json_server.clients_sendbuffer.get(client)]
        readable, writable, _ = select.select([uart] + list(json_server.sockets),
                                              writers, [], remaining)
        if uart in readable:
            return stream.read(1)
        if readable or writable:
            processor.run()
        else:
            return None
