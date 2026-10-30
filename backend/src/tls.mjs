import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { X509Certificate, createHash } from 'node:crypto';
import { createSecureContext } from 'node:tls';
export function readIdentity(directory) {
  const cert = readFileSync(join(directory, 'fullchain.pem'));
  const key = readFileSync(join(directory, 'privkey.pem'));
  const parsed = new X509Certificate(cert);
  if (Date.parse(parsed.validFrom) > Date.now() || Date.parse(parsed.validTo) <= Date.now()) throw new Error('TLS certificate is not currently valid');
  const identity = { cert, key, minVersion: 'TLSv1.2' };
  createSecureContext(identity); // Validate matching certificate/key before changing the live server.
  return { identity, fingerprint: createHash('sha256').update(cert).update(key).digest('hex'), expiresAt: parsed.validTo };
}
export function watchIdentity(server, directory, { intervalMs = 2000, log = console } = {}) {
  let fingerprint = readIdentity(directory).fingerprint;
  const timer = setInterval(() => {
    try {
      const current = readIdentity(directory);
      if (current.fingerprint !== fingerprint) {
        server.setSecureContext(current.identity);
        fingerprint = current.fingerprint;
        log.info('Renewed TLS certificate loaded; expires:', current.expiresAt);
      }
    } catch (error) {
      log.error('TLS reload deferred; retaining the current context:', error.code || error.name);
    }
  }, intervalMs);
  timer.unref();
  return () => clearInterval(timer);
}
