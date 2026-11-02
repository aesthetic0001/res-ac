import { createGrafanaProxy } from './grafana-proxy.mjs';
import { Observability } from './observability.mjs';
import http from 'node:http';
import https from 'node:https';
import { setTimeout as sleep } from 'node:timers/promises';
import { loadConfig } from './config.mjs';
import { Store } from './store.mjs';
import { createApp } from './app.mjs';
import { readIdentity, watchIdentity } from './tls.mjs';

process.umask(0o077);
const config = loadConfig();
const observability = new Observability({ endpoint: process.env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, lokiEndpoint: process.env.LOKI_PUSH_ENDPOINT });
const store = new Store(config.dbPath, config.devices, observability);
observability.collect(store, config);
const metricTimer = setInterval(() => observability.collect(store, config), 60_000);
metricTimer.unref();
let metricsServer;
if (process.env.METRICS_PORT) {
  metricsServer = http.createServer(async (req, res) => {
    if (req.url !== '/metrics' || req.method !== 'GET') { res.writeHead(404); return res.end(); }
    try { res.setHeader('content-type', observability.registry.contentType); res.end(await observability.registry.metrics()); }
    catch { res.writeHead(500); res.end(); }
  });
  metricsServer.on('error', () => observability.log({ operation: 'metrics.listener', outcome: 'failed' }));
  metricsServer.listen(Number(process.env.METRICS_PORT), '0.0.0.0');
}
const app = createApp(config, store, observability);
const grafana = process.env.GRAFANA_PROXY_URL ? createGrafanaProxy(process.env.GRAFANA_PROXY_URL, { protocol: config.httpDev ? 'http' : 'https' }) : null;
const handler = (req, res) => {
  const path = (req.url || '').split('?')[0];
  if (grafana && (path === '/grafana' || path.startsWith('/grafana/')))
    return observability.request(req, res, () => grafana.handle(req, res));
  return app.handler(req, res);
};
let server, stopWatcher = () => {}, stopping = false;
if (config.httpDev) {
  console.warn('HTTP_DEV is enabled; use only for local testing.');
  server = http.createServer(handler);
} else {
  let current;
  while (!current) {
    try { current = readIdentity(config.tlsDir); }
    catch {
      console.info('Waiting for a valid TLS certificate from Certbot...');
      await sleep(5000);
    }
  }
  server = https.createServer(current.identity, handler);
  const tlsEvent = outcome => {
    observability.tlsReload.inc({ outcome });
    observability.record('tls.reload', undefined, Date.now(), 0, { outcome });
  };
  stopWatcher = watchIdentity(server, config.tlsDir, { log: { info: () => tlsEvent('ok'), error: () => tlsEvent('failed') } });
}
if (grafana) server.on('upgrade', (req, socket, head) => grafana.upgrade(req, socket, head));
server.requestTimeout = 15_000; // Limits receiving a request; does not limit the long-poll response wait.
server.headersTimeout = 10_000;
server.timeout = 45_000;
server.listen(config.port, config.host, () => {
  observability.ready.set(1);
  console.info(`${config.httpDev ? 'HTTP' : 'HTTPS'} backend listening on port ${config.port}`);
});
server.on('error', error => { console.error('Server error:', error.code || error.name); process.exitCode = 1; });
function stop() {
  if (stopping) return;
  stopping = true; observability.ready.set(0);
  stopWatcher(); grafana?.close(); clearInterval(metricTimer); metricsServer?.close(); app.close();
  server.close(async () => { store.close(); await observability.close(); process.exit(0); });
  setTimeout(() => process.exit(1), 10_000).unref();
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
