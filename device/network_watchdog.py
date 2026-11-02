#!/usr/bin/env python
"""Recover a stuck eduroam uplink on the legacy Seeeduino Linux system.

No external probes, credential changes, or board reboots. Run as
an independent service so HTTPS polling and sensor collection remain available.
"""
from __future__ import print_function
import os
import re
import subprocess
import sys
import time

from res_ac_agent import decode_json
from dhcp_route_repair import repair, default_exists

monotonic = getattr(time, 'monotonic', lambda: os.times()[4])


def command(arguments, timeout=10, allow_failure=False):
    with open('/dev/null', 'w') as quiet:
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=quiet)
        deadline = monotonic() + timeout
        # These commands produce small, bounded output. Never run commands that
        # print wireless configuration or private agent configuration here.
        while process.poll() is None:
            if monotonic() >= deadline:
                process.kill()
                process.communicate()
                raise IOError('Network command timed out')
            time.sleep(0.1)
        output = process.communicate()[0].decode('utf-8')
    if process.returncode and not allow_failure:
        raise IOError('Network command failed')
    return output.strip()


def setting(key, default=None):
    value = command(['uci', '-q', 'get', key], allow_failure=True)
    return value if value else default


def network_status(text):
    # The installed Python 2 JSON reader rejects whitespace-only empty arrays
    # and objects from ifstatus. Compact whitespace outside JSON strings only.
    compact = re.sub(r'"(?:\\.|[^"\\])*"|\s+',
                     lambda match: match.group(0) if match.group(0).startswith('"') else '', text)
    return decode_json(compact)


def snapshot():
    """Return (state, radio); unknown/disabled states never permit a reset."""
    if (setting('wireless.eduroam.network') != 'wwan' or
            setting('wireless.eduroam.mode') != 'sta' or
            setting('network.wwan.proto') != 'dhcp'):
        return 'disabled', None
    radio = setting('wireless.eduroam.device', '')
    if not re.match(r'^[A-Za-z0-9_]{1,32}$', radio):
        return 'unknown', None
    if any(setting(key, '0') == '1' for key in
           ['wireless.eduroam.disabled', 'wireless.' + radio + '.disabled']):
        return 'disabled', None
    if (setting('network.wwan.auto', '1') == '0' or
            setting('network.wwan.defaultroute', '1') == '0'):
        return 'disabled', None
    status = network_status(command(['ifstatus', 'wwan'], allow_failure=True))
    if not isinstance(status, dict) or not isinstance(status.get('up'), bool):
        return 'unknown', None
    if status.get('autostart') is False:
        return 'disabled', None
    if not status.get('up') or not status.get('ipv4-address'):
        return 'down', radio
    device = status.get('l3_device', '')
    if not re.match(r'^[A-Za-z0-9_.:-]{1,15}$', device):
        return 'unknown', None
    gateways = [route['nexthop'] for route in status.get('route', [])
                if route.get('target') == '0.0.0.0' and route.get('mask') == 0
                and route.get('nexthop')]
    if not gateways:
        return 'down', radio
    metric = int(setting('network.wwan.metric', '0'))
    if all(default_exists(device, gateway, metric) for gateway in gateways):
        return 'up', radio
    # Repair the known DHCP route before considering a radio restart. This uses
    # netifd's current lease, honors policy, and never replaces another route.
    repair('renew', dict(INTERFACE='wwan', interface=device, router=' '.join(gateways)))
    if all(default_exists(device, gateway, metric) for gateway in gateways):
        return 'up', radio
    return 'down', radio


class Recovery(object):
    def __init__(self):
        self.down_since = None
        self.up_since = None
        self.next_attempt = 0
        self.cooldown = 300
        self.radio = None

    def tick(self, state, radio, now, restart):
        if state == 'up':
            self.down_since = None
            if self.up_since is None:
                self.up_since = now
            if now - self.up_since >= 300:
                self.cooldown = 300
            return
        self.up_since = None
        if state != 'down' or not radio:
            self.down_since = None
            self.radio = None
            return
        if self.down_since is None or radio != self.radio:
            self.down_since = now
            self.radio = radio
        if now - self.down_since < 120 or now < self.next_attempt:
            return
        # Reserve the cooldown before running commands, including failed ones.
        self.next_attempt = now + self.cooldown
        self.cooldown = min(self.cooldown * 2, 1800)
        self.down_since = now
        restart(radio)


def report(message):
    command(['logger', '-t', 'res-ac-network', message])


def restart_radio(radio):
    report('Wi-Fi uplink unavailable for two minutes; restarting radio')
    # The legacy wifi tool accepts a UCI radio name. Always attempt the up step,
    # including after a failure/timeout bringing the interface down.
    try:
        command(['wifi', 'down', radio], timeout=20)
    finally:
        command(['wifi', 'up', radio], timeout=20)


def main():
    if '--check' in sys.argv:
        state, radio = snapshot()
        print('Wi-Fi watchdog uplink state: ' + state)
        return
    recovery = Recovery()
    previous = None
    while True:
        try:
            state, radio = snapshot()
            if state != previous:
                report('Wi-Fi uplink state: ' + state)
                previous = state
            recovery.tick(state, radio, monotonic(), restart_radio)
        except Exception as error:
            # Do not include command output, addresses, credentials, or URLs.
            if previous != 'error':
                try:
                    report('Wi-Fi watchdog check failed: ' + type(error).__name__)
                except Exception:
                    pass
            previous = 'error'
            recovery.tick('unknown', None, monotonic(), restart_radio)
        time.sleep(30)


if __name__ == '__main__':
    main()
