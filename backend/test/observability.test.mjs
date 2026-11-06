import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { InMemorySpanExporter } from '@opentelemetry/sdk-trace';
import { Observability, commandPayloadAttributes, newTraceparent, validateTelemetry } from '../src/observability.mjs';
import { Store } from '../src/store.mjs';
import { createApp } from '../src/app.mjs';
import { hashPassword } from '../src/auth.mjs';

async function fixture(t, options = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'resac-observability-'));
  const token = randomBytes(32).toString('hex'), logs = [], exporter = new InMemorySpanExporter();
  const obs = new Observability({ exporter, defaults: false, log: value => logs.push(value), ...options });
  const config = { httpDev: true, adminUsername: 'test', passwordHash: await hashPassword(randomBytes(32).toString('hex')),
    sessionMs: 60000, pollWaitMs: 20, leaseMs: 10, dbPath: join(directory, 'test.sqlite'), devices: [{ id: 'board', token }] };
  const store = new Store(config.dbPath, config.devices, obs);
  const app = createApp(config, store, obs), server = http.createServer(app.handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { app.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); store.close(); await obs.close(); rmSync(directory, { recursive: true, force: true }); });
  const request = (path, body, authenticated = true, parent) => fetch(`http://127.0.0.1:${server.address().port}${path}`, {
    method: body ? 'POST' : 'GET', headers: { 'content-type': 'application/json', ...(authenticated ? { authorization: `Bearer ${token}` } : {}), ...(parent ? { traceparent: parent } : {}) }, body: body ? JSON.stringify(body) : undefined
  });
  return { config, obs, store, request, exporter, logs };
}
const batch = (parent = newTraceparent()) => ({ session: randomBytes(16).toString('hex'), batchId: randomBytes(16).toString('hex'), metrics: { cpu_ratio: .25, memory_available_bytes: 1024 }, events: [
  { name: 'http.device', traceparent: parent, durationMs: 25, ageMs: 100, attributes: { outcome: 'tls', 'curl.code': 60, 'request.kind': 'readings' } }
] });

test('connection evidence survives validation, logs and traces without widening arbitrary attributes', async t => {
  const f = await fixture(t), value = batch();
  const attributes = { outcome: 'connect', 'curl.code': 7, 'http.status_code': 0, 'request.kind': 'telemetry',
    'error.reason': 'network_unreachable', 'error.stage': 'connect',
    'http.timing.dns_ms': 1, 'http.timing.connect_ms': 0, 'http.timing.tls_ms': 0,
    'http.timing.first_byte_ms': 0, 'http.timing.total_ms': 1360,
    'network.start.ipv4_default_route': 'present', 'network.start.interface_state': 'up',
    'network.start.wifi_quality': 70, 'network.start.wifi_signal_dbm': -42,
    'network.ipv4_default_route': 'absent', 'network.interface_state': 'down',
    'network.wifi_quality': 0, 'network.wifi_signal_dbm': -90 };
  value.events[0].attributes = attributes;
  assert.equal((await f.request('/api/devices/board/telemetry', value)).status, 200);
  await f.obs.provider.forceFlush();
  const span = f.exporter.getFinishedSpans().find(span => span.name === 'http.device');
  const log = f.logs.find(log => log.operation === 'http.device');
  for (const [key, val] of Object.entries(attributes)) {
    assert.equal(span.attributes[key], val);
    assert.equal(log[key], val);
  }
  assert.match(await f.obs.registry.metrics(), /resac_device_requests_total\{device="board",kind="telemetry",outcome="connect"\} 1/);
  for (const invalid of [
    { 'error.reason': 'Connection refused at private.example TOKEN' }, { 'error.stage': 'arbitrary' },
    { 'network.ipv4_default_route': true }, { 'network.interface_state': 'wlan0: private' },
    { 'network.wifi_signal_dbm': -131 }, { 'network.wifi_signal_dbm': 'private' },
    { 'network.wifi_quality': 256 }, { 'http.timing.total_ms': NaN },
    { 'http.timing.connect_ms': -1 }, { 'http.timing.total_ms': 86400_001 },
    { 'network.raw': 'private' }, { 'error.message': 'private' }
  ]) assert.equal(validateTelemetry({ ...value, events: [{ ...value.events[0], attributes: invalid }] }), false);
  assert.equal(validateTelemetry({ ...value, events: [{ ...value.events[0], name: 'sensor.read' }] }), false);
});

