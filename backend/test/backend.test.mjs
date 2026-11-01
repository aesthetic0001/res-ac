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

test('fan remote buttons queue short-lived commands and expose transmission results without inferred state', async t => {
  const f = await fixture(t), auth = await f.login();
  const sessionId = randomBytes(16).toString('hex');
  f.store.setFanState('board-1', { sessionId, calibrated: true, power: 'off', speed: 0, revision: 1 });
  for (const button of ['power', 'speed_down', 'speed_up']) {
    const response = await f.request('/api/admin/devices/board-1/commands', {
      method: 'POST', headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'fan.command', payload: { button, sessionId }, ttlSeconds: 30 })
    });
    assert.equal(response.status, 201);
    const queued = await response.json();
    const stored = f.store.command(queued.id);
    assert.equal(stored.expires_at - stored.created_at, 30_000);
    const delivered = await (await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).json();
    assert.deepEqual(delivered.commands[0].payload, { button, sessionId });
    assert.equal(delivered.commands[0].type, 'fan.command');
    const result = { reply: 'ir_sent', fanStateConfirmed: false };
    assert.equal((await f.request(`/api/devices/board-1/commands/${queued.id}/result`, {
      method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({ status: 'succeeded', result })
    })).status, 200);
    const state = await (await f.request('/api/admin/state', { headers: { cookie: auth.cookie } })).json();
    assert.deepEqual(state.commands.find(command => command.id === queued.id).result, result);
  }
});

