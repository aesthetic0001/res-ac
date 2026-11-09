"""Bounded link checks and temporary AP recovery for Python 2.7/3.

Uses the existing supplicant profile; never edits or saves its configuration.
"""
from __future__ import print_function
import binascii
import os
import re
import socket
import time

MAC = re.compile(r'^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$')
DEVICE = re.compile(r'^[A-Za-z0-9_.:-]{1,15}$')
monotonic = getattr(time, 'monotonic', lambda: os.times()[4])


class Supplicant(object):
    def __init__(self, device):
        if not DEVICE.match(device):
            raise ValueError('Invalid wireless device')
        self.path = '/tmp/res-ac-wpa-' + binascii.hexlify(os.urandom(8)).decode('ascii')
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            mask = os.umask(0o077)
            try:
                self.socket.bind(self.path)
            finally:
                os.umask(mask)
            self.socket.settimeout(2)
            last = None
            for directory in ['/var/run/wpa_supplicant-' + device, '/var/run/wpa_supplicant']:
                try:
                    self.socket.connect(directory + '/' + device)
                    break
                except socket.error as error:
                    last = error
            else:
                raise last
        except BaseException:
            self.close()
            raise

    def request(self, command):
        self.socket.send(command.encode('ascii'))
        # Do not ATTACH: this socket receives command replies only.
        reply = self.socket.recv(32769)
        if len(reply) > 32768:
            raise ValueError('Supplicant response too large')
        return reply.decode('utf-8').strip()

    def status(self):
        return dict(line.split('=', 1) for line in self.request('STATUS').splitlines() if '=' in line)

    def close(self):
        self.socket.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


def station(text):
    """The counter must refer to exactly one currently associated station."""
    matches = re.findall(r'^Station ([0-9a-f:]+) ', text, re.M)
    failed = re.findall(r'^\s*tx failed:\s*(\d+)\s*$', text, re.M)
    if len(matches) != 1 or not MAC.match(matches[0]) or len(failed) != 1:
        return None
    return dict(bssid=matches[0], failed=int(failed[0]))


def link_health(device, gateways, command):
    if not DEVICE.match(device) or not gateways:
        return dict(state='unknown')
    gateway = gateways[0]
    try:
        socket.inet_aton(gateway)
    except (socket.error, TypeError):
        return dict(state='unknown')
    before = station(command(['iw', 'dev', device, 'station', 'dump'], allow_failure=True))
    if before is None:
        return dict(state='unknown')
    output = command(['ping', '-c', '4', '-W', '1', gateway], timeout=8, allow_failure=True)
    after = station(command(['iw', 'dev', device, 'station', 'dump'], allow_failure=True))
    counts = re.search(r'(\d+) packets transmitted,\s*(\d+) (?:packets )?received', output)
    if (after is None or before['bssid'] != after['bssid'] or
            after['failed'] < before['failed'] or not counts):
        return dict(state='unknown')
    sent, received = map(int, counts.groups())
    if sent != 4 or not 0 <= received <= sent:
        return dict(state='unknown')
    failed = after['failed'] - before['failed']
    # ICMP filtering/rate limiting alone must never trigger roaming. Require
    # independent link-layer transmit failures during the same probe window.
    state = 'up' if received >= 3 else ('poor' if failed >= 2 else 'unknown')
    return dict(state=state, bssid=after['bssid'], received=received, sent=sent, failed=failed)


def candidates(text, ssid, current, avoided, now):
    found = {}
    for line in text.splitlines():
        fields = line.split('\t')
        if len(fields) != 5:
            continue
        bssid, frequency, signal, flags, name = fields
        try:
            signal, frequency = int(signal), int(frequency)
        except ValueError:
            continue
        if (not MAC.match(bssid) or bssid == current or name != ssid or
                not -80 <= signal < 0 or not 2400 <= frequency <= 5900 or
                not re.search(r'\[WPA2-EAP-[^\]]*CCMP[^\]]*\]', flags) or
                avoided.get(bssid, (0, 0))[0] > now):
            continue
        found[bssid] = (signal, bssid)
    return [bssid for signal, bssid in sorted(found.values(), reverse=True)]


