#!/usr/bin/env python3
"""Build the small UART service; use the verified shared MIPS toolchain cache."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from build_http_transport import dependency, TOOLCHAIN_URL, TOOLCHAIN_SHA256


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['mips', 'native'], default='mips')
    parser.add_argument('--cache-dir', type=Path, default=root/'private/http-build')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    cache = args.cache_dir.resolve(); cache.mkdir(parents=True, exist_ok=True)
    output = (args.output or cache/('res-ac-bridge-'+args.target)).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = ['-std=c99','-Os','-Wall','-Wextra','-Werror','-ffunction-sections',
             '-fdata-sections','-Wl,--gc-sections','-Wl,--build-id=none']
    if args.target == 'mips':
        dependency(cache,'mips-toolchain.tar.xz',TOOLCHAIN_URL,TOOLCHAIN_SHA256,cache/'toolchain')
        prefix = str(cache/'toolchain/mips-linux-musl/bin/mips-linux-musl-')
        compiler, strip = prefix+'cc', prefix+'strip'
        flags += ['-march=mips32','-msoft-float','-fno-pie','-no-pie','-static']
    else: compiler, strip = 'cc', 'strip'
    source = root/'device/native_bridge.c'
    subprocess.run([compiler]+flags+[str(source),'-o',str(output)],check=True)
    subprocess.run([strip,str(output)],check=True)
    manifest=dict(target=args.target,sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                  source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  toolchain_sha256=TOOLCHAIN_SHA256 if args.target=='mips' else None)
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('Built '+str(output))
    if args.target=='native':subprocess.run([str(output),'--version'],check=True)


if __name__=='__main__':main()
