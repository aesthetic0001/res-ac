#!/usr/bin/env python
"""Check saved eduroam credentials on the Seeeduino's Linux system.

python /root/check-eduroam.py            Check fields and current authentication.
python /root/check-eduroam.py --connect  Test authentication for up to 60 seconds.
python /root/check-eduroam.py --show-credentials  Print stored credentials in plaintext.
The password is hidden by default. Requires the board's Python 2.7 and full wpad.
"""
from __future__ import print_function
import os
import socket
import subprocess
import sys
import time


def uci_get(key):
    process = subprocess.Popen(['uci', '-q', 'get', key], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    output = process.communicate()[0]
    return output.rstrip('\n') if process.returncode == 0 else None


def run(arguments):
    with open('/dev/null', 'w') as quiet:
        return subprocess.call(arguments, stdout=quiet, stderr=quiet)


class Supplicant(object):
    def __init__(self, path):
        self.local = '/tmp/check-eduroam-' + str(os.getpid())
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(self.local)
        self.socket.settimeout(2)
        try:
            self.socket.connect(path)
        except socket.error:
            self.close()
            raise

    def close(self):
        self.socket.close()
        if os.path.exists(self.local):
            os.unlink(self.local)

    def request(self, command):
        self.socket.send(command)
        while True:
            response = self.socket.recv(16384)
            if not response.startswith('<'):
                return response

    def status(self):
        response = self.request('STATUS')
        return dict(line.split('=', 1) for line in response.splitlines() if '=' in line)


def find_supplicant():
    for name in os.listdir('/var/run'):
        directory = '/var/run/' + name
        if not name.startswith('wpa_supplicant-') or not os.path.isdir(directory):
            continue
        config = directory + '.conf'
        if not os.path.isfile(config):
            continue
        with open(config) as stream:
            # Match this profile without displaying any configuration values.
            if 'ssid="eduroam"' not in [line.strip() for line in stream]:
                continue
        for child in os.listdir(directory):
            try:
                return Supplicant(directory + '/' + child)
            except socket.error:
                continue
    return None


def report_authenticated():
    print('PASS: eduroam authentication succeeded; the saved credentials were accepted.')
    process = subprocess.Popen(['ifstatus', 'wwan'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    status = process.communicate()[0]
    if '"up": true' in status:
        print('DHCP: the Wi-Fi uplink is up.')
    else:
        print('DHCP: still pending; this is separate from credential validation.')
    return 0


def main():
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        return 0
    if any(option not in ['--connect', '--show-credentials'] for option in sys.argv[1:]):
        print('Use --help for usage.')
        return 2
    identity = uci_get('wireless.eduroam.identity')
    password = uci_get('wireless.eduroam.password')
    if '--show-credentials' in sys.argv:
        print('Saved username: ' + (identity if identity is not None else '(not set)'))
        print('Saved password: ' + (password if password is not None else '(not set)'))
    print('Username stored: ' + ('yes' if identity else 'NO'))
    print('Password stored: ' + ('yes (hidden)' if password else 'NO'))
    if not identity or '@' not in identity or not all(identity.rsplit('@', 1)) or not password:
        print('FAIL: saved credentials are missing or the identity has no username/realm.')
        return 2
    certificate = uci_get('wireless.eduroam.ca_cert')
    if not certificate or not os.path.isfile(certificate):
        print('FAIL: the configured CA certificate is missing.')
        return 2
    if time.gmtime().tm_year < 2025:
        print('FAIL: the board clock must be corrected before checking certificates.')
        return 2

    connect = '--connect' in sys.argv
    previous_disabled = uci_get('wireless.eduroam.disabled')
    changed = connect and previous_disabled == '1'
    accepted = False
    control = None
    try:
        if connect:
            print('Testing the saved credentials with eduroam (up to 60 seconds)...')
            sys.stdout.flush()
            if changed:
                if run(['uci', 'set', 'wireless.eduroam.disabled=0']) or run(['wifi']):
                    print('FAIL: could not enable the eduroam interface.')
                    return 2

        deadline = time.time() + (60 if connect else 3)
        while control is None and time.time() < deadline:
            control = find_supplicant()
            if control is None:
                time.sleep(0.5)
        if control is None:
            print('UNVERIFIED: eduroam is inactive. Use --connect to test the credentials.')
            return 1
        state = control.status()
        print('Supplicant state: ' + state.get('wpa_state', 'unknown'))
        if state.get('wpa_state') == 'COMPLETED' and state.get('ssid') == 'eduroam':
            accepted = True
            return report_authenticated()
        if not connect:
            print('UNVERIFIED: stored credentials exist, but authentication has not succeeded.')
            print('Use --connect to test them with eduroam.')
            return 1

        control.request('ATTACH')
        control.request('REASSOCIATE')
        last_progress = time.time()
        while time.time() < deadline:
            try:
                event = control.socket.recv(16384)
            except socket.timeout:
                event = ''
            if 'CTRL-EVENT-EAP-TLS-CERT-ERROR' in event:
                print('FAIL: server certificate validation failed; password validity is unverified.')
                return 1
            if 'CTRL-EVENT-EAP-FAILURE' in event:
                print('FAIL: EAP authentication failed. This alone does not prove the password is wrong.')
                print('Possible causes include credentials, account policy, or EAP/TLS compatibility.')
                return 1
            if 'CTRL-EVENT-EAP-SUCCESS' in event:
                accepted = True
                return report_authenticated()
            if 'CTRL-EVENT-CONNECTED' in event:
                # Confirm the expected network before reporting success.
                if control.status().get('ssid') == 'eduroam':
                    accepted = True
                    return report_authenticated()
            if time.time() - last_progress >= 5:
                print('Waiting for eduroam authentication...')
                sys.stdout.flush()
                last_progress = time.time()
        print('UNVERIFIED: authentication timed out; this does not prove the password is wrong.')
        return 1
    finally:
        if control is not None:
            control.close()
        if changed and not accepted:
            run(['uci', 'set', 'wireless.eduroam.disabled=' + previous_disabled])
            run(['wifi'])
            print('Restored the previous disabled state after the test.')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (IOError, OSError, socket.error):
        print('ERROR: could not communicate with the board network services.')
        sys.exit(2)
