#!/usr/bin/env python
"""Repair legacy netifd's missing DHCP default route, using the current lease.

Called after bound/renew by /etc/udhcpc.user. No network profile, gateway, or
credentials are embedded here. Match netifd's static route protocol and metric
so it can still remove the route on lease loss or a gateway change.
"""
from __future__ import print_function
import os
import socket
import struct
import subprocess
import sys


def default_exists(device, gateway, metric, path='/proc/net/route'):
    encoded = struct.unpack('=I', socket.inet_aton(gateway))[0]
    with open(path) as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 8 or fields[0] != device:
                continue
            if (fields[1] == '00000000' and fields[7] == '00000000' and
                    int(fields[2], 16) == encoded and int(fields[3], 16) & 1 and
                    int(fields[6]) == metric):
                return True
    return False


def route_message(index, gateway, metric):
    def attribute(kind, data):
        return struct.pack('=HH', len(data) + 4, kind) + data
    # IPv4 /0 in the main table, RTPROT_STATIC (as used by netifd), unicast.
    body = struct.pack('=BBBBBBBBI', socket.AF_INET, 0, 0, 0, 254, 4, 0, 1, 0)
    body += attribute(4, struct.pack('=I', index))  # RTA_OIF
    body += attribute(5, socket.inet_aton(gateway))  # RTA_GATEWAY
    body += attribute(6, struct.pack('=I', metric))  # RTA_PRIORITY
    # RTM_NEWROUTE, REQUEST | ACK | CREATE | EXCL: never replace another route.
    return struct.pack('=IHHII', len(body) + 16, 24, 0x605, 1, 0) + body


def add_default(device, gateway, metric):
    with open('/sys/class/net/' + device + '/ifindex') as stream:
        index = int(stream.read())
    connection = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    try:
        connection.settimeout(2)
        connection.bind((0, 0))
        connection.sendto(route_message(index, gateway, metric), (0, 0))
        response = connection.recv(4096)
        length, kind, flags, sequence, pid = struct.unpack('=IHHII', response[:16])
        if kind != 2 or sequence != 1 or length < 20:
            raise IOError('Unexpected routing acknowledgment')
        error = struct.unpack('=i', response[16:20])[0]
        if error not in (0, -17):  # EEXIST may mean netifd won the race.
            raise OSError(-error, 'Could not restore DHCP route')
    finally:
        connection.close()


def setting(section, option, default):
    request = subprocess.Popen(['uci', '-q', 'get', 'network.' + section + '.' + option],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    value, ignored = request.communicate()
    return value.decode('ascii').strip() if request.returncode == 0 else default


def repair(event, environ=None):
    import re
    env = os.environ if environ is None else environ
    if event not in ('bound', 'renew'):
        return 0
    section, device = env.get('INTERFACE', ''), env.get('interface', '')
    if (not re.match(r'^[A-Za-z0-9_-]+$', section) or
            not re.match(r'^[A-Za-z0-9_.:-]{1,15}$', device)):
        return 0
    if setting(section, 'defaultroute', '1') == '0':
        return 0
    metric = int(setting(section, 'metric', '0'))
    if not 0 <= metric <= 0xffffffff:
        raise ValueError('Invalid interface metric')
    repaired = 0
    for gateway in env.get('router', '').split():
        socket.inet_aton(gateway)  # Validate the DHCP value before using it.
        if gateway in ('0.0.0.0', '255.255.255.255'):
            continue
        if not default_exists(device, gateway, metric):
            add_default(device, gateway, metric)
            if not default_exists(device, gateway, metric):
                raise IOError('DHCP default route is still missing')
            repaired += 1
    return repaired


if __name__ == '__main__':
    try:
        if repair(sys.argv[1] if len(sys.argv) > 1 else ''):
            subprocess.call(['logger', '-t', 'res-ac-network', 'Restored missing DHCP default route'])
    except Exception as error:
        # Never log lease contents, network addresses, or configuration values.
        subprocess.call(['logger', '-t', 'res-ac-network', 'DHCP route repair failed: ' + type(error).__name__])
        sys.exit(1)
