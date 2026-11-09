#!/usr/bin/env python
"""Recover a stuck eduroam uplink on the legacy Seeeduino Linux system.

Bounded local gateway probes, no credential changes or board reboots. Run as
an independent service so HTTPS polling and sensor collection remain available.
"""
from __future__ import print_function
import os
import re
import subprocess
import sys
import time

from res_ac_agent import decode_json, HealthHeartbeat
from service_supervisor import read_health
from dhcp_route_repair import repair, default_exists
from wifi_roaming import Roaming, link_health

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


def snapshot(details=None):
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
    if details is not None:
        details.update(device=device, gateways=gateways)
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


class TransportRecovery(object):
    """Escalate prolonged total transport loss without rebooting Linux/AVR."""
    def __init__(self):
        self.next_attempt = 0
        self.cooldown = 300
        self.stage = 0
        self.good_since = None

    def tick(self, state, radio, now, health, renew, restart):
        if state != 'up' or health is None:
            self.good_since = None
            return False
        failed = health['failed_since']
        if failed is None:
            if self.good_since is None:
                self.good_since = now
            if now - self.good_since >= 300:
                self.stage, self.cooldown = 0, 300
            return False
        self.good_since = None
        if now - failed < 300 or now < self.next_attempt:
            return False
        self.next_attempt = now + self.cooldown
        self.cooldown = min(self.cooldown * 2, 1800)
        if self.stage == 0:
            self.stage = 1
            renew()
        else:
            restart(radio)
        return True


def report(message):
    # Keep recovery evidence even if the firmware's tiny shared syslog buffer
    # is overwritten by unrelated vendor services. RAM only; no flash wear.
    path = '/tmp/res-ac-network.log'
    try:
        if os.path.exists(path) and os.path.getsize(path) > 32768:
            with open(path, 'w'):
                pass
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a') as stream:
            stream.write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' ' + message + '\n')
    except (IOError, OSError):
        pass
    command(['logger', '-t', 'res-ac-network', message])


def restart_radio(radio):
    report('Wi-Fi uplink recovery: restarting configured radio')
    # The legacy wifi tool accepts a UCI radio name. Always attempt the up step,
    # including after a failure/timeout bringing the interface down.
    try:
        command(['wifi', 'down', radio], timeout=20)
    finally:
        command(['wifi', 'up', radio], timeout=20)


def renew_uplink():
    # Recheck manual policy immediately before changing the configured uplink.
    state, radio = snapshot()
    if state != 'up':
        return
    report('Sustained device transport loss: restarting configured DHCP uplink')
    try:
        command(['ifdown', 'wwan'], timeout=20)
    finally:
        command(['ifup', 'wwan'], timeout=20)


def transport_restart_radio(radio):
    state, current = snapshot()
    if state == 'up' and current == radio:
        report('Device transport still unavailable: escalating to radio recovery')
        restart_radio(radio)


def roaming_allowed(radio, device):
    # An explicit AP pin is policy. Do not override it, even temporarily.
    if setting('wireless.eduroam.bssid', ''):
        return False
    details = {}
    state, current_radio = snapshot(details)
    return state == 'up' and current_radio == radio and details.get('device') == device


def main():
    os.umask(0o077)
    if '--check' in sys.argv:
        state, radio = snapshot()
        print('Wi-Fi watchdog uplink state: ' + state)
        return
    if '--check-link' in sys.argv:
        details = {}
        state, radio = snapshot(details)
        if state == 'up':
            health = link_health(details['device'], details['gateways'], command)
            print('Wi-Fi watchdog link state: ' + health['state'])
            if 'received' in health:
                print('Gateway replies: %d/%d; Wi-Fi transmit failures during probe: %d' %
                      (health['received'], health['sent'], health['failed']))
        else:
            print('Wi-Fi watchdog uplink state: ' + state)
        return
    recovery = Recovery()
    transport = TransportRecovery()
    heartbeat = HealthHeartbeat('network') if os.environ.get('RESAC_SUPERVISED') == '1' else None
    roaming = Roaming(command, report, restart_radio, roaming_allowed)
    previous = None
    while True:
        try:
            if heartbeat is not None:
                heartbeat.tick()
            details = {}
            state, radio = snapshot(details)
            if state != previous:
                report('Wi-Fi uplink state: ' + state)
                previous = state
            health = read_health('/tmp/res-ac-agent.health', monotonic(), 30)
            recovered = transport.tick(state, radio, monotonic(), health, renew_uplink, transport_restart_radio)
            if recovered:
                roaming.reset_observation()
                time.sleep(30)
                continue
            recovery.tick(state, radio, monotonic(), restart_radio)
            if state == 'up' and not setting('wireless.eduroam.bssid', ''):
                roaming.tick(details['device'], details['gateways'], radio, monotonic())
            else:
                roaming.reset_observation()
        except Exception as error:
            # Do not include command output, addresses, credentials, or URLs.
            if previous != 'error':
                try:
                    report('Wi-Fi watchdog check failed: ' + type(error).__name__)
                except Exception:
                    pass
            previous = 'error'
            recovery.tick('unknown', None, monotonic(), restart_radio)
            roaming.reset_observation()
        time.sleep(30)


if __name__ == '__main__':
    main()
