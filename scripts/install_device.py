#!/usr/bin/env python3
"""Install and preflight the Linux sensor agent before flashing the AVR firmware."""
import argparse
import datetime
import gzip
import hashlib
import io
import shlex
import json
import os
from pathlib import Path
import sys
import tarfile
from urllib.request import urlopen
from serial_console import Console


# Separate, reproducible TLS client for legacy PolarSSL IP-SAN incompatibility.
# Upstream source/build workflow: https://github.com/stunnel/static-curl
CURL_RELEASE = '8.23.0-1'
CURL_ARCHIVE_SHA256 = '3a8cccb00390da9e31e4397929d8cb604bd9e437148281df2ed923ccdd94ab5b'


def modern_curl():
    url = ('https://github.com/stunnel/static-curl/releases/download/' + CURL_RELEASE +
           '/curl-linux-mips-musl-' + CURL_RELEASE + '.tar.xz')
    with urlopen(url, timeout=60) as response:
        archive = response.read(16 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != CURL_ARCHIVE_SHA256:
        raise RuntimeError('TLS client archive checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        binary = package.extractfile('curl').read(16 * 1024 * 1024)
    return gzip.compress(binary, compresslevel=9, mtime=0)


def http_helper(path):
    binary = path.read_bytes()
    # The board needs big-endian ELF32 MIPS, with a fixed-address static build.
    if (len(binary) < 52 or binary[:6] != b'\x7fELF\x01\x02' or
            binary[16:20] != b'\x00\x02\x00\x08'):
        raise ValueError('HTTP helper must be a static big-endian ELF32 MIPS executable')
    offset = int.from_bytes(binary[28:32], 'big')
    size = int.from_bytes(binary[42:44], 'big')
    count = int.from_bytes(binary[44:46], 'big')
    if not count or size < 32 or offset + size * count > len(binary):
        raise ValueError('HTTP helper has an invalid ELF program table')
    if any(int.from_bytes(binary[offset + i * size:offset + i * size + 4], 'big') == 3 for i in range(count)):
        raise ValueError('HTTP helper must not require a dynamic loader')
    return gzip.compress(binary, compresslevel=9, mtime=0)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--config', type=Path, default=root / 'private' / 'device.json')
    parser.add_argument('--no-start', action='store_true', help='Install/verify only; leave the service stopped')
    parser.add_argument('--modern-curl', action='store_true',
                        help='Legacy compatibility option; the persistent helper includes modern TLS')
    parser.add_argument('--http-helper', type=Path, default=root / 'private/http-build/res-ac-http-mips',
                        help='MIPS helper built locally by scripts/build_http_transport.py')
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error('Run scripts/configure_device.py first, or provide a private --config file.')
    if args.config.stat().st_mode & 0o077:
        parser.error('Private configuration must have mode 600.')
    data = args.config.read_bytes()
    config = json.loads(data)
    if not args.http_helper.is_file():
        parser.error('Build scripts/build_http_transport.py first, or provide --http-helper.')
    helper_gzip = http_helper(args.http_helper)
    config['http_helper'] = '/tmp/res-ac-http'
    config.pop('curl_binary', None)
    data = (json.dumps(config, indent=2) + '\n').encode()
    if config.get('ca_file') != '/etc/res-ac/ca-bundle.pem':
        parser.error('This installer expects ca_file=/etc/res-ac/ca-bundle.pem.')
    # Download public CA certificates using the host's verified HTTPS trust store.
    # The certificate bytes are intentionally not kept in the public repository.
    bundle = b''
    for name in ['isrgrootx1.pem', 'isrg-root-x2.pem']:
        with urlopen('https://letsencrypt.org/certs/' + name, timeout=20) as response:
            cert = response.read(16384)
        if b'-----BEGIN CERTIFICATE-----' not in cert:
            raise RuntimeError('Unexpected CA certificate response')
        bundle += cert + b'\n'
    console = Console(args.port)
    try:
        console.run('mkdir -p /usr/lib/res-ac /etc/res-ac; chmod 700 /etc/res-ac')
        # Stage and preflight in RAM before replacing the existing TLS client.
        console.upload(helper_gzip, '/tmp/res-ac-http.pending.gz')
        console.run('gzip -dc /tmp/res-ac-http.pending.gz > /tmp/res-ac-http.pending && '
                    'chmod 700 /tmp/res-ac-http.pending', timeout=30)
        version = console.run('/tmp/res-ac-http.pending --version')
        if b'res-ac-http protocol=1 ' not in version:
            raise RuntimeError('Unexpected HTTP helper protocol')
        agent_source = (root / 'device/res_ac_agent.py').read_bytes()
        console.upload(agent_source, '/tmp/res_ac_agent.pending.py')
        console.upload(bundle, '/etc/res-ac/ca-bundle.pem', 0o644)
        # Probe the staged private config before replacing an existing working one.
        staged = '/etc/res-ac/agent.pending.json'
        probe_config = dict(config, http_helper='/tmp/res-ac-http.pending')
        console.upload((json.dumps(probe_config) + '\n').encode(), staged)
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        console.run('date -u -s ' + shlex.quote(timestamp) + ' && date +%s > /etc/eduroam-clock')
        probe = console.run('python -u /tmp/res_ac_agent.pending.py --config ' + staged + ' --probe', timeout=35)
        if b'PASS: verified HTTPS' not in probe:
            raise RuntimeError('The board HTTPS/device-token preflight did not pass. Firmware has not been changed.')
        print('Verified trusted HTTPS and device authentication; no synthetic reading was inserted.')
        console.run('if test -x /etc/init.d/res-ac-agent; then /etc/init.d/res-ac-agent stop; fi; true')
        # Reuse the previous compressed TLS archive's flash slot, rather than
        # storing two large static TLS binaries. Startup expands it as the helper.
        existing = console.run('md5sum /usr/lib/res-ac/curl.gz 2>/dev/null; true')
        if hashlib.md5(helper_gzip).hexdigest().encode() not in existing:
            console.upload(helper_gzip, '/usr/lib/res-ac/curl.gz')
        console.run('mv /tmp/res-ac-http.pending /tmp/res-ac-http && '
                    'rm -f /tmp/res-ac-http.pending.gz /tmp/res_ac_agent.pending.py '
                    '/tmp/res-ac-curl /tmp/res-ac-curl.new')
        console.upload(agent_source, '/usr/lib/res-ac/res_ac_agent.py')
        console.upload((root / 'device/dhcp_route_repair.py').read_bytes(), '/usr/lib/res-ac/dhcp_route_repair.py')
        console.upload((root / 'device/network_watchdog.py').read_bytes(), '/usr/lib/res-ac/network_watchdog.py')
        console.upload(data, staged)
        console.run('mv /etc/res-ac/agent.pending.json /etc/res-ac/agent.json; chmod 600 /etc/res-ac/agent.json')
        console.upload((root / 'device/res-ac-agent.init').read_bytes(), '/etc/init.d/res-ac-agent', 0o755)
        console.run('/etc/init.d/res-ac-agent enable')
        console.run('if test -x /etc/init.d/res-ac-network; then /etc/init.d/res-ac-network stop; fi; true')
        console.upload((root / 'device/res-ac-network.init').read_bytes(), '/etc/init.d/res-ac-network', 0o755)
        console.run('/etc/init.d/res-ac-network enable')
        # Preserve other user DHCP hooks; the helper only repairs missing routes.
        hook = ('# res-ac DHCP route repair\n'
                'python /usr/lib/res-ac/dhcp_route_repair.py "$1" || :\n')
        console.run('touch /etc/udhcpc.user; chmod 600 /etc/udhcpc.user; '
                    'grep -q "# res-ac DHCP route repair" /etc/udhcpc.user || '
                    '{ umask 077; printf %s ' + shlex.quote(hook + '\n') +
                    ' > /etc/udhcpc.user.res-ac.tmp && cat /etc/udhcpc.user >> /etc/udhcpc.user.res-ac.tmp && '
                    'mv /etc/udhcpc.user.res-ac.tmp /etc/udhcpc.user; }')
        if not args.no_start:
            console.run('/etc/init.d/res-ac-agent start')
            console.run('/etc/init.d/res-ac-network start')
        print('Agent installed and enabled for boot. Private configuration was not printed.')
        print('The Bridge collector becomes active when the reporting firmware starts.')
    finally:
        console.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # HTTP/network exceptions and serial errors must not accidentally print secrets.
        print('Installation failed: ' + type(error).__name__, file=sys.stderr)
        if getattr(error, 'safe_output', ''):
            print(error.safe_output, file=sys.stderr)
        print('Firmware was not flashed by this installer. Check the board preflight before uploading.', file=sys.stderr)
        sys.exit(1)