test('configured devices expose zero failure counters before their first failure', async t => {
  const f = await fixture(t);
  f.obs.collect(f.store, f.config);
  const requestCounter = /resac_device_requests_total\{device="board",kind="fan-state",outcome="connect"\} (\d+)/;
  const eventCounter = /resac_device_events_total\{device="board",operation="http.request",outcome="connect"\} (\d+)/;
  const before = await f.obs.registry.metrics();
  assert.equal(before.match(requestCounter)[1], '0');
  assert.equal(before.match(eventCounter)[1], '0');
  const value = batch();
  value.events[0].name = 'http.request';
  value.events[0].attributes = { outcome: 'connect', 'request.kind': 'fan-state', 'curl.code': 7 };
  f.obs.ingest('board', value);
  f.obs.collect(f.store, f.config); // Collection must not reset accumulated events.
  f.obs.ingest('board', value); // Nor may a retried batch duplicate the first failure.
  const after = await f.obs.registry.metrics();
  assert.equal(after.match(requestCounter)[1], '1');
  assert.equal(after.match(eventCounter)[1], '1');
});

test('authenticated diagnostics validate atomically, deduplicate retries, and correlate network failures', async t => {
  const f = await fixture(t), value = batch();
  assert.equal((await f.request('/api/devices/board/telemetry', value, false)).status, 401);
  assert.equal((await f.request('/api/devices/other/telemetry', value)).status, 401);
  assert.equal((await f.request('/api/devices/board/telemetry', { ...value, events: [...value.events, { ...value.events[0], name: 'arbitrary-operation' }] })).status, 400);
  assert.equal((await f.request('/api/devices/board/telemetry', value)).status, 200);
  assert.equal((await f.request('/api/devices/board/telemetry', value)).status, 200);
  await f.obs.provider.forceFlush();
  const deviceSpans = f.exporter.getFinishedSpans().filter(span => span.name === 'http.device');
  assert.equal(deviceSpans.length, 1);
  assert.equal(deviceSpans[0].spanContext().traceId, value.events[0].traceparent.slice(3, 35));
  assert.equal(deviceSpans[0].status.code, 2);
  assert.equal(deviceSpans[0].attributes['timing.approximate'], true);
  const metrics = await f.obs.registry.metrics();
  assert.match(metrics, /resac_device_requests_total\{device="board",kind="readings",outcome="tls"\} 1/);
  assert.match(metrics, /resac_device_value\{device="board",metric="cpu_ratio"\} 0.25/);
  assert.ok(f.logs.some(log => log.trace_id === value.events[0].traceparent.slice(3, 35)));
});

test('command type and source reach spans and logs without inferring errors from error.kind', async t => {
  const f = await fixture(t), value = batch();
  const payload = { level: 3, sessionId: 'a'.repeat(32) };
  value.events = ['manual', 'automatic'].flatMap(source => ['succeeded', 'failed'].map(outcome => ({
    name: 'command.execute', traceparent: newTraceparent(), durationMs: 12, ageMs: 0,
    attributes: { 'command.id': `${source}-${outcome}`, 'command.type': 'fan.level', 'command.source': source,
      ...commandPayloadAttributes('fan.level', payload), outcome, 'error.kind': outcome === 'succeeded' ? 'none' : 'avr_timeout' }
  })));
  assert.equal((await f.request('/api/devices/board/telemetry', value)).status, 200);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans().filter(span => span.name === 'command.execute');
  assert.equal(spans.length, 4);
  for (const event of value.events) {
    const span = spans.find(span => span.attributes['command.id'] === event.attributes['command.id']);
    const log = f.logs.find(log => log['command.id'] === event.attributes['command.id']);
    for (const key of ['command.type', 'command.source', 'outcome']) {
      assert.equal(span.attributes[key], event.attributes[key]);
      assert.equal(log[key], event.attributes[key]);
    }
    assert.equal(log.level, event.attributes.outcome === 'succeeded' ? 'info' : 'error');
    assert.equal(log.trace_id, span.spanContext().traceId);
    assert.equal(log.span_id, span.spanContext().spanId);
    assert.deepEqual(JSON.parse(span.attributes['command.payload']), payload);
    assert.deepEqual(log['command.payload'], payload);
  }
});

