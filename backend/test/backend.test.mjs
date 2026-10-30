import { test, before } from 'node:test';
import { randomBytes } from 'node:crypto';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { Store } from '../src/store.mjs';
import { createApp } from '../src/app.mjs';
import { hashPassword } from '../src/auth.mjs';
import { publicIp } from '../deploy/setup.mjs';

let passwordHash;
// Ephemeral test-only credentials: nothing usable is embedded in the repository.
const testPassword = randomBytes(24).toString('base64url');
const rotatedPassword = randomBytes(24).toString('base64url');
const firstDeviceToken = randomBytes(32).toString('base64url');
const secondDeviceToken = randomBytes(32).toString('base64url');
before(async () => { passwordHash = await hashPassword(testPassword); });
async function fixture(t, overrides = {}) {
  const config = { httpDev: true, adminUsername: 'admin', passwordHash, sessionMs: 60_000,
    pollWaitMs: 100, leaseMs: 60, devices: [{ id: 'board-1', token: firstDeviceToken }, { id: 'board-2', token: secondDeviceToken }], ...overrides };
  const directory = mkdtempSync(join(tmpdir(), 'sensor-test-'));
  const path = join(directory, 'state.sqlite');
  const store = new Store(path, config.devices);
  const app = createApp(config, store);
  const server = http.createServer(app.handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => { app.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); store.close(); rmSync(directory, { recursive: true, force: true }); });
  async function request(path, options = {}) {
    return fetch(base + path, { ...options, headers: { origin: base, ...options.headers } });
  }
  async function login() {
    const response = await request('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: 'admin', password: testPassword }) });
    assert.equal(response.status, 200);
    const cookie = response.headers.get('set-cookie').split(';')[0];
    const state = await (await request('/api/admin/state', { headers: { cookie } })).json();
    return { cookie, csrf: state.csrf };
  }
  async function enqueue(auth, payload = {}) {
    const response = await request('/api/admin/devices/board-1/commands', { method: 'POST', headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' }, body: JSON.stringify({ type: 'device.ping', payload, ttlSeconds: 60 }) });
    assert.equal(response.status, 201); return response.json();
  }
  const deviceHeaders = { authorization: `Bearer ${firstDeviceToken}`, 'content-type': 'application/json' };
  return { request, login, enqueue, config, store, path, deviceHeaders };
}

test('login gates dashboard/API; sessions, CSRF, and logout work', async t => {
  const f = await fixture(t);
  assert.equal((await f.request('/', { redirect: 'manual' })).status, 303);
  assert.equal((await f.request('/api/admin/state')).status, 401);
  const incorrect = await f.request('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ username: 'admin', password: 'incorrect' }) });
  assert.equal(incorrect.status, 401);
  const auth = await f.login();
  assert.equal((await f.request('/', { headers: { cookie: auth.cookie } })).status, 200);
  assert.equal((await f.request('/api/admin/devices/board-1/commands', { method: 'POST', headers: { cookie: auth.cookie, 'content-type': 'application/json' }, body: '{}' })).status, 403);
  assert.equal((await f.request('/api/logout', { method: 'POST', headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, origin: 'http://other.example', 'content-type': 'application/json' }, body: '{}' })).status, 403);
  assert.equal((await f.request('/api/logout', { method: 'POST', headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' }, body: '{}' })).status, 200);
  assert.equal((await f.request('/api/admin/state', { headers: { cookie: auth.cookie } })).status, 401);
});

test('device tokens are scoped and readings are validated and deduplicated', async t => {
  const f = await fixture(t);
  assert.equal((await f.request('/api/devices/board-1/commands')).status, 401);
  assert.equal((await f.request('/api/devices/board-2/commands', { headers: f.deviceHeaders })).status, 401);
  const post = value => f.request('/api/devices/board-1/readings', { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify(value) });
  assert.equal((await post({ temperatureC: '20', humidity: 50 })).status, 400);
  assert.equal((await post({ temperatureC: 20, humidity: 101 })).status, 400);
  const reading = { readingId: 'sample-1', temperatureC: 21.5, humidity: 48 };
  assert.equal((await post(reading)).status, 201);
  assert.equal((await post(reading)).status, 200);
  const auth = await f.login();
  const snapshot = await (await f.request('/api/admin/state', { headers: { cookie: auth.cookie } })).json();
  assert.equal(snapshot.devices[0].latest.temperatureC, 21.5);
  assert.equal(f.store.db.prepare('SELECT count(*) AS n FROM readings').get().n, 1);
});

test('long polling returns 204 when empty and wakes immediately for new commands', async t => {
  const f = await fixture(t, { pollWaitMs: 1600 });
  const auth = await f.login();
  f.config.pollWaitMs = 40;
  const start = Date.now();
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 204);
  assert.ok(Date.now() - start >= 25);
  f.config.pollWaitMs = 1600;
  const waiting = f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders });
  await sleep(30);
  const queued = await f.enqueue(auth, { value: 'test' });
  const response = await waiting;
  assert.equal(response.status, 200);
  assert.equal((await response.json()).commands[0].id, queued.id);
});

test('a command is leased, redelivered after lease expiry, and results are idempotent', async t => {
  const f = await fixture(t);
  const auth = await f.login(), queued = await f.enqueue(auth);
  const first = await (await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).json();
  assert.equal(first.commands[0].id, queued.id);
  await sleep(75);
  const again = await (await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).json();
  assert.equal(again.commands[0].id, queued.id);
  const report = (status, result, id = 'board-1', headers = f.deviceHeaders) => f.request(`/api/devices/${id}/commands/${queued.id}/result`, { method: 'POST', headers, body: JSON.stringify({ status, result }) });
  assert.equal((await report('succeeded', { b: 2, a: 1 }, 'board-2', { ...f.deviceHeaders, authorization: `Bearer ${secondDeviceToken}` })).status, 404);
  assert.equal((await report('succeeded', { b: 2, a: 1 })).status, 200);
  assert.equal((await (await report('succeeded', { a: 1, b: 2 })).json()).duplicate, true);
  assert.equal((await report('failed', {})).status, 409);
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 204);
});

test('undelivered and expired commands cannot be acknowledged or delivered', async t => {
  const f = await fixture(t), auth = await f.login(), queued = await f.enqueue(auth);
  const report = () => f.request(`/api/devices/board-1/commands/${queued.id}/result`, { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({ status: 'succeeded' }) });
  assert.equal((await report()).status, 409);
  f.store.db.prepare('UPDATE commands SET expires_at=? WHERE id=?').run(Date.now() - 1, queued.id);
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 204);
  assert.equal(f.store.command(queued.id).status, 'expired');
});

test('readings, queued commands, and sessions survive reopening SQLite; credential rotation revokes old sessions', async t => {
  const f = await fixture(t), auth = await f.login(), queued = await f.enqueue(auth);
  const reopened = new Store(f.path, f.config.devices);
  assert.equal(reopened.command(queued.id).status, 'pending');
  assert.ok(reopened.session(auth.cookie.slice('session='.length)));
  reopened.close();
  f.config.passwordHash = await hashPassword(rotatedPassword);
  // New process means a new app/auth version, pointing at the same database.
  const rotated = createApp(f.config, f.store), server = http.createServer(rotated.handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const response = await fetch(`http://127.0.0.1:${server.address().port}/api/admin/state`, { headers: { cookie: auth.cookie } });
  assert.equal(response.status, 401);
  rotated.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
});

test('aborted polls release their slot and concurrent polls are bounded', async t => {
  const f = await fixture(t, { pollWaitMs: 2000 });
  const controller = new AbortController();
  const first = f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders, signal: controller.signal }).catch(() => null);
  await sleep(30);
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 409);
  controller.abort(); await first; await sleep(30);
  f.config.pollWaitMs = 30;
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 204);
});

test('IP detection excludes private/documentation ranges but accepts real public IPv4/IPv6', () => {
  for (const ip of ['127.0.0.1', '10.0.0.1', '172.16.1.1', '192.168.1.1', '100.64.1.1', '203.0.113.1', '198.51.100.1', '2001:db8::1', '::1', 'fe80::1']) assert.equal(publicIp(ip), false, ip);
  for (const ip of ['1.1.1.1', '198.41.0.4', '203.1.2.3', '2606:4700:4700::1111']) assert.equal(publicIp(ip), true, ip);
});

test('a pending command poll does not block sensor uploads and oversized bodies are rejected', async t => {
  const f = await fixture(t, { pollWaitMs: 150 });
  const waiting = f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders });
  await sleep(20);
  const posted = await f.request('/api/devices/board-1/readings', { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({ temperatureC: 22, humidity: 50 }) });
  assert.equal(posted.status, 201);
  assert.equal((await waiting).status, 204);
  const oversized = await f.request('/api/devices/board-1/readings', { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({ temperatureC: 22, humidity: 50, ignored: 'x'.repeat(40_000) }) });
  assert.equal(oversized.status, 413);
});
