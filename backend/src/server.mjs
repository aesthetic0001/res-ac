import http from 'node:http';
import https from 'node:https';
import { setTimeout as sleep } from 'node:timers/promises';
import { loadConfig } from './config.mjs';
import { Store } from './store.mjs';
import { createApp } from './app.mjs';
import { readIdentity, watchIdentity } from './tls.mjs';

process.umask(0o077);
const config = loadConfig();
const store = new Store(config.dbPath, config.devices);
const app = createApp(config, store);
let server, stopWatcher = () => {}, stopping = false;
if (config.httpDev) {
  console.warn('HTTP_DEV is enabled; use only for local testing.');
  server = http.createServer(app.handler);
} else {
  let current;
  while (!current) {
    try { current = readIdentity(config.tlsDir); }
    catch {
      console.info('Waiting for a valid TLS certificate from Certbot...');
      await sleep(5000);
    }
  }
  server = https.createServer(current.identity, app.handler);
  stopWatcher = watchIdentity(server, config.tlsDir);
}
server.requestTimeout = 15_000; // Limits receiving a request; does not limit the long-poll response wait.
server.headersTimeout = 10_000;
server.timeout = 45_000;
server.listen(config.port, config.host, () => console.info(`${config.httpDev ? 'HTTP' : 'HTTPS'} backend listening on port ${config.port}`));
server.on('error', error => { console.error('Server error:', error.code || error.name); process.exitCode = 1; });
function stop() {
  if (stopping) return;
  stopping = true;
  stopWatcher(); app.close();
  server.close(() => { store.close(); process.exit(0); });
  setTimeout(() => process.exit(1), 10_000).unref();
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
