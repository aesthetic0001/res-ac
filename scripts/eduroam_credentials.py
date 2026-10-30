#!/usr/bin/env python3
"""Enter eduroam credentials privately and finish the prepared Seeeduino setup.

Run with ~/.platformio/penv/bin/python scripts/eduroam_credentials.py
Close other serial monitors before running this command.
"""
import argparse
import base64
import datetime
import getpass
import re
import shlex
import sys
import time

import serial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--realm', help='Institution realm used when entering a username without @realm')
    parser.add_argument('--check', action='store_true', help='Check board readiness without entering or saving credentials')
    args = parser.parse_args()
    with serial.Serial(args.port, 115200, timeout=0.1, write_timeout=3, exclusive=True) as port:
        def send(data):
            for offset in range(0, len(data), 8):
                port.write(data[offset:offset + 8])
                time.sleep(0.01)

        def wait_for(marker, timeout=15):
            output = b''
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                output += port.read(port.in_waiting or 1)
                if marker in output:
                    return output
            raise RuntimeError('The board did not respond as expected; check its serial console.')

        def command(text):
            send(text.encode() + b'\r')
            return wait_for(b'/# ')

        def save_credentials(identity, password, check=False):
            send(('python -u /root/eduroam-receiver.py' + (' --check' if check else '') + '\r').encode())
            wait_for(b'CREDENTIALS_READY')
            send(base64.b64encode(identity.encode('utf-8')) + b'\n'
                 + base64.b64encode(password.encode('utf-8')) + b'\n')
            output = wait_for(b'/# ')
            if b'CREDENTIALS_SAVED' not in output:
                failure = next((line.decode('ascii', errors='replace') for line in output.splitlines()
                                if line.startswith(b'CREDENTIALS_FAILED:')), 'No success acknowledgment')
                raise RuntimeError('The board could not save and verify the credentials (' + failure + ').')

        # Only command text is sent through the shell. Credentials go to the
        # receiving Python process with terminal echo disabled, bypassing history.
        send(b'\r')
        wait_for(b'/# ')
        command('stty -echo')
        try:
            check = command('uci -q get wireless.eduroam.ssid')
            if b'eduroam' not in [line.strip() for line in check.splitlines()]:
                raise RuntimeError('The board has not been prepared for eduroam yet.')
            if args.check:
                save_credentials('serial-check@example.invalid', 'test"\\~ $ unicode-\u00e9 ', check=True)
                print('Board is ready. Credential transfer, UCI save, and readback passed in an isolated configuration.')
                return
            identity = input('Eduroam identity (username@institution-realm): ').strip()
            if '@' not in identity:
                realm = args.realm or input('Institution realm: ').strip()
                identity += '@' + realm
            if not re.fullmatch(r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', identity):
                parser.error('Use the full eduroam identity provided by your institution.')
            password = getpass.getpass('Eduroam password (hidden): ')
            if any(c in password for c in '\r\n\0'):
                parser.error('Passwords containing line breaks or NUL are unsupported.')
            if not password:
                parser.error('Password cannot be empty.')
            save_credentials(identity, password)
            now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            command('date -u -s ' + shlex.quote(now))
            command('date +%s > /etc/eduroam-clock; chmod 600 /etc/config/wireless; wifi')
            print('Credentials saved. Checking eduroam and DHCP for up to 60 seconds...', flush=True)
            for attempt in range(12):
                time.sleep(5)
                output = command("ifstatus wwan | grep -E '\"up\"|\"address\"|\"target\"'")
                if b'"up": true' in output:
                    print(output.decode(errors='replace').replace('/# ', '').strip())
                    print('The wwan interface is up. You can ask Codex to verify DNS and connectivity.')
                    return
                print('Still waiting (' + str((attempt + 1) * 5) + '/60 seconds)...', flush=True)
            print('Connection is not up yet. Ask Codex to inspect authentication and DHCP logs.')
        finally:
            command('stty echo')


if __name__ == '__main__':
    try:
        main()
    except (serial.SerialException, OSError, RuntimeError) as error:
        print('Setup failed: ' + str(error), file=sys.stderr)
        sys.exit(1)
