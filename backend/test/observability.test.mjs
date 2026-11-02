import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { InMemorySpanExporter } from '@opentelemetry/sdk-trace';
import { Observability, newTraceparent, validateTelemetry } from '../src/observability.mjs';
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
  assert.equal(byName['db.reading'].parentSpanContext.spanId, byName['http.server'].spanContext().spanId);
  assert.equal(byName['http.device'].kind, 2); // CLIENT
  assert.equal(byName['http.server'].kind, 1); // SERVER
  assert.ok(f.logs.some(log => log.operation === 'sensor.read' && log.service === 'res-ac-avr'));
});

test('command result persistence remains beneath the result-upload server span', async t => {
  const f = await fixture(t);
  const command = f.store.enqueue('board', 'device.ping', {}, 60);
  f.store.claim('board', 10000);
  const client = command.traceparent.slice(0, 36) + randomBytes(8).toString('hex') + '-01';
  assert.equal((await f.request(`/api/devices/board/commands/${command.id}/result`, { status: 'succeeded', result: { reply: 'pong' } }, true, client)).status, 200);
  await f.obs.provider.forceFlush();
  const spans = f.exporter.getFinishedSpans();
  const server = spans.find(span => span.name === 'http.server');
  const result = spans.find(span => span.name === 'command.result');
  const db = spans.find(span => span.name === 'db.result');
  assert.equal(server.parentSpanContext.spanId, client.slice(36, 52));
  assert.equal(result.parentSpanContext.spanId, server.spanContext().spanId);
  assert.equal(db.parentSpanContext.spanId, result.spanContext().spanId);
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
