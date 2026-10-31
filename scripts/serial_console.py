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

    def download(self, source, progress=None, max_bytes=32 * 1024 * 1024):
        """Read a board file without echoing its contents; verify before returning.

        ACK each block to keep the AVR's small serial buffers bounded. Callers
        must store configuration/flash backups in an ignored private directory.
        """
        sender = ("import sys,binascii\nf=open(%r,'rb')\n"
                  "print('RESAC_DOWNLOAD_READY');sys.stdout.flush()\nsys.stdin.readline()\n"
                  "while True:\n b=f.read(2304)\n if not b:break\n"
                  " sys.stdout.write('RESAC_DOWNLOAD_DATA:'+binascii.b2a_base64(b));sys.stdout.flush()\n"
                  " if sys.stdin.readline().strip()!='ACK':sys.exit(1)\n"
                  "f.close()\nprint('RESAC_DOWNLOAD_DONE');sys.stdout.flush()\n") % source
        self.send(('python -u -c ' + shlex.quote(sender) + '\r').encode())
        self.expect(b'RESAC_DOWNLOAD_READY')
        self.send(b'ACK\n')
        data = bytearray()
        while True:
            line = self.expect(b'\n', 15)
            if b'RESAC_DOWNLOAD_DONE' in line:
                break
            match = re.search(rb'RESAC_DOWNLOAD_DATA:([A-Za-z0-9+/=]+)', line)
            if not match:
                continue
            data.extend(base64.b64decode(match.group(1), validate=True))
            if len(data) > max_bytes:
                self.send(b'CANCEL\n')
                self.expect(b'/# ')
                raise RuntimeError('Board download exceeds the configured size limit')
            if progress:
                progress(len(data))
            self.send(b'ACK\n')
        if b'/# ' not in line:
            self.expect(b'/# ')
        digest = self.run('md5sum ' + shlex.quote(source), timeout=60)
        if hashlib.md5(data).hexdigest().encode() not in digest:
            raise RuntimeError('Board download checksum mismatch')
        return bytes(data)

    def close(self):
        try:
            self.send(b'stty echo\r')
            self.expect(b'/# ', 5)
        finally:
            self.port.close()
