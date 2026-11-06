#!/usr/bin/env python
"""Explicit diagnostic launcher; never installed as the normal Bridge service."""
import os
import signal
import sys
from timing_trace import TimingTrace
from bridge_timing import BridgeTiming


def main():
    options = {'--bridge-dir': '/usr/lib/python2.7/bridge', '--limit': '1024'}
    args = sys.argv[1:]
    if '--help' in args:
        sys.stderr.write('run_bridge_timing.py --output /tmp/new-capture.json [--bridge-dir PATH] [--limit 1024]\n')
        return 0
    if len(args) % 2:
        raise ValueError('Timing options require values')
    for key, value in zip(args[::2], args[1::2]):
        if key not in ('--output', '--bridge-dir', '--limit'):
            raise ValueError('Unknown timing option')
        options[key] = value
    output = options.get('--output')
    if not output:
        raise ValueError('Timing output is required')
    TimingTrace.validate_output(output)
    trace = TimingTrace(int(options['--limit']))
    sys.path.insert(0, options['--bridge-dir'])
    import packet
    from mailbox import json_server, mailbox
    hooks = BridgeTiming(trace).install(packet, json_server, mailbox)
    signal.signal(signal.SIGUSR1, lambda *_: trace.request_dump())
    # Export only after capture is frozen. Serialization is outside measured work.
    def checkpoint(reader):
        if trace.dump_requested:
            trace.write(output)
    original_process = packet.PacketReader.process
    def process(reader):
        result = original_process(reader)
        checkpoint(reader)
        return result
    packet.PacketReader.process = process
    path = os.path.join(options['--bridge-dir'], 'bridge.py')
    try:
        with open(path, 'rb') as stream:
            code = compile(stream.read(), path, 'exec')
        scope = dict(__name__='__main__', __file__=path)
        eval(code, scope, scope)
    finally:
        hooks.uninstall()
        if trace.active:
            trace.request_dump()
        if trace.dump_requested:
            trace.write(output)


if __name__ == '__main__':
    main()
