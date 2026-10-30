import http from 'node:http';
import https from 'node:https';
import { readFileSync } from 'node:fs';
import { X509Certificate } from 'node:crypto';
import { join } from 'node:path';
const dev = process.env.HTTP_DEV === 'true';
if (!dev) {
  try {
    const cert = new X509Certificate(readFileSync(join(process.env.TLS_DIR || '/run/sensor-tls', 'fullchain.pem')));
    if (Date.parse(cert.validTo) <= Date.now()) process.exit(1);
  } catch { process.exit(1); }
}
const request = (dev ? http : https).get({ host: '127.0.0.1', port: process.env.PORT || 3000, path: '/healthz', rejectUnauthorized: false, timeout: 3000 }, response => {
  response.resume(); process.exitCode = response.statusCode === 200 ? 0 : 1;
});
// This loopback-only health check checks liveness; external clients must validate certificates normally.
request.on('error', () => { process.exitCode = 1; });
request.on('timeout', () => request.destroy());