test('command payloads retain supported contents and exclude unrelated fields', () => {
  const sessionId = 'a'.repeat(32);
  for (const [type, payload] of [
    ['device.ping', {}], ['fan.command', { button: 'power', sessionId }],
    ['fan.level', { level: 0, sessionId }], ['fan.calibrate', { confirmOff: true, sessionId }],
    ...[0, .1, 10, Number.MAX_VALUE].map(hysteresisC =>
      ['fan.curve', { enabled: true, thresholdsC: [-1, 20, 24, 24, 30], hysteresisC, sessionId }]),
    ['ir.send', { protocol: 'NEC', address: 56960, command: 22, repeats: 3, sessionId }]
  ]) {
    const attributes = { 'command.type': type, ...commandPayloadAttributes(type, { ...payload, token: 'private' }) };
    assert.deepEqual(JSON.parse(attributes['command.payload']), payload);
    const value = batch(); value.events[0].attributes = attributes;
    assert.equal(validateTelemetry(value), true);
  }
  assert.deepEqual(commandPayloadAttributes('unknown', { token: 'private' }), {});
  assert.deepEqual(JSON.parse(commandPayloadAttributes('fan.level', { level: true, sessionId: 'private' })['command.payload']), {});
  const value = batch();
  for (const payload of ['not json', 'x'.repeat(1025), 'null', '[]', '{"level":true}', '{"level":6}', '{"level":3,"token":"private"}']) {
    value.events[0].attributes = { 'command.type': 'fan.level', 'command.payload': payload };
    assert.equal(validateTelemetry(value), false);
  }
});

test('HTTP log levels distinguish successful requests, client errors, and server errors', async t => {
  const f = await fixture(t);
  assert.equal((await f.request('/healthz')).status, 200);
  assert.equal((await f.request('/api/devices/board/readings', { temperatureC: 22, humidity: 50 }, false)).status, 401);
  f.store.db.exec('DROP TABLE readings');
  assert.equal((await f.request('/api/devices/board/readings', { temperatureC: 22, humidity: 50 })).status, 500);
  const levels = Object.fromEntries(f.logs.filter(log => log.operation === 'http.server').map(log => [log.status, log.level]));
  assert.deepEqual(levels, { 200: 'info', 401: 'warn', 500: 'error' });
});

test('command metadata is optional for older agents and bounded for newer agents', () => {
  const value = batch(), event = value.events[0];
  assert.equal(validateTelemetry(value), true);
  for (const attributes of [
    { 'command.type': 'fan.level', 'command.source': 'automatic' },
    { 'command.type': 'device.ping', 'command.source': 'manual' }
  ]) assert.equal(validateTelemetry({ ...value, events: [{ ...event, attributes }] }), true);
  for (const attributes of [
    { 'command.type': 1 }, { 'command.type': 'x'.repeat(65) }, { 'command.type': 'payload with spaces' },
    { 'command.source': 'untrusted' }, { 'command.source': 1 }, { payload: 'private' }
  ]) assert.equal(validateTelemetry({ ...value, events: [{ ...event, attributes }] }), false);
});

test('command context survives reopening SQLite and redelivery, and result spans join it', async t => {
  const f = await fixture(t);
  const command = f.obs.run('command.request', {}, () => f.store.enqueue('board', 'device.ping', {}, 60));
  assert.ok(command.traceparent);
  const reopened = new Store(f.config.dbPath, f.config.devices);
  const first = reopened.claim('board', 1);
  assert.equal(first.traceparent, command.traceparent);
  reopened.db.prepare('UPDATE commands SET lease_until=0 WHERE id=?').run(command.id);
  const second = reopened.claim('board', 1000);
  assert.equal(second.traceparent, first.traceparent); reopened.close();
  assert.equal((await f.request(`/api/devices/board/commands/${command.id}/result`, { status: 'failed', result: { error: 'test' } })).status, 200);
  await f.obs.provider.forceFlush();
  const resultSpan = f.exporter.getFinishedSpans().find(span => span.name === 'command.result');
  assert.equal(resultSpan.spanContext().traceId, command.traceparent.slice(3, 35));
  assert.equal(resultSpan.attributes['command.type'], 'device.ping');
  assert.equal(resultSpan.attributes['command.source'], 'manual');
  assert.equal(f.store.command(command.id).status, 'failed');
});

