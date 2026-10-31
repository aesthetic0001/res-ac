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


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--config', type=Path, default=root / 'private' / 'device.json')
    parser.add_argument('--no-start', action='store_true', help='Install/verify only; leave the service stopped')
    parser.add_argument('--modern-curl', action='store_true',
                        help='Install a separate static MIPS TLS client for legacy firmware/IP certificates')
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error('Run scripts/configure_device.py first, or provide a private --config file.')
    if args.config.stat().st_mode & 0o077:
        parser.error('Private configuration must have mode 600.')
    data = args.config.read_bytes()
    config = json.loads(data)
    curl_gzip = modern_curl() if args.modern_curl else None
    if curl_gzip:
        config['curl_binary'] = '/tmp/res-ac-curl'
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
        if curl_gzip:
            # Only a compressed copy occupies flash. The executable lives in RAM.
            existing = console.run('md5sum /usr/lib/res-ac/curl.gz 2>/dev/null; true')
            if hashlib.md5(curl_gzip).hexdigest().encode() not in existing:
                last_percent = [-10]
                def progress(done, total):
                    percent = done * 100 // total
                    if percent >= last_percent[0] + 5:
                        print('TLS client transfer: %d%%' % percent, flush=True)
                        last_percent[0] = percent
                console.upload(curl_gzip, '/usr/lib/res-ac/curl.gz', progress=progress)
            console.run('gzip -dc /usr/lib/res-ac/curl.gz > /tmp/res-ac-curl.new && '
                        'chmod 700 /tmp/res-ac-curl.new && mv /tmp/res-ac-curl.new /tmp/res-ac-curl', timeout=30)
            console.run('/tmp/res-ac-curl --version')
        console.upload((root / 'device/res_ac_agent.py').read_bytes(), '/usr/lib/res-ac/res_ac_agent.py')
        console.upload((root / 'device/dhcp_route_repair.py').read_bytes(), '/usr/lib/res-ac/dhcp_route_repair.py')
        console.upload(bundle, '/etc/res-ac/ca-bundle.pem', 0o644)
        # Probe the staged private config before replacing an existing working one.
        staged = '/etc/res-ac/agent.pending.json'
        console.upload(data, staged)
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        console.run('date -u -s ' + shlex.quote(timestamp) + ' && date +%s > /etc/eduroam-clock')
        probe = console.run('python -u /usr/lib/res-ac/res_ac_agent.py --config ' + staged + ' --probe', timeout=35)
        if b'PASS: verified HTTPS' not in probe:
            raise RuntimeError('The board HTTPS/device-token preflight did not pass. Firmware has not been changed.')
        print('Verified trusted HTTPS and device authentication; no synthetic reading was inserted.')
        console.run('if test -x /etc/init.d/res-ac-agent; then /etc/init.d/res-ac-agent stop; fi; true')
        console.run('mv /etc/res-ac/agent.pending.json /etc/res-ac/agent.json; chmod 600 /etc/res-ac/agent.json')
        console.upload((root / 'device/res-ac-agent.init').read_bytes(), '/etc/init.d/res-ac-agent', 0o755)
        console.run('/etc/init.d/res-ac-agent enable')
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
