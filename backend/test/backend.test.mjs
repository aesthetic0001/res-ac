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
import { encodeCommand, commandMediaType } from '../src/command-wire.mjs';

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
  const store = new Store(path, config.devices, undefined, { fanHeartbeatCheckpointMs: config.fanHeartbeatCheckpointMs });
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
  f.store.setFanState('board-1', { sessionId, calibrated: true, power: 'on', speed: 1, revision: 1 });
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

test('speed presses require power on while absolute levels can power on first', async t => {
  const f = await fixture(t), auth = await f.login(), sessionId = randomBytes(16).toString('hex');
  const state = { sessionId, calibrated: true, power: 'off', speed: 0, revision: 1, firmwareVersion: 'v4' };
  f.store.setFanState('board-1', state);
  const queue = (type, payload) => f.request('/api/admin/devices/board-1/commands', { method: 'POST',
    headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' }, body: JSON.stringify({ type, payload: { ...payload, sessionId } }) });
  for (const button of ['speed_up', 'speed_down']) assert.equal((await queue('fan.command', { button })).status, 409);
  for (const command of [0x16, 0x14]) assert.equal((await queue('ir.send', { protocol: 'NEC', address: 0xDE80, command })).status, 409);
  assert.equal((await queue('fan.command', { button: 'power' })).status, 201);
  assert.equal((await queue('fan.level', { level: 5 })).status, 201);
  f.store.setFanState('board-1', { ...state, power: 'on', speed: 1, revision: 2 });
  for (const button of ['speed_up', 'speed_down']) assert.equal((await queue('fan.command', { button })).status, 201);
  for (const command of [0x16, 0x14]) assert.equal((await queue('ir.send', { protocol: 'NEC', address: 0xDE80, command })).status, 201);
});

test('HTTP fan heartbeats avoid presence writes while preserving fresh live state and revision validation', async t => {
  let now = Date.now(); t.mock.method(Date, 'now', () => now);
  const f = await fixture(t, { fanHeartbeatCheckpointMs: 60_000 });
  const state = { sessionId: randomBytes(16).toString('hex'), calibrated: true, power: 'off', speed: 0, revision: 1, firmwareVersion: 'v4' };
  const report = value => f.request('/api/devices/board-1/fan-state', {
    method: 'POST', headers: f.deviceHeaders, body: JSON.stringify(value)
  });
  assert.equal((await report(state)).status, 200);
  const changes = f.store.db.prepare('SELECT total_changes() n').get().n;
  const persistedAt = f.store.db.prepare('SELECT received_at FROM fan_states WHERE device_id=?').get('board-1').received_at;
  now += 10_000;
  assert.equal((await report(state)).status, 200);
  assert.equal(f.store.db.prepare('SELECT total_changes() n').get().n, changes);
  assert.equal(f.store.fanState('board-1').receivedAt, now);
  assert.equal(f.store.device('board-1').last_seen, now);
  assert.equal(f.store.db.prepare('SELECT received_at FROM fan_states WHERE device_id=?').get('board-1').received_at, persistedAt);
  assert.equal((await report({ ...state, revision: 2 })).status, 200);
  assert.equal((await report(state)).status, 409);
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

test('target levels and temperature curves validate inputs and remain calibration/session gated', async t => {
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
  assert.equal((await queue('fan.curve',{...settings,hysteresisC:-.1,sessionId})).status,400);
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

test('curve commands and device reports preserve zero, fractional, and uncapped hysteresis', async t => {
  const f=await fixture(t),auth=await f.login(),sessionId=randomBytes(16).toString('hex');
  const state={sessionId,calibrated:true,power:'off',speed:0,revision:1,firmwareVersion:'v4'};
  f.store.setFanState('board-1',state);
  for(const hysteresisC of [0,.1,10,Number.MAX_VALUE]) {
    const settings={enabled:true,thresholdsC:[24.5,24.7,24.7,25,25],hysteresisC};
    const queued=await f.request('/api/admin/devices/board-1/commands',{method:'POST',headers:{cookie:auth.cookie,'x-csrf-token':auth.csrf,'content-type':'application/json'},body:JSON.stringify({type:'fan.curve',payload:{...settings,sessionId}})});
    assert.equal(queued.status,201);
    assert.equal(JSON.parse(f.store.command((await queued.json()).id).payload).hysteresisC,hysteresisC);
    const reported=await f.request('/api/devices/board-1/fan-state',{method:'POST',headers:f.deviceHeaders,body:JSON.stringify({...state,automatic:{...settings,status:'active',targetLevel:0}})});
    assert.equal(reported.status,200);
    assert.equal(f.store.fanState('board-1').automatic.hysteresisC,hysteresisC);
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

for (const format of ['', '?format=binary-v1']) test('enqueue and waiting claim commit atomically before any device response ' + format, async t => {
  const f = await fixture(t, { pollWaitMs: 5000, leaseMs: 10_000 }), auth = await f.login();
  let ready;
  const waiting = new Promise(resolve => { ready = resolve; });
  const claim = f.store.claim.bind(f.store);
  f.store.claim = (...args) => { const result = claim(...args); if (!result) ready(); return result; };
  const poll = f.request('/api/devices/board-1/commands' + format, { headers: f.deviceHeaders });
  await waiting;
  const exec = f.store.db.exec.bind(f.store.db), calls = [];
  let fail = true;
  f.store.db.exec = sql => {
    calls.push(sql);
    if (sql === 'COMMIT' && fail) { fail = false; throw new Error('Injected commit failure'); }
    return exec(sql);
  };
  const post = () => f.request('/api/admin/devices/board-1/commands', { method: 'POST',
    headers: { cookie: auth.cookie, 'x-csrf-token': auth.csrf, 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'device.ping', payload: {}, ttlSeconds: 60 }) });
  assert.equal((await post()).status, 500);
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM commands').get().n, 0);
  assert.deepEqual(calls, ['BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK']);
  assert.equal(await Promise.race([poll.then(() => 'delivered'), sleep(25).then(() => 'waiting')]), 'waiting');
  calls.length = 0;
  const queued = await (await post()).json();
  const response = await poll;
  if (format) {
    assert.equal(response.headers.get('content-type'), commandMediaType);
    const saved = f.store.command(queued.id);
    const expected = { commands: [{ id: saved.id, type: saved.type, payload: JSON.parse(saved.payload),
      createdAt: new Date(saved.created_at).toISOString(), expiresAt: new Date(saved.expires_at).toISOString() }] };
    assert.deepEqual(Buffer.from(await response.arrayBuffer()), encodeCommand(expected));
  } else assert.equal((await response.json()).commands[0].id, queued.id);
  assert.equal(f.store.command(queued.id).status, 'inflight');
  assert.equal(f.store.command(queued.id).attempts, 1);
  assert.deepEqual(calls, ['BEGIN IMMEDIATE', 'COMMIT']);
  assert.equal(f.store.db.prepare('PRAGMA synchronous').get().synchronous, 2);
});

test('presence and result persistence share one commit and retry safely after rollback', async t => {
  const f = await fixture(t), auth = await f.login(), queued = await f.enqueue(auth);
  assert.equal((await f.request('/api/devices/board-1/commands', { headers: f.deviceHeaders })).status, 200);
  const exec = f.store.db.exec.bind(f.store.db), calls = [];
  let fail = true;
  f.store.db.exec = sql => {
    calls.push(sql);
    if (sql === 'COMMIT' && fail) { fail = false; throw new Error('Injected commit failure'); }
    return exec(sql);
  };
  const post = () => f.request(`/api/devices/board-1/commands/${queued.id}/result`, {
    method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({ status: 'succeeded', result: { reply: 'pong' } }) });
  const before = f.store.db.prepare('SELECT last_seen FROM devices WHERE id=?').get('board-1').last_seen;
  const now = Date.now() + 5001;
  t.mock.method(Date, 'now', () => now);
  assert.equal((await post()).status, 500);
  assert.equal(f.store.command(queued.id).status, 'inflight');
  assert.equal(f.store.db.prepare('SELECT last_seen FROM devices WHERE id=?').get('board-1').last_seen, before);
  assert.equal(f.store.device('board-1').last_seen, now);
  assert.deepEqual(calls, ['BEGIN IMMEDIATE', 'COMMIT', 'ROLLBACK']);
  calls.length = 0;
  assert.equal((await post()).status, 200);
  assert.equal(f.store.command(queued.id).status, 'succeeded');
  assert.deepEqual(calls, ['BEGIN IMMEDIATE', 'COMMIT']);
});

test('result handoff commits result, next lease and recovery mapping atomically', async t => {
  const f = await fixture(t, {leaseMs: 10_000}), auth = await f.login();
  const first = await f.enqueue(auth), second = await f.enqueue(auth);
  await f.request('/api/devices/board-1/commands', {headers: f.deviceHeaders});
  const exec = f.store.db.exec.bind(f.store.db), calls = []; let fail = true;
  f.store.db.exec = sql => {
    calls.push(sql);
    if (sql === 'COMMIT' && fail) {fail = false; throw Error('Injected commit failure');}
    return exec(sql);
  };
  const post = () => f.request(`/api/devices/board-1/commands/${first.id}/result?next=1`, {
    method: 'POST', headers: f.deviceHeaders, body: JSON.stringify({status:'succeeded',result:{reply:'pong'}})});
  assert.equal((await post()).status, 500);
  assert.equal(f.store.command(first.id).status, 'inflight');
  assert.equal(f.store.command(second.id).status, 'pending');
  assert.equal(f.store.db.prepare('SELECT count(*) n FROM command_handoffs').get().n, 0);
  assert.deepEqual(calls, ['BEGIN IMMEDIATE','COMMIT','ROLLBACK']); calls.length = 0;
  const accepted = await (await post()).json();
  assert.equal(accepted.resultId, first.id); assert.equal(accepted.commands[0].id, second.id);
  assert.deepEqual(calls, ['BEGIN IMMEDIATE','COMMIT']);
  assert.equal(f.store.command(second.id).attempts, 1);
  // Ignore the first response's command as though it was lost in transit.
  const recovered = await (await post()).json();
  assert.equal(recovered.duplicate, true); assert.deepEqual(recovered.commands, accepted.commands);
  assert.equal(f.store.command(second.id).attempts, 1);
  const binary = await f.request(`/api/devices/board-1/commands/${first.id}/result?next=1&format=binary-v1`, {
    method:'POST',headers:f.deviceHeaders,body:JSON.stringify({status:'succeeded',result:{reply:'pong'}})});
  assert.equal(binary.headers.get('content-type'),commandMediaType);
  assert.deepEqual(Buffer.from(await binary.arrayBuffer()),encodeCommand(recovered));
});

test('handoff recovery survives reopening and never advances a duplicate decision', () => {
  const directory = mkdtempSync(join(tmpdir(),'handoff-')),path=join(directory,'state.sqlite');
  let store = new Store(path,[{id:'board',token:firstDeviceToken}]);
  try {
    const a=store.enqueue('board','device.ping',{},120),b=store.enqueue('board','device.ping',{},120),c=store.enqueue('board','device.ping',{},120);
    store.claim('board',10_000);
    assert.equal(store.resultAndClaim('board',a.id,'succeeded',{reply:'pong'},10_000).commands[0].id,b.id);
    store.close();store=new Store(path);
    assert.equal(store.resultAndClaim('board',a.id,'succeeded',{reply:'pong'},10_000).commands[0].id,b.id);
    assert.equal(store.command(b.id).attempts,1);
    assert.equal(store.resultAndClaim('board',b.id,'succeeded',{reply:'pong'},10_000).commands[0].id,c.id);
    assert.deepEqual(store.resultAndClaim('board',a.id,'succeeded',{reply:'pong'},10_000).commands,[]);
    assert.equal(store.command(c.id).attempts,1);
    assert.equal(store.resultAndClaim('board',a.id,'failed',null,10_000).code,409);
    assert.equal(store.resultAndClaim('other',a.id,'succeeded',{reply:'pong'},10_000).code,404);
    assert.deepEqual(store.resultAndClaim('board',c.id,'succeeded',{reply:'pong'},10_000).commands,[]);
    assert.equal(store.db.prepare('SELECT next_id FROM command_handoffs WHERE previous_id=?').get(c.id),undefined);
    const d=store.enqueue('board','device.ping',{},120);
    assert.deepEqual(store.resultAndClaim('board',c.id,'succeeded',{reply:'pong'},10_000).commands,[]);
    assert.equal(store.command(d.id).status,'pending');
    assert.equal(store.claim('board',10_000).id,d.id);
  } finally {store.close();rmSync(directory,{recursive:true,force:true});}
});

test('handoff lease recovery renews only expired leases and honors command TTL', t => {
  const store=new Store(':memory:',[{id:'board',token:firstDeviceToken}]);
  try {
    const a=store.enqueue('board','device.ping',{},120),b=store.enqueue('board','device.ping',{},120);
    store.claim('board',1000);store.resultAndClaim('board',a.id,'succeeded',null,1000);
    const lease=store.command(b.id).lease_until;
    t.mock.method(Date,'now',()=>lease+1);
    assert.equal(store.resultAndClaim('board',a.id,'succeeded',null,1000).commands[0].id,b.id);
    assert.equal(store.command(b.id).attempts,2);
    store.resultAndClaim('board',a.id,'succeeded',null,1000);
    assert.equal(store.command(b.id).attempts,2);
    t.mock.method(Date,'now',()=>b.expires_at+1);
    assert.deepEqual(store.resultAndClaim('board',a.id,'succeeded',null,1000).commands,[]);
    assert.equal(store.command(b.id).status,'expired');
  } finally {store.close();}
});

test('an existing poll receives the next lease instead of duplicating the handoff', async t => {
  const f=await fixture(t,{pollWaitMs:5000,leaseMs:10_000}),auth=await f.login();
  const first=await f.enqueue(auth),second=await f.enqueue(auth);
  await f.request('/api/devices/board-1/commands',{headers:f.deviceHeaders});
  let ready;const waiting=new Promise(resolve=>{ready=resolve;});const claim=f.store.claim.bind(f.store);
  f.store.claim=(...args)=>{const value=claim(...args);if(!value)ready();return value;};
  const poll=f.request('/api/devices/board-1/commands',{headers:f.deviceHeaders});await waiting;
  const exec=f.store.db.exec.bind(f.store.db),calls=[];
  f.store.db.exec=sql=>{calls.push(sql);return exec(sql);};
  const response=await f.request(`/api/devices/board-1/commands/${first.id}/result?next=1`,{
    method:'POST',headers:f.deviceHeaders,body:JSON.stringify({status:'succeeded',result:{reply:'pong'}})});
  assert.deepEqual((await response.json()).commands,[]);
  assert.equal((await (await poll).json()).commands[0].id,second.id);
  assert.equal(f.store.command(second.id).attempts,1);
  assert.deepEqual(calls,['BEGIN IMMEDIATE','COMMIT']);
});

test('transaction observers run only after successful durable commit', () => {
  const store = new Store(':memory:', [{ id: 'board', token: firstDeviceToken }]);
  const events = [];
  store.observability = {
    run: (_name, _attributes, fn) => fn(),
    currentTraceparent: () => '00-' + 'a'.repeat(32) + '-' + 'b'.repeat(16) + '-01',
    record: name => events.push(name), queueTime: { observe: () => events.push('queueMetric') }
  };
  try {
    assert.throws(() => store.transaction(() => {
      store.enqueue('board', 'device.ping', {}, 60); store.claim('board', 10_000);
      assert.deepEqual(events, []); throw new Error('Rollback');
    }), /Rollback/);
    assert.deepEqual(events, []);
    assert.equal(store.db.prepare('SELECT count(*) n FROM commands').get().n, 0);
    store.transaction(() => {
      store.enqueue('board', 'device.ping', {}, 60); store.claim('board', 10_000);
      assert.deepEqual(events, []);
    });
    assert.deepEqual(events, ['command.queue_wait', 'queueMetric', 'command.delivery']);
    assert.throws(() => store.transaction(async () => {}), /synchronous/);
    assert.equal(store.db.prepare('PRAGMA synchronous').get().synchronous, 2);
  } finally { store.close(); }
});

test('presence remains live while disk writes coalesce, roll back, and handle clock changes', t => {
  let now=100_000;
  t.mock.method(Date,'now',()=>now);
  const store=new Store(':memory:',[{id:'board',token:firstDeviceToken}]);
  const persisted=()=>store.db.prepare('SELECT last_seen FROM devices WHERE id=?').get('board').last_seen;
  const changes=()=>store.db.prepare('SELECT total_changes() n').get().n;
  try {
    store.seen('board');assert.equal(persisted(),100_000);
    const before=changes();
    now+=100;store.seen('board');
    assert.equal(changes(),before);
    assert.equal(store.device('board').last_seen,now);
    assert.equal(store.snapshot().devices[0].lastSeen,new Date(now).toISOString());
    now=105_000;store.seen('board');assert.equal(persisted(),105_000);
    now=110_000;
    assert.throws(()=>store.transaction(()=>{store.seen('board');throw new Error('rollback');}),/rollback/);
    assert.equal(persisted(),105_000);assert.equal(store.device('board').last_seen,110_000);
    now=110_001;store.seen('board');assert.equal(persisted(),now);
    now=109_000;store.seen('board');assert.equal(persisted(),now);
  } finally {store.close();}
});

test('clean shutdown persists the latest coalesced presence without changing command durability', t => {
  let now=100_000;t.mock.method(Date,'now',()=>now);
  const directory=mkdtempSync(join(tmpdir(),'presence-test-')),path=join(directory,'state.sqlite');
  const store=new Store(path,[{id:'board',token:firstDeviceToken}]);
  store.seen('board');now+=10;store.seen('board');store.close();
  const reopened=new Store(path);
  try {
    assert.equal(reopened.device('board').last_seen,now);
    assert.equal(reopened.db.prepare('PRAGMA synchronous').get().synchronous,2);
  } finally {reopened.close();rmSync(directory,{recursive:true,force:true});}
});
