"""Paced USB-to-Linux console transfers with per-block ACKs and checksums.

Only usable while the serial-terminal recovery firmware is running. Transfer
bodies and unfiltered remote error output are deliberately never printed.
"""
import base64
import hashlib
import re
import secrets
import shlex
import time
import serial


class RemoteError(RuntimeError):
    def __init__(self, output):
        super().__init__('A board command failed; unfiltered output is withheld.')
        self.safe_output = '\n'.join(
            line.decode('ascii', errors='replace') for line in output.splitlines()
            if re.match(rb'^(HTTPS probe failed \(HTTP status [0-9]+; curl exit [0-9]+\)|FAIL: backend probe returned HTTP [0-9]+; check private configuration|Agent failed: [A-Za-z]+)$', line)
        )


class Console:
    def __init__(self, path):
        self.port = serial.Serial(path, 115200, timeout=0.1, write_timeout=3, exclusive=True)
        self.send(b'\r')
        self.expect(b'/# ', 10)
        self.send(b'stty -echo\r')
        self.expect(b'/# ', 10)

    def send(self, data):
        for offset in range(0, len(data), 8):
            self.port.write(data[offset:offset + 8])
            time.sleep(0.01)

    def expect(self, marker, timeout=20):
        data = b''
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            data += self.port.read(self.port.in_waiting or 1)
            if marker in data:
                return data
        raise RuntimeError('Serial console timed out; check that the recovery terminal firmware is running.')

    def run(self, command, timeout=20):
        marker = '__RESAC_EXIT_' + secrets.token_hex(6) + '_'
        wrapped = command + '; printf "\\n' + marker + '%s__\\n" "$?"'
        self.send(wrapped.encode() + b'\r')
        output = self.expect(b'/# ', timeout)
        status = re.search(re.escape(marker).encode() + rb'(\d+)__', output)
        if not status or status.group(1) != b'0':
            raise RemoteError(output)
        return output[:status.start()].strip()

    def upload(self, data, destination, mode=0o600, progress=None):
        temporary = '/tmp/res-ac-upload-' + secrets.token_hex(5)
        receiver = ("import sys,binascii,os\nos.umask(077)\nf=open(%r,'wb')\n"
                    "print('RESAC_READY');sys.stdout.flush()\nwhile True:\n"
                    " s=sys.stdin.readline().strip()\n if s=='END': break\n"
                    " f.write(binascii.a2b_base64(s));print('RESAC_ACK');sys.stdout.flush()\nf.close()\n") % temporary
        self.send(('python -u -c ' + shlex.quote(receiver) + '\r').encode())
        self.expect(b'RESAC_READY')
        # Stay below Linux's canonical input line limit. Pace bytes below the
        # AVR's 250000-baud UART rate; each acknowledged block bounds buffering.
        for offset in range(0, len(data), 2304):
            block = base64.b64encode(data[offset:offset + 2304]) + b'\n'
            for position in range(0, len(block), 32):
                self.port.write(block[position:position + 32])
                time.sleep(0.002)
            self.expect(b'RESAC_ACK', 8)
            if progress:
                progress(min(offset + 2304, len(data)), len(data))
        self.send(b'END\n')
        self.expect(b'/# ')
        checked = self.run('md5sum ' + shlex.quote(temporary))
        if hashlib.md5(data).hexdigest().encode() not in checked:
            raise RuntimeError('Board transfer checksum mismatch')
        self.run('cp ' + shlex.quote(temporary) + ' ' + shlex.quote(destination) +
                 ' && chmod ' + format(mode, 'o') + ' ' + shlex.quote(destination) +
                 ' && rm -f ' + shlex.quote(temporary), timeout=180)

    def close(self):
        try:
            self.send(b'stty echo\r')
            self.expect(b'/# ', 5)
        finally:
            self.port.close()
