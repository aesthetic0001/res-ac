import { AsyncLocalStorage } from 'node:async_hooks';
import { randomBytes } from 'node:crypto';
import { statSync, statfsSync } from 'node:fs';
import { dirname } from 'node:path';
import { X509Certificate } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { ROOT_CONTEXT, trace, SpanStatusCode, SpanKind } from '@opentelemetry/api';
import { TracerProvider, BatchSpanProcessor, RandomIdGenerator } from '@opentelemetry/sdk-trace';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { Registry, Counter, Gauge, Histogram, collectDefaultMetrics } from 'prom-client';

export const validTraceparent = value => typeof value === 'string' && /^00-(?!0{32})[a-f0-9]{32}-(?!0{16})[a-f0-9]{16}-01$/.test(value);
const serviceNames = { backend: 'res-ac-backend', agent: 'res-ac-agent', avr: 'res-ac-avr' };
const rootEvents = new Set(['reading.collect', 'command.local', 'http.request']);
const validSpanId = value => typeof value === 'string' && /^(?!0{16})[a-f0-9]{16}$/.test(value);
export const newTraceparent = () => `00-${randomBytes(16).toString('hex')}-${randomBytes(8).toString('hex')}-01`;
const parentContext = value => validTraceparent(value) ? trace.setSpanContext(ROOT_CONTEXT, {
  traceId: value.slice(3, 35), spanId: value.slice(36, 52), traceFlags: 1, isRemote: true
}) : ROOT_CONTEXT;
export const eventNames = new Set(['reading.collect', 'command.local', 'http.request', 'sensor.read', 'bridge.dispatch', 'bridge.collect', 'bridge.error', 'reading.buffer', 'reading.drop',
  'http.device', 'command.execute', 'avr.execute', 'telemetry.drop']);
const attributeNames = new Set(['reading.id', 'command.id', 'outcome', 'curl.code', 'http.status_code', 'request.kind', 'error.kind', 'avr.uptime_ms']);
export const deviceMetricNames = new Set(['cpu_ratio', 'memory_available_bytes', 'memory_total_bytes', 'agent_rss_bytes',
  'uptime_seconds', 'disk_free_bytes', 'sensor_errors_total', 'sensor_sample_age_seconds', 'buffer_depth', 'readings_dropped_total', 'telemetry_dropped_total', 'telemetry_queue_depth']);