test('reading uploads propagate trace context and database errors are measured without SQL values', async t => {
  const f = await fixture(t), parent = newTraceparent();
  const reading = { readingId: 'sample-1', temperatureC: 23, humidity: 50 };
  assert.equal((await f.request('/api/devices/board/readings', reading, true, parent)).status, 201);
  assert.equal((await f.request('/api/devices/board/readings', reading, true, parent)).status, 200);
  f.obs.collect(f.store, f.config);
  f.store.db.exec('DROP TABLE readings');
  assert.equal((await f.request('/api/devices/board/readings', reading, true, parent)).status, 500);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans().filter(span => span.name === 'db.reading');
  assert.equal(spans.length, 3);
  assert.ok(spans.every(span => span.spanContext().traceId === parent.slice(3, 35)));
  assert.equal(spans.at(-1).status.code, 2);
  const metrics = await f.obs.registry.metrics();
  assert.match(metrics, /resac_db_rows\{table="readings"\} 1/);
  assert.match(metrics, /resac_db_duration_seconds_count\{operation="reading",outcome="error"\} 1/);
  assert.ok(!JSON.stringify(f.logs).includes('temperatureC'));
});

test('unavailable exporters do not fail application writes and export failures are visible', async t => {
  const f = await fixture(t, { exporter: { export(spans, done) { done({ code: 1 }); }, shutdown: async () => {} } });
  assert.equal((await f.request('/api/devices/board/readings', { temperatureC: 22, humidity: 50 })).status, 201);
  await assert.rejects(f.obs.provider.forceFlush());
  assert.match(await f.obs.registry.metrics(), /resac_trace_export_errors_total 1/);
});

test('reject malformed contexts, unbounded labels, negative and nonfinite resource values', () => {
  const value = batch(); assert.equal(validateTelemetry(value), true);
  for (const change of [ { session: [value.session] }, { batchId: [value.batchId] }, { metrics: { unexpected: 1 } }, { metrics: { cpu_ratio: 2 } }, { metrics: { agent_rss_bytes: -1 } },
    { metrics: { agent_rss_bytes: NaN } }, { events: Array(33).fill(value.events[0]) },
    { events: [{ ...value.events[0], traceparent: '00-' + '0'.repeat(32) + '-123456789abcdef0-01' }] },
    { events: [{ ...value.events[0], attributes: { authorization: 'secret' } }] } ]) assert.equal(validateTelemetry({ ...value, ...change }), false);
});

test('device root spans use the propagated IDs rather than creating missing parents', async t => {
  const f = await fixture(t), value = batch();
  value.events = [{ ...value.events[0], name: 'reading.collect', attributes: { outcome: 'ok', 'reading.id': 'sample' } }];
  f.obs.ingest('board', value);
  await f.obs.provider.forceFlush();
  const root = f.exporter.getFinishedSpans().find(span => span.name === 'reading.collect');
  assert.equal(root.spanContext().traceId, value.events[0].traceparent.slice(3, 35));
  assert.equal(root.spanContext().spanId, value.events[0].traceparent.slice(36, 52));
  assert.equal(root.parentSpanContext, undefined);
});

test('legacy pending commands receive context on first delivery and retain it on retry', async t => {
  const f = await fixture(t);
  const command = f.store.enqueue('board', 'device.ping', {}, 60);
  f.store.db.prepare('UPDATE commands SET traceparent=NULL WHERE id=?').run(command.id);
  const delivered = f.store.claim('board', 1);
  assert.ok(delivered.traceparent);
  f.store.db.prepare('UPDATE commands SET lease_until=0 WHERE id=?').run(command.id);
  assert.equal(f.store.claim('board', 1000).traceparent, delivered.traceparent);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans().filter(span => ['command.queue_wait', 'command.delivery'].includes(span.name));
  assert.equal(spans.length, 3);
  for (const span of spans) {
    assert.equal(span.attributes['command.type'], 'device.ping');
    assert.equal(span.attributes['command.source'], 'manual');
  }
  const logs = f.logs.filter(log => log['command.id'] === command.id);
  assert.equal(logs.length, 3);
  assert.ok(logs.every(log => log['command.type'] === 'device.ping' && log['command.source'] === 'manual' && log.level === 'info'));
});

