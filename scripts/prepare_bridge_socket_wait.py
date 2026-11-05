#!/usr/bin/env python3
"""Prepare a narrowly matched legacy Yun PacketReader update; never deploy it."""
import argparse
import hashlib
from pathlib import Path


ORIGINAL = '''  def t_read(self):
    ret = select.select([stdin.fileno()], [], [], 0.050)
    if ret[0] == [stdin.fileno()]:
      return stdin.read(1)
    return None'''
REPLACEMENT = '''  def t_read(self):
    # res-ac: wake on Mailbox TCP readiness without aborting UART frames.
    from bridge_socket_wait import read_byte
    return read_byte(stdin, self.processor)'''


def prepare(source):
    text = source.decode('utf-8')
    if text.count(ORIGINAL) == 1 and REPLACEMENT not in text:
        return text.replace(ORIGINAL, REPLACEMENT).encode('utf-8')
    if text.count(REPLACEMENT) == 1 and ORIGINAL not in text:
        return source
    raise ValueError('Unrecognized Bridge PacketReader; refusing an unreviewed replacement')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Privately downloaded bridge/packet.py')
    parser.add_argument('output', type=Path, help='Local output; installer validates it against the board source')
    args = parser.parse_args()
    source = args.source.read_bytes()
    result = prepare(source)
    args.output.write_bytes(result)
    print('Prepared Bridge socket wait; source SHA-256=' + hashlib.sha256(source).hexdigest())


if __name__ == '__main__':
    main()
