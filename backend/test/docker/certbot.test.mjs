import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';

// Exercise the real Compose mounts: an image-only test misses package-directory shadowing.
test('Certbot imports and the deploy hook runs with the actual Compose bind mount', t => {
  const project = 'res-ac-certbot-test-' + randomBytes(6).toString('hex');
  const compose = ['compose', '-f', fileURLToPath(new URL('../../compose.yaml', import.meta.url)), '-p', project];
  const docker = (...args) => execFileSync('docker', args, { encoding: 'utf8', timeout: 60_000, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => {
    // This unique project has no real certificates or user data.
    docker(...compose, 'down', '--volumes', '--remove-orphans');
  });
  const version = docker(...compose, 'run', '--rm', '--no-deps', '--entrypoint', 'certbot', 'certbot', '--version');
  assert.match(version, /certbot 5\.9\.0/);
  const code = `import os, runpy, stat
os.makedirs('/tmp/test-certificate')
for name in ('fullchain.pem', 'privkey.pem'):
    with open('/tmp/test-certificate/' + name, 'w') as output:
        output.write('disposable publication fixture')
os.environ['RENEWED_LINEAGE'] = '/tmp/test-certificate'
runpy.run_path('/opt/sensor-backend-hooks/publish.py')
for name in ('fullchain.pem', 'privkey.pem'):
    path = '/run/sensor-tls/' + name
    assert open(path).read() == 'disposable publication fixture'
    assert os.stat(path).st_uid == 1000
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
print('Certificate publication permissions verified')`;
  const published = docker(...compose, 'run', '--rm', '--no-deps', '--entrypoint', 'python', 'certbot', '-c', code);
  assert.match(published, /Certificate publication permissions verified/);
});