test('device, AVR and backend spans share one trace with server requests nested under the propagated client', async t => {
  const f = await fixture(t), root = newTraceparent();
  const client = root.slice(0, 36) + randomBytes(8).toString('hex') + '-01';
  assert.equal((await f.request('/api/devices/board/readings', { readingId: 'multi-service', temperatureC: 23, humidity: 50 }, true, client)).status, 201);
  const value = batch(root);
  value.events = [
    { ...value.events[0], name: 'reading.collect', durationMs: 0, attributes: { outcome: 'ok', 'reading.id': 'multi-service' } },
    { ...value.events[0], name: 'sensor.read', attributes: { outcome: 'ok', 'reading.id': 'multi-service' } },
    { ...value.events[0], name: 'http.device', spanId: client.slice(36, 52), attributes: { outcome: 'ok', 'request.kind': 'readings' } }
  ];
  assert.equal(validateTelemetry(value), true);
  f.obs.ingest('board', value);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans().filter(span => span.spanContext().traceId === root.slice(3, 35));
  const byName = Object.fromEntries(spans.map(span => [span.name, span]));
  assert.equal(byName['reading.collect'].resource.attributes['service.name'], 'res-ac-agent');
  assert.equal(byName['sensor.read'].resource.attributes['service.name'], 'res-ac-avr');
  assert.equal(byName['http.device'].resource.attributes['service.name'], 'res-ac-agent');
  assert.equal(byName['http.server'].resource.attributes['service.name'], 'res-ac-backend');
  assert.equal(byName['db.reading'].resource.attributes['service.name'], 'res-ac-backend');
  assert.equal(byName['http.device'].spanContext().spanId, client.slice(36, 52));
  assert.equal(byName['http.device'].parentSpanContext.spanId, root.slice(36, 52));
  assert.equal(byName['http.server'].parentSpanContext.spanId, byName['http.device'].spanContext().spanId);
  assert.equal(byName['db.transaction'].parentSpanContext.spanId, byName['http.server'].spanContext().spanId);
  assert.equal(byName['db.reading'].parentSpanContext.spanId, byName['db.transaction'].spanContext().spanId);
  assert.equal(byName['http.device'].kind, 2); // CLIENT
  assert.equal(byName['http.server'].kind, 1); // SERVER
  assert.ok(f.logs.some(log => log.operation === 'sensor.read' && log.service === 'res-ac-avr'));
});

test('command result persistence remains beneath the result-upload server span', async t => {
  const f = await fixture(t);
  const payload = { button: 'power', sessionId: 'a'.repeat(32) };
  const command = f.store.enqueue('board', 'fan.command', payload, 60);
  f.store.claim('board', 10000);
  const client = command.traceparent.slice(0, 36) + randomBytes(8).toString('hex') + '-01';
  assert.equal((await f.request(`/api/devices/board/commands/${command.id}/result`, { status: 'succeeded', result: { reply: 'pong' } }, true, client)).status, 200);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans();
  const server = spans.find(span => span.name === 'http.server');
  const result = spans.find(span => span.name === 'command.result');
  const db = spans.find(span => span.name === 'db.result');
  const transaction = spans.find(span => span.name === 'db.transaction' && span.parentSpanContext?.spanId === result.spanContext().spanId);
  assert.equal(server.parentSpanContext.spanId, client.slice(36, 52));
  assert.equal(result.parentSpanContext.spanId, server.spanContext().spanId);
  assert.ok(transaction);
  assert.equal(db.parentSpanContext.spanId, transaction.spanContext().spanId);
  for (const span of spans.filter(span => ['command.queue_wait', 'command.delivery', 'command.result'].includes(span.name))) {
    assert.deepEqual(JSON.parse(span.attributes['command.payload']), payload);
  }
  for (const log of f.logs.filter(log => log['command.id'] === command.id)) {
    assert.deepEqual(log['command.payload'], payload);
  }
});

