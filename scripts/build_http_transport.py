#!/usr/bin/env python3
"""Build the persistent HTTPS helper locally; never contacts the device.

Python 3.12+ is required for safe archive extraction. Default output is ignored
under private/http-build/. --target native builds a Linux test helper using the
host's shared libcurl; the default MIPS build is static for the legacy board.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
from urllib.request import urlopen

SDK_URL = 'https://github.com/stunnel/static-curl/releases/download/8.23.0-1/curl-linux-mips-dev-8.23.0-1.tar.xz'
SDK_SHA256 = '591064758005d09dabd7f55940aef75fbadf682b439fd23274eaebfe6ae59660'
TOOLCHAIN_URL = 'https://github.com/userdocs/qbt-musl-cross-make/releases/download/2644/x86_64-mips-linux-musl.tar.xz'
TOOLCHAIN_SHA256 = 'f8ffbb60b3e5c1d6a08148be466a0f24a0de85b8c3edf7e467b997df999d2973'


def dependency(cache, name, url, digest, destination):
    archive = cache / name
    if not archive.exists():
        with urlopen(url, timeout=60) as response:
            data = response.read(64 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('Download checksum mismatch: ' + name)
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Cached checksum mismatch: ' + name)
    marker = destination / '.verified-sha256'
    if not marker.exists() or marker.read_text() != digest:
        with tarfile.open(archive) as package:
            for member in package:
                # The static compiler does not need its absolute dynamic-loader
                # symlink. All extracted entries still pass tarfile's data filter.
                if member.issym() and member.linkname.startswith('/'):
                    continue
                package.extract(member, destination, filter='data')
        marker.write_text(digest)


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['mips', 'native'], default='mips')
    parser.add_argument('--cache-dir', type=Path, default=root / 'private/http-build')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output = (args.output or cache / ('res-ac-http-' + args.target)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    dependency(cache, 'curl-mips-dev.tar.xz', SDK_URL, SDK_SHA256, cache / 'sdk')
    sdk = cache / 'sdk/curl-mips'
    flags = ['-std=c99', '-Os', '-Wall', '-Wextra', '-Werror', '-ffunction-sections',
             '-fdata-sections', '-Wl,--gc-sections', '-Wl,--build-id=none', '-I' + str(sdk / 'include')]
    source = root / 'device/http_transport.c'
    if args.target == 'mips':
        dependency(cache, 'mips-toolchain.tar.xz', TOOLCHAIN_URL, TOOLCHAIN_SHA256, cache / 'toolchain')
        prefix = cache / 'toolchain/mips-linux-musl/bin/mips-linux-musl-'
        compiler, strip = str(prefix) + 'cc', str(prefix) + 'strip'
        flags += ['-march=mips32', '-msoft-float', '-fno-pie', '-no-pie', '-static']
        libraries = ['-Wl,--start-group'] + [str(p) for p in sorted((sdk / 'lib').glob('*.a'))]
        libraries += ['-Wl,--end-group', '-pthread', '-ldl', '-lm', '-latomic']
    else:
        compiler, strip = 'cc', 'strip'
        libraries = ['-Wl,-l:libcurl.so.4']
    subprocess.run([compiler] + flags + [str(source)] + libraries + ['-o', str(output)], check=True)
    subprocess.run([strip, str(output)], check=True)
    manifest = dict(target=args.target, sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                    source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), sdk_sha256=SDK_SHA256,
                    toolchain_sha256=TOOLCHAIN_SHA256 if args.target == 'mips' else None)
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built ' + str(output))
    if args.target == 'native':
        subprocess.run([str(output), '--version'], check=True)


if __name__ == '__main__':
    main()
