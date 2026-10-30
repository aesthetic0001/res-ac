import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import https from 'node:https';
import tls from 'node:tls';
import { setTimeout as sleep } from 'node:timers/promises';
import { readIdentity, watchIdentity } from '../src/tls.mjs';

test('HTTPS keeps its current certificate during partial renewal and reloads a matching new pair without restart', async t => {
  const directory = mkdtempSync(join(tmpdir(), 'sensor-tls-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const make = name => {
    const cert = join(directory, `${name}.pem`), key = join(directory, `${name}.key`);
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', key, '-out', cert], { stdio: 'ignore' });
    return { cert: readFileSync(cert), key: readFileSync(key) };
  };
  const first = make('first'), next = make('next');
  writeFileSync(join(directory, 'fullchain.pem'), first.cert); writeFileSync(join(directory, 'privkey.pem'), first.key);
  const server = https.createServer(readIdentity(directory).identity, (_, res) => res.end('ok'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const logs = [], stop = watchIdentity(server, directory, { intervalMs: 15, log: { info: (...args) => logs.push(args.join(' ')), error() {} } });
  t.after(async () => { stop(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const fingerprint = () => new Promise((resolve, reject) => {
    const socket = tls.connect({ host: '127.0.0.1', port: server.address().port, rejectUnauthorized: false }, () => {
      const value = socket.getPeerCertificate().fingerprint256; socket.end(); resolve(value);
    });
    socket.on('error', reject);
  });
  const old = await fingerprint();
  writeFileSync(join(directory, 'fullchain.pem'), next.cert);
  await sleep(50);
  assert.equal(await fingerprint(), old, 'A mismatched intermediate pair must not replace the live TLS context');
  writeFileSync(join(directory, 'privkey.pem'), next.key);
  let renewed = old;
  for (let i = 0; i < 30 && renewed === old; i++) { await sleep(20); renewed = await fingerprint(); }
  assert.notEqual(renewed, old);
  assert.ok(logs.some(line => line.includes('Renewed TLS certificate loaded')));
});