export function validateTelemetry(value) {
  if (!value || typeof value.session !== 'string' || typeof value.batchId !== 'string' ||
      !/^[a-f0-9]{32}$/.test(value.session) || !/^[a-f0-9]{32}$/.test(value.batchId) ||
      !Array.isArray(value.events) || value.events.length > 32 || !value.metrics || typeof value.metrics !== 'object' || Array.isArray(value.metrics)) return false;
  for (const [key, number] of Object.entries(value.metrics)) {
    if (!deviceMetricNames.has(key) || typeof number !== 'number' || !Number.isFinite(number) || number < 0 || number > 1e16 || (key === 'cpu_ratio' && number > 1)) return false;
  }
  return value.events.every(event => event && eventNames.has(event.name) && validTraceparent(event.traceparent) &&
    (event.spanId === undefined || (validSpanId(event.spanId) &&
      (rootEvents.has(event.name) ? event.spanId === event.traceparent.slice(36, 52) : event.spanId !== event.traceparent.slice(36, 52)))) &&
    Number.isFinite(event.durationMs) && event.durationMs >= 0 && event.durationMs <= 86400_000 &&
    Number.isFinite(event.ageMs) && event.ageMs >= 0 && event.ageMs <= 7 * 86400_000 &&
    event.attributes && typeof event.attributes === 'object' && !Array.isArray(event.attributes) &&
    Object.entries(event.attributes).every(([key, val]) => attributeNames.has(key) &&
      ((typeof val === 'string' && /^[a-zA-Z0-9_.-]{1,100}$/.test(val)) || (typeof val === 'number' && Number.isFinite(val) && val >= 0 && val <= 1e16))));
}
const buckets = [.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 30.1, 31, 35, 40, 45, 60];
export function routeTemplate(path) {
  if (path === '/grafana' || path.startsWith('/grafana/')) return '/grafana/*';
  if (/^\/api\/devices\/[^/]+\/(readings|commands|fan-state|telemetry)(?:\/[^/]+\/result)?$/.test(path))
    return path.replace(/\/devices\/[^/]+/, '/devices/:id').replace(/\/commands\/[^/]+\/result$/, '/commands/:commandId/result');
  if (/^\/api\/admin\/devices\/[^/]+\/(readings|commands)$/.test(path)) return path.replace(/\/devices\/[^/]+/, '/devices/:id');
  return ['/', '/login', '/healthz', '/api/login', '/api/logout', '/api/admin/state'].includes(path) ? path : 'other';
}
export class Observability {
  constructor({ endpoint, exporter, lokiEndpoint, log = value => console.info(JSON.stringify(value)), defaults = true } = {}) {
    this.local = new AsyncLocalStorage(); this.registry = new Registry();
    this.logs = []; this.lokiEndpoint = lokiEndpoint; this.sendingLogs = false;
    this.log = value => {
      try { log(value); } catch {}
      if (lokiEndpoint) {
        if (this.logs.length >= 256) { this.logs.shift(); this.logDrops?.inc(); }
        this.logs.push(value);
      }
    };
    const raw = exporter || (endpoint ? new OTLPTraceExporter({ url: endpoint, timeoutMillis: 2000 }) : null);
    this.queuedSpans = 0;
    const batch = raw ? new BatchSpanProcessor({ exporter: { export: (spans, done) => { this.queuedSpans -= spans.length; raw.export(spans, result => {
      if (result.code) this.exportErrors.inc(); done(result);
    }); }, shutdown: () => raw.shutdown(), forceFlush: () => raw.forceFlush?.() || Promise.resolve() }, maxQueueSize: 2048, maxExportBatchSize: 128, scheduledDelayMillis: 1000, exportTimeoutMillis: 2500 }) : null;
    const processors = batch ? [{ onStart: (...args) => batch.onStart(...args), onEnd: span => {
      if (this.queuedSpans >= 2048) { this.traceDrops.inc(); return; }
      this.queuedSpans++; batch.onEnd(span);
    }, forceFlush: () => batch.forceFlush(), shutdown: () => batch.shutdown() }] : [];
    const randomIds = new RandomIdGenerator();
    // Providers carry immutable source identities. A shared processor keeps one
    // bounded export queue and one exporter lifecycle across all three resources.
    this.providers = Object.fromEntries(Object.entries(serviceNames).map(([component, service]) => [component,
      new TracerProvider({ idGenerator: { generateTraceId: () => this.forcedRoot?.traceId || randomIds.generateTraceId(),
        generateSpanId: () => this.forcedRoot?.spanId || randomIds.generateSpanId() },
      resource: resourceFromAttributes({ 'service.name': service }), spanProcessors: processors })]));
    this.tracers = Object.fromEntries(Object.entries(this.providers).map(([component, provider]) => [component, provider.getTracer('res-ac')]));
    this.provider = this.providers.backend;
    this.tracer = this.tracers.backend;
    const counter = (name, labels = []) => new Counter({ name: `resac_${name}`, help: name, labelNames: labels, registers: [this.registry] });
    const gauge = (name, labels = []) => new Gauge({ name: `resac_${name}`, help: name, labelNames: labels, registers: [this.registry] });
    const histogram = (name, labels) => new Histogram({ name: `resac_${name}`, help: name, labelNames: labels, buckets, registers: [this.registry] });
    this.requests = counter('http_requests_total', ['route', 'method', 'status']);
    this.latency = histogram('http_duration_seconds', ['route', 'method']);
    this.dbTime = histogram('db_duration_seconds', ['operation', 'outcome']);
    this.exportErrors = counter('trace_export_errors_total');
    this.traceDrops = counter('trace_dropped_total');
    this.logDrops = counter('log_dropped_total');
    this.logErrors = counter('log_export_errors_total');
    this.logTimer = lokiEndpoint ? setInterval(() => this.flushLogs(), 1000) : null;
    this.logTimer?.unref();
    this.deviceEvents = counter('device_events_total', ['device', 'operation', 'outcome']);
    this.deviceHttp = counter('device_requests_total', ['device', 'kind', 'outcome']);
    this.deviceLatency = histogram('device_request_duration_seconds', ['device', 'kind']);
    this.operationLatency = histogram('device_operation_duration_seconds', ['device', 'operation']);
    this.queueTime = histogram('command_queue_duration_seconds', ['device']);
    this.deviceValues = gauge('device_value', ['device', 'metric']);
    this.heartbeat = gauge('device_telemetry_timestamp_seconds', ['device']);
    this.contact = gauge('device_contact_timestamp_seconds', ['device']);
    this.fresh = gauge('device_reading_received_timestamp_seconds', ['device']);
    this.rows = gauge('db_rows', ['table']); this.fileSize = gauge('db_bytes', ['file']);
    this.commands = gauge('commands', ['status']); this.attempts = gauge('command_delivery_attempts');
    this.ready = gauge('backend_ready'); this.ready.set(0);
    this.tlsReload = counter('tls_reload_total', ['outcome']);
    this.disk = gauge('backend_disk_available_bytes'); this.tls = gauge('tls_expiry_timestamp_seconds');
    this.recentBatches = new Map(); this.knownDevices = new Set();
    if (defaults) collectDefaultMetrics({ register: this.registry, prefix: 'resac_' });
  }
  currentTraceparent() {
    const span = this.local.getStore(); if (!span) return undefined;
    const value = span.spanContext(); return `00-${value.traceId}-${value.spanId}-01`;
  }
  span(name, attributes = {}, parent, startTime, kind = SpanKind.INTERNAL) {
    const component = serviceNames[attributes.component] ? attributes.component : 'backend';
    return this.tracers[component].startSpan(name, { attributes: { component, ...attributes }, startTime, kind }, parent ? parentContext(parent) : (this.local.getStore() ? trace.setSpan(ROOT_CONTEXT, this.local.getStore()) : ROOT_CONTEXT));
  }
  run(name, attributes, fn, parent) {
    const span = this.span(name, attributes, parent), started = performance.now(); let failed = false;
    return this.local.run(span, () => {
      try { const result = fn(); span.setStatus({ code: SpanStatusCode.OK }); return result; }
      catch (error) { failed = true; span.setAttribute('error.type', /^[A-Za-z0-9_]{1,64}$/.test(error.code || error.name) ? (error.code || error.name) : 'Error'); span.setStatus({ code: SpanStatusCode.ERROR, message: 'operation_failed' }); throw error; }
      finally { span.end(); if (name.startsWith('db.')) this.dbTime.observe({ operation: name.slice(3), outcome: failed ? 'error' : 'ok' }, (performance.now() - started) / 1000); }
    });
  }
  record(name, parent, start, duration, attributes = {}, spanId) {
    const component = serviceNames[attributes.component] ? attributes.component : 'backend';
    attributes = { component, ...attributes };
    const kind = ['http.device', 'http.request'].includes(name) ? SpanKind.CLIENT : SpanKind.INTERNAL;
    const root = rootEvents.has(name) && validTraceparent(parent);
    let span;
    // Reconstruct the ID the agent propagated before making the HTTP request.
    // Old agents omit spanId; their existing parent-context format still works.
    if (root || validSpanId(spanId)) {
      this.forcedRoot = { traceId: parent.slice(3, 35), spanId: spanId || parent.slice(36, 52) };
      try { span = root ? this.tracers[component].startSpan(name, { attributes, startTime: start, kind }, ROOT_CONTEXT)
        : this.span(name, attributes, parent, start, kind); }
      finally { this.forcedRoot = null; }
    } else span = this.span(name, attributes, parent, start, kind);
    const outcome = attributes.outcome || 'ok';
    span.setStatus({ code: ['ok', 'succeeded'].includes(outcome) ? SpanStatusCode.OK : SpanStatusCode.ERROR });
    span.end(start + duration);
    const value = span.spanContext();
    this.log({ time: new Date().toISOString(), service: serviceNames[component], operation: name, trace_id: value.traceId, span_id: value.spanId, ...attributes });
  }
  request(req, res, fn) {
    const route = routeTemplate((req.url || '').split('?')[0]);
    const method = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'].includes(req.method) ? req.method : 'OTHER';
    const span = this.span('http.server', { 'http.route': route, 'http.request.method': method }, !route.endsWith('/telemetry') && validTraceparent(req.headers.traceparent) ? req.headers.traceparent : undefined, undefined, SpanKind.SERVER);
    const started = performance.now(); let finished = false;
    const end = aborted => {
      if (finished) return; finished = true;
      const status = aborted ? 'aborted' : String(res.statusCode);
      this.requests.inc({ route, method, status }); this.latency.observe({ route, method }, (performance.now() - started) / 1000);
      span.setAttribute('http.response.status_code', res.statusCode);
      span.setStatus({ code: aborted || res.statusCode >= 500 ? SpanStatusCode.ERROR : SpanStatusCode.OK }); span.end();
      this.log({ time: new Date().toISOString(), service: 'res-ac-backend', operation: 'http.server', route, method, status, trace_id: span.spanContext().traceId, span_id: span.spanContext().spanId });
    };
    res.once('finish', () => end(false)); res.once('close', () => end(!res.writableFinished));
    return this.local.run(span, fn);
  }
  ingest(device, value) {
    // Bounded retry deduplication. Do not let a lost response double-count a batch.
    const previous = this.recentBatches.get(device) || [];
    if (previous.includes(value.batchId)) return;
    previous.push(value.batchId); this.recentBatches.set(device, previous.slice(-128));
    this.heartbeat.set({ device }, Date.now() / 1000);
    for (const metric of deviceMetricNames) this.deviceValues.remove({ device, metric });
    for (const [metric, number] of Object.entries(value.metrics)) this.deviceValues.set({ device, metric }, number);
    for (const event of value.events) {
      const attributes = { ...event.attributes, 'device.id': device, 'device.session': value.session, 'timing.approximate': true, component: ['sensor.read', 'avr.execute'].includes(event.name) ? 'avr' : 'agent' };
      this.record(event.name, event.traceparent, Date.now() - event.ageMs - event.durationMs, event.durationMs, attributes, event.spanId);
      const outcome = ['ok', 'succeeded', 'failed', 'timeout', 'dns', 'connect', 'tls', 'other', 'dropped'].includes(attributes.outcome) ? attributes.outcome : 'other';
      this.deviceEvents.inc({ device, operation: event.name, outcome });
      this.operationLatency.observe({ device, operation: event.name }, event.durationMs / 1000);
      if (['http.device', 'http.request'].includes(event.name)) {
        const kind = ['readings', 'commands', 'result', 'fan-state'].includes(attributes['request.kind']) ? attributes['request.kind'] : 'other';
        this.deviceHttp.inc({ device, kind, outcome }); this.deviceLatency.observe({ device, kind }, event.durationMs / 1000);
      }
    }
  }
  collect(store, config) {
    try {
      store.expire();
      for (const table of ['readings', 'commands', 'devices', 'sessions']) this.rows.set({ table }, store.db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n);
      this.commands.reset();
      for (const row of store.db.prepare('SELECT status, COUNT(*) AS n FROM commands GROUP BY status').all()) this.commands.set({ status: row.status }, row.n);
      this.attempts.set(store.db.prepare('SELECT COALESCE(SUM(attempts),0) AS n FROM commands').get().n);
      for (const row of store.db.prepare('SELECT id,last_seen FROM devices').all()) {
        if (!this.knownDevices.has(row.id)) { this.heartbeat.set({ device: row.id }, 0); this.knownDevices.add(row.id); }
        this.contact.set({ device: row.id }, (row.last_seen || 0) / 1000);
        const latest = store.db.prepare('SELECT received_at FROM readings WHERE device_id=? ORDER BY id DESC LIMIT 1').get(row.id);
        this.fresh.set({ device: row.id }, (latest?.received_at || 0) / 1000);
      }
      for (const [file, suffix] of [['main', ''], ['wal', '-wal']]) {
        let size = 0; try { size = statSync(config.dbPath + suffix).size; } catch {} this.fileSize.set({ file }, size);
      }
      const fs = statfsSync(dirname(config.dbPath)); this.disk.set(fs.bavail * fs.bsize);
      if (!config.httpDev) this.tls.set(Date.parse(new X509Certificate(readFileSync(`${config.tlsDir}/fullchain.pem`)).validTo) / 1000);
    } catch { this.log({ operation: 'metrics.collect', outcome: 'failed' }); }
  }
  async flushLogs() {
    if (this.sendingLogs || !this.logs.length) return;
    this.sendingLogs = true;
    const batch = this.logs.splice(0, 128);
    try {
      const response = await fetch(this.lokiEndpoint, { method: 'POST', headers: { 'content-type': 'application/json' },
        signal: AbortSignal.timeout(2000), body: JSON.stringify({ streams: [{ stream: { service_name: 'res-ac' },
          values: batch.map((value, i) => [String(BigInt(Date.now()) * 1000000n + BigInt(i)), JSON.stringify(value)]) }] }) });
      if (!response.ok) throw new Error('log_export_failed');
    } catch { this.logErrors.inc(); this.logDrops.inc(batch.length); }
    finally { this.sendingLogs = false; }
  }
  async close() { clearInterval(this.logTimer); await this.flushLogs(); const results = await Promise.allSettled(Object.values(this.providers).map(provider => provider.shutdown()));
    if (results.some(result => result.status === 'rejected')) this.exportErrors.inc(); }
}