test('calibration gates remote requests, is session-bound, and stale telemetry cannot unlock controls', async t => {
  const f = await fixture(t), auth = await f.login();
  const sessionId = randomBytes(16).toString('hex');
  const queue = (type, payload) => f.request('/api/admin/devices/board-1/commands', { method: 'POST',
    headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' }, body: JSON.stringify({ type, payload }) });
  const report = state => f.request('/api/devices/board-1/fan-state', { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify(state) });
  assert.equal((await queue('fan.command', { button: 'power', sessionId })).status, 409);
  const state = { sessionId, calibrated: false, power: 'unknown', speed: null, revision: 1 };
  assert.equal((await report(state)).status, 200);
  assert.equal((await queue('fan.command', { button: 'power', sessionId })).status, 409);
  assert.equal((await queue('ir.send', { protocol: 'NEC', address: 56960, command: 0, sessionId })).status, 409);
  assert.equal((await queue('fan.calibrate', { confirmOff: false, sessionId })).status, 400);
  assert.equal((await queue('fan.calibrate', { confirmOff: true, sessionId: 'b'.repeat(32) })).status, 409);
  assert.equal((await queue('fan.calibrate', { confirmOff: true, sessionId })).status, 201);
  // Merely enqueuing confirmation is not calibration: only device telemetry unlocks.
  assert.equal((await queue('fan.command', { button: 'power', sessionId })).status, 409);
  assert.equal((await report({ ...state, calibrated: true, power: 'off', speed: 0, revision: 2 })).status, 200);
  assert.equal((await report(state)).status, 409);
  assert.equal((await queue('fan.command', { button: 'power', sessionId })).status, 201);
  f.store.db.prepare('UPDATE fan_states SET received_at=?').run(Date.now()-31000);
  assert.equal((await queue('fan.command', { button: 'power', sessionId })).status, 409);
  assert.equal((await report({ ...state, sessionId: 'c'.repeat(32) })).status, 200);
  assert.equal((await queue('fan.calibrate', { confirmOff: true, sessionId })).status, 409);
  assert.equal((await report({ ...state, calibrated: true, power: 'off', speed: 3 })).status, 400);
  assert.equal((await f.request('/api/devices/board-2/fan-state', { method: 'POST', headers: f.deviceHeaders, body: JSON.stringify(state) })).status, 401);
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

test('cyclic target levels and temperature curves validate inputs and remain calibration/session gated', async t => {
  const f=await fixture(t),auth=await f.login(),sessionId=randomBytes(16).toString('hex');
  const settings={enabled:true,thresholdsC:[22,24,26,28,30],hysteresisC:1};
  const queue=(type,payload)=>f.request('/api/admin/devices/board-1/commands',{method:'POST',headers:{cookie:auth.cookie,'x-csrf-token':auth.csrf,'content-type':'application/json'},body:JSON.stringify({type,payload})});
  const state={sessionId,calibrated:false,power:'unknown',speed:null,revision:1,firmwareVersion:'v4'};
  f.store.setFanState('board-1',state);
  assert.equal((await queue('fan.level',{level:3,sessionId})).status,409);
  assert.equal((await queue('fan.curve',{...settings,sessionId})).status,409);
  assert.equal((await queue('fan.curve',{...settings,enabled:false,sessionId})).status,201);
  f.store.setFanState('board-1',{...state,calibrated:true,power:'off',speed:0,revision:2});
  for(const level of [0,1,2,3,4,5]) assert.equal((await queue('fan.level',{level,sessionId})).status,201);
  for(const level of [-1,6,2.5,true]) assert.equal((await queue('fan.level',{level,sessionId})).status,400);
  assert.equal((await queue('fan.curve',{...settings,sessionId})).status,201);
  for(const thresholdsC of [[22,21,26,28,30],[22,24,26,28],[22,24,26,28,61]])
    assert.equal((await queue('fan.curve',{...settings,thresholdsC,sessionId})).status,400);
  assert.equal((await queue('fan.curve',{...settings,hysteresisC:0,sessionId})).status,400);
  assert.equal((await queue('fan.curve',{...settings,sessionId:'b'.repeat(32)})).status,409);
  f.store.setFanState('board-1',{...state,firmwareVersion:'v3',revision:3});
  assert.equal((await queue('fan.curve',{...settings,enabled:false,sessionId})).status,409);
});

test('curve APIs and live telemetry accept ties and small gaps while rejecting decreasing temperatures', async t => {
  const f=await fixture(t),auth=await f.login(),sessionId=randomBytes(16).toString('hex');
  const state={sessionId,calibrated:true,power:'off',speed:0,revision:1,firmwareVersion:'v4'};
  f.store.setFanState('board-1',state);
  for(const thresholdsC of [[22,24,24,28,30],[24,24,24,24,24],[22,22.1,22.1,28,30]]) {
    const settings={enabled:true,thresholdsC,hysteresisC:1};
    const queued=await f.request('/api/admin/devices/board-1/commands',{method:'POST',headers:{cookie:auth.cookie,'x-csrf-token':auth.csrf,'content-type':'application/json'},body:JSON.stringify({type:'fan.curve',payload:{...settings,sessionId}})});
    assert.equal(queued.status,201);
    assert.deepEqual(JSON.parse(f.store.command((await queued.json()).id).payload).thresholdsC,thresholdsC);
    const response=await f.request('/api/devices/board-1/fan-state',{method:'POST',headers:f.deviceHeaders,body:JSON.stringify({...state,automatic:{...settings,status:'active',targetLevel:5,minAdjustmentSeconds:30}})});
    assert.equal(response.status,200);
    assert.deepEqual(f.store.fanState('board-1').automatic.thresholdsC,thresholdsC);
  }
});

test('live automatic settings and target are persisted independently of sensor readings', async t => {
  const f=await fixture(t),auth=await f.login();
  const value={sessionId:randomBytes(16).toString('hex'),calibrated:true,power:'on',speed:3,revision:1,firmwareVersion:'v4',controlSource:'automatic',
    automatic:{enabled:true,thresholdsC:[22,24,26,28,30],hysteresisC:1,status:'active',targetLevel:3,minAdjustmentSeconds:30}};
  const report=body=>f.request('/api/devices/board-1/fan-state',{method:'POST',headers:f.deviceHeaders,body:JSON.stringify(body)});
  assert.equal((await report(value)).status,200);
  const state=await (await f.request('/api/admin/state',{headers:{cookie:auth.cookie}})).json();
  assert.deepEqual(state.devices[0].fanState.automatic,value.automatic);
  assert.equal(state.devices[0].fanState.controlSource,'automatic');
  assert.equal(f.store.db.prepare('SELECT count(*) AS n FROM readings').get().n,0);
  assert.equal((await report({...value,automatic:{...value.automatic,targetLevel:6}})).status,400);
  assert.equal((await report({...value,automatic:{...value.automatic,status:'made_up'}})).status,400);
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

test('reading history requires a web session, validates ranges, and handles empty or unknown devices', async t => {
  const f = await fixture(t);
  const path = '/api/admin/devices/board-1/readings';
  assert.equal((await f.request(path)).status, 401);
  assert.equal((await f.request(path, { headers: f.deviceHeaders })).status, 401);
  const auth = await f.login(), headers = { cookie: auth.cookie };
  const response = await f.request(path, { headers });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const empty = await response.json();
  assert.deepEqual(empty.readings, []);
  assert.equal(empty.sampleCount, 0);
  assert.equal(empty.to - empty.from, 24 * 3600_000);
  for (const hours of ['0', '-1', '2', '169', 'NaN', '1.5', '']) {
    assert.equal((await f.request(`${path}?hours=${hours}`, { headers })).status, 400);
  }
  for (const hours of [1, 6, 24, 168]) {
    assert.equal((await f.request(`${path}?hours=${hours}`, { headers })).status, 200);
  }
  assert.equal((await f.request('/api/admin/devices/missing/readings', { headers })).status, 404);
});

test('reading history averages the full window into bounded chronological points and isolates devices', async t => {
  const f = await fixture(t), now = Date.now(), from = now - 3600_000;
  const insert = f.store.db.prepare('INSERT INTO readings(device_id,reading_id,temperature_c,humidity,recorded_at,received_at) VALUES(?,?,?,?,?,?)');
  f.store.db.exec('BEGIN');
  for (let i = 0; i < 3600; i++) {
    // A bad device clock must not put otherwise valid readings outside this view.
    insert.run('board-1', `history-${i}`, i % 30, 40 + i % 10, '2000-01-01T00:00:00Z', from + i * 1000);
  }
  insert.run('board-1', 'too-old', 90, 90, null, from - 1);
  insert.run('board-1', 'future', 90, 90, null, now + 60_000);
  insert.run('board-2', 'other-device', 90, 90, null, from + 1000);
  f.store.db.exec('COMMIT');
  const history = f.store.readingHistory('board-1', 1, now);
  assert.equal(history.readings.length, 600);
  assert.equal(history.sampleCount, 3600);
  assert.equal(history.readings[0].sampleCount, 6);
  assert.equal(history.readings[0].temperatureC, 2.5);
  assert.equal(history.readings[0].humidity, 42.5);
  assert.ok(history.readings.every((point, i, rows) => point.receivedAt >= from && point.receivedAt < now && (!i || point.receivedAt > rows[i - 1].receivedAt)));
  const auth = await f.login();
  const response = await f.request('/api/admin/devices/board-1/readings?hours=1', { headers: { cookie: auth.cookie } });
  const result = await response.json();
  assert.equal(response.status, 200);
  assert.ok(result.readings.length <= 600);
  assert.ok(result.sampleCount > 3500);
  assert.ok(result.readings.every(point => point.temperatureC < 30 && point.humidity < 50));
});
