import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { Store } from '../src/store.mjs';
import { loadConfig } from '../src/config.mjs';

const state = { sessionId: 'a'.repeat(32), calibrated: true, power: 'off', speed: 0, revision: 1,
  firmwareVersion: 'v4', controlSource: 'calibration', automatic: { enabled: false, status: 'disabled' } };
function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'resac-heartbeat-'));
  const path = join(directory, 'state.sqlite'), token = randomBytes(32).toString('hex');
  const store = new Store(path, [{ id: 'board', token }], undefined, { fanHeartbeatCheckpointMs: 60_000 });
  const disk = new DatabaseSync(path, { readOnly: true });
  t.after(() => { disk.close(); store.close(); rmSync(directory, { recursive: true, force: true }); });
  return { store, disk, path, directory, token };
}
const persisted = disk => disk.prepare('SELECT state,received_at FROM fan_states WHERE device_id=?').get('board');

test('identical heartbeats keep live freshness exact without SQL writes until the durable checkpoint', t => {
  let now = 1_000_000; t.mock.method(Date, 'now', () => now);
  const { store, disk } = fixture(t);
  assert.equal(store.reportFanState('board', state), true);
  const changes = store.db.prepare('SELECT total_changes() n').get().n;
  for (let i = 1; i < 12; i++) {
    now += 5000;
    assert.equal(store.reportFanState('board', { ...state, automatic: { status: 'disabled', enabled: false } }), true);
    assert.equal(store.fanState('board').receivedAt, now);
    assert.equal(store.device('board').last_seen, now);
    assert.equal(persisted(disk).received_at, 1_000_000);
    assert.equal(store.db.prepare('SELECT total_changes() n').get().n, changes);
  }
  now += 5000; store.reportFanState('board', state);
  assert.equal(persisted(disk).received_at, now);
  assert.equal(store.db.prepare('PRAGMA synchronous').get().synchronous, 2);
  now -= 100; store.reportFanState('board', state);
  assert.equal(persisted(disk).received_at, now, 'clock rollback forces a conservative fresh checkpoint');
});

test('revision, session, firmware and automation changes persist immediately and stale revisions are rejected', t => {
  let now = 2_000_000; t.mock.method(Date, 'now', () => now);
  const { store, disk } = fixture(t);
  const transitions = [state, { ...state, revision: 2 }, { ...state, revision: 2, controlSource: 'manual' },
    { ...state, revision: 2, automatic: { enabled: false, status: 'waiting_for_sample' } },
    { ...state, revision: 2, firmwareVersion: 'v3' }, { ...state, sessionId: 'b'.repeat(32), revision: 0 }];
  for (const next of transitions) {
    now += 100;
    assert.equal(store.reportFanState('board', next), true);
    assert.deepEqual(JSON.parse(persisted(disk).state), next);
    assert.equal(persisted(disk).received_at, now);
  }
  store.reportFanState('board', { ...state, sessionId: 'b'.repeat(32), revision: 4 });
  const before = persisted(disk);
  assert.equal(store.reportFanState('board', { ...state, sessionId: 'b'.repeat(32), revision: 3 }), false);
  assert.deepEqual(persisted(disk), before);
  const returned = store.fanState('board'); returned.automatic.status = 'corrupted';
  assert.equal(store.fanState('board').automatic.status, 'disabled');
});

test('failed commits and nested writes cannot publish rolled-back state or accept a backwards revision', t => {
  const { store, disk } = fixture(t); store.reportFanState('board', state);
  assert.throws(() => store.transaction(() => {
    assert.equal(store.setFanState('board', { ...state, revision: 3 }), true);
    assert.equal(store.setFanState('board', { ...state, revision: 2 }), false);
    assert.equal(store.fanState('board').revision, 3);
    throw new Error('rollback');
  }), /rollback/);
  assert.equal(store.fanState('board').revision, 1);
  store.db.exec('CREATE TABLE heartbeat_fault(device_id TEXT REFERENCES devices(id) DEFERRABLE INITIALLY DEFERRED)');
  assert.throws(() => store.transaction(() => {
    store.reportFanState('board', { ...state, revision: 4 });
    store.db.prepare('INSERT INTO heartbeat_fault VALUES(?)').run('missing');
  }), /FOREIGN KEY/);
  assert.equal(store.fanState('board').revision, 1);
  assert.equal(JSON.parse(persisted(disk).state).revision, 1);
});

test('SIGKILL retains acknowledged state and restores an older, conservative heartbeat timestamp', t => {
  const directory = mkdtempSync(join(tmpdir(), 'resac-heartbeat-crash-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'state.sqlite'), token = randomBytes(32).toString('hex');
  const program = `import {Store} from ${JSON.stringify(new URL('../src/store.mjs', import.meta.url).href)};
    let now=10000; Date.now=()=>now;
    const s=new Store(${JSON.stringify(path)},[{id:'board',token:${JSON.stringify(token)}}],undefined,{fanHeartbeatCheckpointMs:60000});
    s.reportFanState('board',${JSON.stringify(state)});now=15000;s.reportFanState('board',${JSON.stringify(state)});
    console.log(s.fanState('board').receivedAt);process.kill(process.pid,'SIGKILL');`;
  const child = spawnSync(process.execPath, ['--input-type=module', '-e', program], { encoding: 'utf8' });
  assert.equal(child.signal, 'SIGKILL'); assert.equal(child.stdout.trim(), '15000');
  const recovered = new Store(path, [], undefined, { fanHeartbeatCheckpointMs: 60_000 });
  try {
    assert.deepEqual(recovered.fanState('board'), { ...state, receivedAt: 10000 });
    assert.equal(recovered.device('board').last_seen, 10000);
    assert.equal(recovered.db.prepare('PRAGMA quick_check').get().quick_check, 'ok');
  } finally { recovered.close(); }
});

test('graceful shutdown persists the last actual heartbeat rather than inventing newer freshness', t => {
  const directory = mkdtempSync(join(tmpdir(), 'resac-heartbeat-close-'));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const path = join(directory, 'state.sqlite');let now=10000;t.mock.method(Date,'now',()=>now);
  const store = new Store(path, [{id:'board',token:randomBytes(32).toString('hex')}], undefined, {fanHeartbeatCheckpointMs:60000});
  store.reportFanState('board',state);now=15000;store.reportFanState('board',state);now=40000;store.close();
  const recovered = new Store(path);
  try {
    assert.equal(recovered.fanState('board').receivedAt,15000);
    assert.equal(recovered.device('board').last_seen,15000);
  } finally { recovered.close(); }
});

test('the heartbeat experiment is explicit and bounded in configuration', () => {
  const env={ADMIN_USERNAME:'test',ADMIN_PASSWORD_HASH:'scrypt:'+'a'.repeat(32)+':'+'b'.repeat(128),DEVICE_ID:'board',DEVICE_TOKEN:randomBytes(32).toString('hex')};
  assert.equal(loadConfig(env).fanHeartbeatCheckpointMs,0);
  assert.equal(loadConfig({...env,FAN_HEARTBEAT_CHECKPOINT_MS:'60000'}).fanHeartbeatCheckpointMs,60000);
  for(const value of ['-1','NaN','Infinity','5000','60001'])assert.throws(()=>loadConfig({...env,FAN_HEARTBEAT_CHECKPOINT_MS:value}),/CHECKPOINT/);
});
