import { resolve } from 'node:path';
export function loadConfig(env = process.env) {
  for (const name of ['ADMIN_USERNAME', 'ADMIN_PASSWORD_HASH', 'DEVICE_ID', 'DEVICE_TOKEN']) {
    if (!env[name]) throw new Error(`${name} is required. Run ./deploy.sh first.`);
  }
  if (!/^scrypt:[a-f0-9]{32}:[a-f0-9]{128}$/.test(env.ADMIN_PASSWORD_HASH)) throw new Error('Invalid ADMIN_PASSWORD_HASH');
  if (!/^[a-zA-Z0-9_-]{1,64}$/.test(env.DEVICE_ID)) throw new Error('Invalid DEVICE_ID');
  if (env.DEVICE_TOKEN.length < 32) throw new Error('DEVICE_TOKEN must have at least 32 characters');
  const port = Number(env.PORT || 3000);
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid PORT');
  return {
    port, host: env.HOST || '0.0.0.0', httpDev: env.HTTP_DEV === 'true',
    dbPath: resolve(env.DATA_DIR || './data', 'sensors.sqlite'),
    tlsDir: resolve(env.TLS_DIR || '/run/sensor-tls'),
    adminUsername: env.ADMIN_USERNAME, passwordHash: env.ADMIN_PASSWORD_HASH,
    devices: [{ id: env.DEVICE_ID, token: env.DEVICE_TOKEN }],
    pollWaitMs: 30_000, leaseMs: 60_000, sessionMs: 24 * 60 * 60 * 1000
  };
}
