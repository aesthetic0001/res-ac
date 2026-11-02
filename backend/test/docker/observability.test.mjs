import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomBytes } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import { hashPassword } from '../../src/auth.mjs';
import { newTraceparent } from '../../src/observability.mjs';
const exec = promisify(execFile);

test('isolated stack ingests device/backend traces, metrics, logs and provisions Grafana', { timeout: 240000 }, async t => {
  const directory = await mkdtemp(join(tmpdir(), 'resac-stack-'));
  const name = 'resac-observe-test-' + randomBytes(6).toString('hex');
  const password = randomBytes(24).toString('hex'), deviceToken = randomBytes(32).toString('hex');
  const env = { ...process.env, ADMIN_USERNAME: 'test-admin', ADMIN_PASSWORD_HASH: await hashPassword(password), DEVICE_ID: 'test-board', DEVICE_TOKEN: deviceToken };
  const overlay = join(directory, 'test.yaml');
  await writeFile(overlay, `services:
  backend:
    image: ${name}-backend:local
    environment:
      HTTP_DEV: 'true'
      ADMIN_USERNAME: \${ADMIN_USERNAME}
      ADMIN_PASSWORD_HASH: \${ADMIN_PASSWORD_HASH}
      DEVICE_ID: \${DEVICE_ID}
      DEVICE_TOKEN: \${DEVICE_TOKEN}
    ports: !override ['127.0.0.1::3000']
  grafana:
    ports: !override ['127.0.0.1::3000']
  tempo:
    ports: ['127.0.0.1::3200']
  prometheus:
    ports: ['127.0.0.1::9090']
  loki:
    ports: ['127.0.0.1::3100']
`);
  const args = ['compose', '--project-name', name, '-f', 'compose.yaml', '-f', 'compose.observability.yaml', '-f', overlay];
  const docker = async (...more) => (await exec('docker', [...args, ...more], { cwd: new URL('../../', import.meta.url), env, maxBuffer: 4 * 1024 * 1024 })).stdout.trim();
  t.after(async () => {
    await docker('down', '-v', '--remove-orphans');
    await exec('docker', ['image', 'rm', `${name}-backend:local`]);
    await rm(directory, { recursive: true, force: true });
  });
  async function until(fn, message, timeout = 60000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) { try { const result = await fn(); if (result) return result; } catch {} await sleep(1000); }
    throw new Error(message);
  }
  await docker('up', '-d', '--build', 'backend', 'otel-collector', 'prometheus', 'tempo', 'loki', 'grafana');
  const base = async (service, port) => 'http://' + await docker('port', service, String(port));
  const backend = await base('backend', 3000), tempo = await base('tempo', 3200), loki = await base('loki', 3100), prometheus = await base('prometheus', 9090), grafana = (await base('grafana', 3000)) + '/grafana';
  await until(async () => (await fetch(backend + '/healthz')).ok, 'backend did not become ready');
  const headers = { 'content-type': 'application/json', authorization: `Bearer ${deviceToken}` };
  const parent = newTraceparent(), traceId = parent.slice(3, 35);
  const client = parent.slice(0, 36) + randomBytes(8).toString('hex') + '-01';
  assert.equal((await fetch(backend + '/api/devices/test-board/readings', { method: 'POST', headers: { ...headers, traceparent: client }, body: JSON.stringify({ readingId: 'smoke-reading', temperatureC: 23, humidity: 50 }) })).status, 201);
  const value = { session: randomBytes(16).toString('hex'), batchId: randomBytes(16).toString('hex'), metrics: { cpu_ratio: .25 }, events: [
    { name: 'reading.collect', traceparent: parent, durationMs: 0, ageMs: 100, attributes: { outcome: 'ok', 'reading.id': 'smoke-reading' } },
    { name: 'sensor.read', traceparent: parent, durationMs: 7, ageMs: 100, attributes: { outcome: 'ok', 'reading.id': 'smoke-reading' } },
    { name: 'http.device', traceparent: parent, spanId: client.slice(36, 52), durationMs: 10, ageMs: 100, attributes: { outcome: 'ok', 'request.kind': 'readings' } },
    { name: 'http.device', traceparent: parent, durationMs: 10, ageMs: 100, attributes: { outcome: 'ok', 'request.kind': 'commands' } }
  ] };
  assert.equal((await fetch(backend + '/api/devices/test-board/telemetry', { method: 'POST', headers, body: JSON.stringify(value) })).status, 200);
  // Exercise real command enqueue, delivery and final-result persistence.
  const login = await fetch(backend + '/api/login', { method: 'POST', headers: { 'content-type': 'application/json', origin: backend }, body: JSON.stringify({ username: env.ADMIN_USERNAME, password }) });
  assert.equal(login.status, 200);
  const cookie = login.headers.get('set-cookie').split(';')[0];
  const state = await (await fetch(backend + '/api/admin/state', { headers: { cookie } })).json();
  const queued = await fetch(backend + '/api/admin/devices/test-board/commands', { method: 'POST', headers: { cookie, origin: backend, 'x-csrf-token': state.csrf, 'content-type': 'application/json' }, body: JSON.stringify({ type: 'device.ping', payload: {} }) });
  assert.equal(queued.status, 201);
  const delivered = await (await fetch(backend + '/api/devices/test-board/commands', { headers })).json();
  const command = delivered.commands[0]; assert.ok(command.traceparent);
  assert.equal((await fetch(backend + `/api/devices/test-board/commands/${command.id}/result`, { method: 'POST', headers: { ...headers, traceparent: command.traceparent }, body: JSON.stringify({ status: 'succeeded', result: { reply: 'pong' } }) })).status, 200);
  const liveTrace = await until(async () => {
    const response = await fetch(tempo + '/api/traces/' + traceId, { headers: { accept: 'application/json' } });
    if (!response.ok) return false;
    const value = await response.json(), text = JSON.stringify(value);
    return ['sensor.read', 'db.reading', 'reading.collect', 'res-ac-backend', 'res-ac-agent', 'res-ac-avr'].every(item => text.includes(item)) ? value : false;
  }, 'correlated device/backend spans did not reach Tempo');
  const resourceSpans = liveTrace.batches || liveTrace.resourceSpans;
  const traceSpans = resourceSpans.flatMap(group => (group.scopeSpans || group.instrumentationLibrarySpans).flatMap(scope => scope.spans));
  const httpClient = traceSpans.find(span => span.name === 'http.device' && span.spanId === traceSpans.find(item => item.name === 'http.server').parentSpanId);
  assert.ok(httpClient, 'Tempo server span must nest under the reconstructed device HTTP client');
  await until(async () => {
    const response = await fetch(prometheus + '/api/v1/query?' + new URLSearchParams({ query: 'resac_http_requests_total' }));
    return response.ok && (await response.json()).data?.result?.length;
  }, 'metrics did not reach Prometheus');
  await until(async () => {
    const response = await fetch(loki + '/loki/api/v1/query_range?' + new URLSearchParams({ query: `{service_name="res-ac"} | json | trace_id="${traceId}"` }));
    return response.ok && (await response.json()).data?.result?.length;
  }, 'correlated logs did not reach Loki');
  await until(async () => {
    const response = await fetch(grafana + '/api/dashboards/uid/res-ac-observability', { headers: { authorization: 'Basic ' + Buffer.from('admin:admin').toString('base64') } });
    return response.ok && (await response.json()).dashboard.panels.length >= 20;
  }, 'Grafana dashboard was not provisioned');
  const publicLogin = await fetch(backend + '/grafana/login');
  assert.equal(publicLogin.status, 200);
  const loginHtml = await publicLogin.text();
  assert.match(loginHtml, /<base href="\/grafana\/"/);
  const assetPath = loginHtml.match(/src="([^"]+\.js[^"]*)"/)[1];
  assert.equal((await fetch(new URL(assetPath, backend + '/grafana/'))).status, 200);
  assert.equal((await fetch(backend + '/grafana/api/dashboards/uid/res-ac-observability')).status, 401);
  const grafanaAuth = { authorization: 'Basic ' + Buffer.from('admin:admin').toString('base64') };
  assert.equal((await fetch(backend + '/grafana/api/dashboards/uid/res-ac-observability', { headers: grafanaAuth })).status, 200);
  const tempoSource = await (await fetch(grafana + '/api/datasources/uid/tempo', { headers: grafanaAuth })).json();
  const lokiSource = await (await fetch(grafana + '/api/datasources/uid/loki', { headers: grafanaAuth })).json();
  await until(async () => {
    const response = await fetch(prometheus + '/api/v1/query?' + new URLSearchParams({ query: 'increase(resac_trace_export_errors_total[5m])' }));
    return response.ok && (await response.json()).data?.result?.length;
  }, 'rate/increase charts need at least two Prometheus scrapes');
  // Exercise dashboard expressions against a real Prometheus, including pairs of
  // metrics with identical labels and device requests with different kinds.
  const dashboard = await (await fetch(grafana + '/api/dashboards/uid/res-ac-observability', { headers: grafanaAuth })).json();
  for (const panel of dashboard.dashboard.panels) {
    assert.ok(panel.description?.trim(), `Missing description: ${panel.title}`);
    if (panel.datasource.uid !== 'prometheus') continue;
    const names = new Set();
    for (const target of panel.targets) {
      const response = await fetch(prometheus + '/api/v1/query?' + new URLSearchParams({ query: target.expr }));
      assert.equal(response.status, 200, `Invalid PromQL: ${panel.title}`);
      const data = await response.json(); assert.equal(data.status, 'success');
      for (const series of data.data.result) {
        const name = target.legendFormat.replace(/\{\{([^}]+)\}\}/g, (_, key) => series.metric[key] || '').trim();
        assert.ok(name, `Empty legend: ${panel.title}`);
        assert.ok(!names.has(name), `Duplicate legend ${name}: ${panel.title}`); names.add(name);
      }
    }
    if (panel.id === 5) assert.equal(names.size, 2, 'Contact and diagnostic ages must both be visible');
    if (panel.id === 21) assert.equal(names.size, 4, 'Export failures and dropped records must remain separate');
  }
  assert.ok(tempoSource.jsonData.tracesToLogsV2.query.includes('${__trace.traceId}'));
  assert.equal(lokiSource.jsonData.derivedFields[0].url, '${__value.raw}');
});