test('reject malformed explicit IDs and self-parent cycles while supporting legacy events', () => {
  const value = batch(), parentId = value.events[0].traceparent.slice(36, 52);
  assert.equal(validateTelemetry(value), true);
  for (const spanId of ['0'.repeat(16), 'too-short', 123, parentId])
    assert.equal(validateTelemetry({ ...value, events: [{ ...value.events[0], spanId }] }), false);
  const root = { ...value.events[0], name: 'http.request', spanId: parentId };
  assert.equal(validateTelemetry({ ...value, events: [root] }), true);
  assert.equal(validateTelemetry({ ...value, events: [{ ...root, spanId: randomBytes(8).toString('hex') }] }), false);
});

test('three providers share one bounded exporter with one shutdown', async () => {
  const spans = []; let shutdowns = 0;
  const obs = new Observability({ defaults: false, log: () => {}, exporter: {
    export(batch, done) { spans.push(...batch); done({ code: 0 }); }, async shutdown() { shutdowns++; }
  } });
  for (const component of ['backend', 'agent', 'avr']) obs.record('operation', newTraceparent(), Date.now(), 1, { component });
  await obs.close();
  assert.equal(shutdowns, 1);
  assert.deepEqual(new Set(spans.map(span => span.resource.attributes['service.name'])), new Set(['res-ac-backend', 'res-ac-agent', 'res-ac-avr']));
});


test('batched command phase durations validate and retain the correct component', async t => {
  const f = await fixture(t), value = batch();
  const names = ['command.journal', 'command.notify', 'bridge.roundtrip', 'avr.fetch', 'avr.safety'];
  value.events = names.map(name => ({name, traceparent: newTraceparent(), durationMs: 17, ageMs: 0,
    attributes: {'command.type': 'device.ping', 'command.source': 'manual', outcome: 'ok'}}));
  assert.equal((await f.request('/api/devices/board/telemetry', value)).status, 200);
  await Promise.all(Object.values(f.obs.providers).map(provider => provider.forceFlush()));
  for (const name of names) {
    const span = f.exporter.getFinishedSpans().find(span => span.name === name);
    assert.ok(span);
    assert.equal(span.resource.attributes['service.name'], name.startsWith('avr.') ? 'res-ac-avr' : 'res-ac-agent');
    assert.ok(f.logs.some(log => log.operation === name));
  }
});

test('compact command timing fields expand only on the backend and deduplicate as one batch', async t => {
  const f=await fixture(t), value=batch();
  value.events=[{name:'command.execute',traceparent:newTraceparent(),durationMs:400,ageMs:0,
    attributes:{'command.type':'device.ping',outcome:'succeeded','command.timing.fetch_ms':30,
      'command.timing.safety_ms':0,'command.timing.notify_ms':80,'command.timing.roundtrip_ms':300,
      'command.timing.decode_ms':20}}];
  assert.equal((await f.request('/api/devices/board/telemetry',value)).status,200);
  assert.equal((await f.request('/api/devices/board/telemetry',value)).status,200);
  await Promise.all(Object.values(f.obs.providers).map(provider=>provider.forceFlush()));
  for(const name of ['command.execute','command.decode','avr.fetch','avr.safety','command.notify','bridge.roundtrip'])
    assert.equal(f.exporter.getFinishedSpans().filter(span=>span.name===name).length,1);
  for(const invalid of [-1,86400_001,NaN,'30'])
    assert.equal(validateTelemetry({...value,events:[{...value.events[0],attributes:{'command.timing.fetch_ms':invalid}}]}),false);
  assert.equal(validateTelemetry({...value,events:[{...value.events[0],name:'reading.buffer'}]}),false);
});

test('compact agent and final-journal phases stay bounded on existing HTTP events', () => {
  const value=batch();
  const phases=['http.agent.prepare_ms','http.agent.consume_ms','http.agent.ipc_wait_ms','command.final_journal_ms'];
  for (const key of phases) {
    value.events[0].attributes={outcome:'ok','request.kind':'result',[key]:25};
    assert.equal(validateTelemetry(value),true);
    for (const invalid of [-1,NaN,Infinity,86400_001,'secret']) {
      value.events[0].attributes[key]=invalid;
      assert.equal(validateTelemetry(value),false);
    }
  }
  value.events[0]={...value.events[0],name:'command.execute',attributes:{outcome:'ok','http.agent.prepare_ms':10}};
  assert.equal(validateTelemetry(value),false);
});