class Roaming(object):
    def __init__(self, command, report, restart, allowed, control=Supplicant):
        self.command, self.report, self.restart = command, report, restart
        self.allowed, self.control = allowed, control
        self.poor_since = None
        self.poor_bssid = None
        self.good_since = None
        self.pending = None
        self.settle_until = 0
        self.next_attempt = 0
        self.cooldown = 300
        self.avoided = {}
        self.verifying = False

    def reset_observation(self):
        self.poor_since = self.poor_bssid = self.good_since = None
        self.pending = None

    def avoid(self, bssid, now):
        previous = self.avoided.get(bssid, (0, 0))
        failures = min(previous[1] + 1, 3)
        self.avoided[bssid] = (now + min(900 * (2 ** (failures - 1)), 3600), failures)
        if len(self.avoided) > 32:
            oldest = min(self.avoided, key=lambda key: self.avoided[key][0])
            del self.avoided[oldest]

    def reserve(self, now):
        self.next_attempt = now + self.cooldown
        self.cooldown = min(self.cooldown * 2, 1800)
        self.settle_until = now + 60
        self.poor_since = self.poor_bssid = None
        self.pending = None

    def tick(self, device, gateways, radio, now):
        if now < self.settle_until:
            return
        health = link_health(device, gateways, self.command)
        self.observe(health, device, radio, now)

    def observe(self, health, device, radio, now):
        state = health.get('state')
        if state == 'up':
            self.poor_since = self.poor_bssid = None
            self.pending = None
            if self.verifying:
                self.report('Wi-Fi access-point recovery verified by local gateway replies')
                self.verifying = False
            if self.good_since is None:
                self.good_since = now
            if now - self.good_since >= 300:
                self.cooldown = 300
                self.next_attempt = 0
            return
        self.good_since = None
        bssid = health.get('bssid', '')
        if state != 'poor' or not MAC.match(bssid):
            self.poor_since = self.poor_bssid = None
            self.pending = None
            return
        if bssid != self.poor_bssid:
            self.poor_since, self.poor_bssid = now, bssid
            self.pending = None
        if now - self.poor_since < 120 or now < self.next_attempt:
            return
        if not self.allowed(radio, device):
            self.reset_observation()
            return
        connection = None
        try:
            connection = self.control(device)
            status = connection.status()
            # ROAM uses this existing enterprise profile. Never choose another
            # SSID or weaken its EAP/cipher/certificate settings.
            if (status.get('wpa_state') != 'COMPLETED' or status.get('bssid') != bssid or
                    'EAP' not in status.get('key_mgmt', '') or not status.get('ssid')):
                self.reset_observation()
                return
            if self.pending is None:
                if connection.request('SCAN') != 'OK':
                    self.reserve(now)
                    self.report('Wi-Fi recovery scan unavailable; waiting before retry')
                    return
                self.pending = (device, radio, bssid, status['ssid'], now)
                self.report('Sustained gateway loss and Wi-Fi transmit failures; scanning alternative access points')
                return
            old_device, old_radio, old_bssid, ssid, started = self.pending
            if (device, radio, bssid, status['ssid']) != (old_device, old_radio, old_bssid, ssid):
                self.reset_observation()
                return
            if now - started < 15:
                return
            choices = candidates(connection.request('SCAN_RESULTS'), ssid, bssid, self.avoided, now)
            # Check policy again after the scan: manual ifdown/profile changes
            # must win over recovery, including while a scan is outstanding.
            if not self.allowed(radio, device):
                self.reset_observation()
                return
            self.reserve(now)
            if choices:
                self.avoid(bssid, now)
                if connection.request('ROAM ' + choices[0]) == 'OK':
                    self.verifying = True
                    self.report('Trying another access point on the configured enterprise network')
                else:
                    self.avoid(choices[0], now)
                    self.report('Wi-Fi access-point switch rejected; waiting before retry')
            else:
                self.report('No eligible alternative access point; restarting configured radio')
                self.restart(radio)
        except (IOError, OSError, ValueError, socket.error):
            if self.next_attempt <= now:
                self.reserve(now)
            self.report('Wi-Fi access-point recovery unavailable; waiting before retry')
        finally:
            if connection is not None:
                connection.close()
