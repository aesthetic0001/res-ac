import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AsyncLocalStorage } from 'node:async_hooks';
import { DatabaseSync } from 'node:sqlite';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { Store } from '../src/store.mjs';
import { WriteQueue } from '../src/write-queue.mjs';

function fixture(t, options) {
  const directory = mkdtempSync(join(tmpdir(), 'resac-writes-'));
  const path = join(directory, 'state.sqlite');
  const store = new Store(path, [{ id: 'board', token: 'test-only' }]);
  const queue = new WriteQueue(store, options);
  t.after(() => { queue.close(); store.close(); rmSync(directory, { recursive: true, force: true }); });
  return { store, queue, path };
}
const count = db => db.prepare('SELECT count(*) n FROM commands').get().n;
const enqueue = store => store.enqueue('board', 'device.ping', {}, 60);

test('ready writes share one FULL commit; observers and responses see only durable rows', async t => {
  const { store, queue, path } = fixture(t);
  const reader = new DatabaseSync(path, { readOnly: true });
  t.after(() => reader.close());
  const execute = store.db.exec.bind(store.db);
  let commits = 0, observed = 0;
  store.db.exec = sql => {
    if (sql === 'COMMIT') { commits++; assert.equal(count(reader), 0); assert.equal(observed, 0); }
    const value = execute(sql);
    if (sql === 'COMMIT') assert.equal(count(reader), 24);
    return value;
  };
  const rows = await Promise.all(Array.from({ length: 24 }, () => queue.run(() => {
    const row = enqueue(store);
    store.afterCommit(() => { assert.equal(count(reader), 24); observed++; });
    return row;
  })));
  assert.equal(commits, 1); assert.equal(observed, 24);
  assert.equal(new Set(rows.map(row => row.id)).size, 24);
  assert.equal(store.db.prepare('PRAGMA synchronous').get().synchronous, 2);
});

test('a rejected request rolls back its rows and commit observers without rejecting neighbors', async t => {
  const { store, queue } = fixture(t);
  let failedObserver = false;
  const failed = queue.run(() => {
    enqueue(store); store.seen('board');
    store.afterCommit(() => { failedObserver = true; });
    throw Object.assign(new Error('stale session'), { status: 409 });
  });
  const good = queue.run(() => enqueue(store));
  const result = await Promise.allSettled([failed, good]);
  assert.equal(result[0].status, 'rejected'); assert.equal(result[0].reason.status, 409);
  assert.equal(result[1].status, 'fulfilled'); assert.equal(count(store.db), 1);
  assert.equal(failedObserver, false);
  assert.equal(store.db.prepare('SELECT last_seen FROM devices').get().last_seen, null);
  assert.equal(store.presence.get('board').persisted, null);
  await queue.run(() => store.seen('board'));
  assert.notEqual(store.db.prepare('SELECT last_seen FROM devices').get().last_seen, null);
});

test('failed commit rejects every request, emits no observer, and leaves the queue usable', async t => {
  const { store, queue } = fixture(t);
  const execute = store.db.exec.bind(store.db);
  let fail = true, observed = false;
  store.db.exec = sql => {
    if (sql === 'COMMIT' && fail) { fail = false; throw new Error('commit failure'); }
    return execute(sql);
  };
  const result = await Promise.allSettled(Array.from({ length: 8 }, () => queue.run(() => {
    enqueue(store); store.afterCommit(() => { observed = true; });
  })));
  assert.ok(result.every(row => row.status === 'rejected'));
  assert.equal(observed, false); assert.equal(count(store.db), 0);
  await queue.run(() => enqueue(store)); assert.equal(count(store.db), 1);
});

test('SQL faults abort the whole commit rather than masquerading as isolated validation errors', async t => {
  const { store, queue } = fixture(t);
  const result = await Promise.allSettled([
    queue.run(() => enqueue(store)),
    queue.run(() => { store.db.exec('INSERT INTO nonexistent_table VALUES(1)'); })
  ]);
  assert.ok(result.every(row => row.status === 'rejected')); assert.equal(count(store.db), 0);
  await queue.run(() => enqueue(store)); assert.equal(count(store.db), 1);
});

test('work and backlog are bounded; overflow receives 503 without losing accepted writes', async t => {
  const { store, queue } = fixture(t);
  const execute = store.db.exec.bind(store.db); let commits = 0;
  store.db.exec = sql => { if (sql === 'COMMIT') commits++; return execute(sql); };
  const result = await Promise.allSettled(Array.from({ length: 129 }, () => queue.run(() => enqueue(store))));
  assert.equal(result.filter(row => row.status === 'fulfilled').length, 128);
  assert.equal(result[128].reason.status, 503); assert.equal(count(store.db), 128);
  assert.equal(commits, 4);
});

test('each request keeps its own async context inside a shared transaction', async t => {
  const { queue } = fixture(t); const local = new AsyncLocalStorage();
  const values = await Promise.all(['first', 'second', 'third'].map(value =>
    local.run(value, () => queue.run(() => local.getStore()))));
  assert.deepEqual(values, ['first', 'second', 'third']);
});

test('async and thenable callbacks cannot leave partial committed writes', async t => {
  const { store, queue } = fixture(t);
  let called = false;
  await assert.rejects(queue.run(async () => { called = true; }), /synchronous/);
  assert.equal(called, false);
  const result = await Promise.allSettled([
    queue.run(() => { enqueue(store); return Promise.resolve(); }), queue.run(() => enqueue(store))
  ]);
  assert.ok(result.every(row => row.status === 'rejected')); assert.equal(count(store.db), 0);
});

test('enqueue timestamps and TTL include time waiting before a durable batch', async t => {
  const { store, queue } = fixture(t), readyAt = Date.now() - 1000;
  const row = await queue.run(() => store.enqueue('board', 'device.ping', {}, 60, readyAt));
  assert.equal(row.created_at, readyAt); assert.equal(row.expires_at, readyAt + 60000);
});

test('shutdown drains accepted work before closing the database and refuses new writes', async t => {
  const { store, queue, path } = fixture(t);
  const pending = Array.from({ length: 70 }, () => queue.run(() => enqueue(store)));
  queue.close(); await Promise.all(pending);
  await assert.rejects(queue.run(() => enqueue(store)), { status: 503 });
  const reader = new DatabaseSync(path, { readOnly: true });
  assert.equal(count(reader), 70); reader.close();
});

test('optional span failures cannot hide committed results', async t => {
  const failure = () => { throw Error('export failed'); };
  const { store, queue } = fixture(t, { observability: { span: () => ({ setAttribute: failure }) } });
  await queue.run(() => enqueue(store)); assert.equal(count(store.db), 1);
});

test('disabled control retains independent durable commits', async t => {
  const { store, queue } = fixture(t, { enabled: false });
  let commits = 0; const execute = store.db.exec.bind(store.db);
  store.db.exec = sql => { if (sql === 'COMMIT') commits++; return execute(sql); };
  await Promise.all(Array.from({ length: 5 }, () => queue.run(() => enqueue(store))));
  assert.equal(commits, 5); assert.equal(count(store.db), 5);
});

for (const phase of ['before', 'after']) test(`process death ${phase} shared COMMIT recovers consistent state`, async t => {
  const directory = mkdtempSync(join(tmpdir(), 'resac-crash-writes-')), path = join(directory, 'state.sqlite');
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const source = `import{Store}from ${JSON.stringify(new URL('../src/store.mjs', import.meta.url).href)};
import{WriteQueue}from ${JSON.stringify(new URL('../src/write-queue.mjs', import.meta.url).href)};
const store=new Store(${JSON.stringify(path)},[{id:'board',token:'test-only'}]),q=new WriteQueue(store),exec=store.db.exec.bind(store.db);
function stop(){process.stdout.write('BARRIER\\n');process.kill(process.pid,'SIGSTOP');}
store.db.exec=sql=>{if(sql==='COMMIT'&&${JSON.stringify(phase)}==='before')stop();const value=exec(sql);if(sql==='COMMIT'&&${JSON.stringify(phase)}==='after')stop();return value;};
await Promise.all(Array.from({length:24},()=>q.run(()=>store.enqueue('board','device.ping',{},60))));console.log('ACK');`;
  const child = spawn(process.execPath, ['--input-type=module', '-e', source], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => { if (child.exitCode === null) child.kill('SIGKILL'); });
  let output = '', errors = '';
  child.stdout.on('data', data => { output += data; }); child.stderr.on('data', data => { errors += data; });
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error('Crash barrier timed out: ' + errors)), 10000);
    const done = () => { if (output.includes('BARRIER\n')) { clearTimeout(timeout); child.stdout.off('data', done); resolve(); } };
    child.stdout.on('data', done);
  });
  child.kill('SIGKILL'); await once(child, 'close');
  assert.equal(output, 'BARRIER\n');
  const reader = new DatabaseSync(path);
  try { assert.equal(count(reader), phase === 'before' ? 0 : 24); assert.equal(reader.prepare('PRAGMA quick_check').get().quick_check, 'ok'); }
  finally { reader.close(); }
});
