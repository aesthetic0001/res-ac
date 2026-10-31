import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { token, digest, equal, verifyPassword, cookieToken } from './auth.mjs';
import { curveSettings } from './fan-curve.mjs';

class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }
const assert = (test, message) => { if (!test) throw new HttpError(400, message); };
function json(res, status, value) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' });
  res.end(status === 204 ? undefined : JSON.stringify(value));
}
async function body(req) {
  if (!/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')) throw new HttpError(415, 'Use application/json');
  let size = 0;
  const chunks = [];
  await new Promise((resolve, reject) => {
    const data = chunk => {
      size += chunk.length;
      if (size > 32_768) {
        req.off('data', data);
        req.resume();
        reject(new HttpError(413, 'Request body is too large'));
        return;
      }
      chunks.push(chunk);
    };
    req.on('data', data);
    req.once('end', resolve);
    req.once('error', reject);
    req.once('aborted', () => reject(new HttpError(400, 'Request was aborted')));
  });
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    assert(value && typeof value === 'object' && !Array.isArray(value), 'Expected a JSON object');
    return value;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(400, 'Invalid JSON');
  }
}
function sameOrigin(req, config) {
  const expected = `${config.httpDev ? 'http' : 'https'}://${req.headers.host}`;
  if (req.headers.origin !== expected) throw new HttpError(403, 'Origin does not match this server');
}
export function createApp(config, store) {
  const assets = new Map([
    ['/login', ['login.html', 'text/html']], ['/', ['index.html', 'text/html']],
    ['/assets/style.css', ['style.css', 'text/css']], ['/assets/app.js', ['app.js', 'text/javascript']],
    ['/assets/login.js', ['login.js', 'text/javascript']],
    ['/assets/graphs.js', ['graphs.js', 'text/javascript']],
    ['/assets/fan-curve.js', ['fan-curve.js', 'text/javascript']]
  ].map(([route, [name, type]]) => [route, { data: readFileSync(fileURLToPath(new URL(`../public/${name}`, import.meta.url))), type }]));
  const waiters = new Map(), limits = new Map();
  const notify = deviceId => waiters.get(deviceId)?.wake();
  let shuttingDown = false;
  const authVersion = digest(config.adminUsername + '\0' + config.passwordHash);
  function session(req) {
    const found = store.session(cookieToken(req.headers.cookie));
    return found && equal(found.auth_version, authVersion) ? found : null;
  }
  function requireAdmin(req, mutation = false) {
    const found = session(req);
    if (!found) throw new HttpError(401, 'Please sign in');
    if (mutation) {
      sameOrigin(req, config);
      if (!equal(req.headers['x-csrf-token'] || '', found.csrf)) throw new HttpError(403, 'Invalid CSRF token');
    }
    return found;
  }
  function requireDevice(req, id) {
    const supplied = req.headers.authorization || '';
    const found = store.device(id);
    if (!found || !supplied.startsWith('Bearer ') || !equal(digest(supplied.slice(7)), found.token_hash)) throw new HttpError(401, 'Invalid device credentials');
    store.seen(id);
  }
  function poll(req, res, deviceId) {
    if (waiters.has(deviceId)) throw new HttpError(409, 'Only one command poll may be open per device');
    const deadline = Date.now() + config.pollWaitMs;
    let timer, stopped = false;
    const cleanup = () => {
      stopped = true; clearTimeout(timer); waiters.delete(deviceId); res.off('close', cleanup);
    };
    const wake = () => {
      if (stopped) return;
      clearTimeout(timer);
      if (res.destroyed) return cleanup();
      try {
        const command = store.claim(deviceId, config.leaseMs);
        if (command) { cleanup(); json(res, 200, { commands: [command] }); return; }
        if (Date.now() >= deadline) { cleanup(); json(res, 204); return; }
        timer = setTimeout(wake, Math.min(1000, deadline - Date.now()));
      } catch {
        cleanup(); json(res, 500, { error: 'Could not retrieve commands' });
      }
    };
    waiters.set(deviceId, { wake, close: () => { cleanup(); json(res, 503, { error: 'Server is restarting; reconnect shortly' }); } });
    res.on('close', cleanup);
    wake();
  }
  const handler = async (req, res) => {
    res.setHeader('x-content-type-options', 'nosniff');
    res.setHeader('x-frame-options', 'DENY');
    res.setHeader('referrer-policy', 'no-referrer');
    res.setHeader('content-security-policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
    if (!config.httpDev) res.setHeader('strict-transport-security', 'max-age=31536000');
    res.setHeader('cache-control', 'no-store');
    try {
      const url = new URL(req.url, 'http://local');
      const path = url.pathname;
      if (req.method === 'GET' && path === '/healthz') {
        store.db.prepare('SELECT 1').get();
        return json(res, shuttingDown ? 503 : 200, { ok: !shuttingDown });
      }
      if (shuttingDown) throw new HttpError(503, 'Server is restarting');
      if (req.method === 'POST' && path === '/api/login') {
        sameOrigin(req, config);
        const ip = req.socket.remoteAddress;
        let limit = limits.get(ip);
        if (!limit || limit.until < Date.now()) {
          limit = { count: 0, until: Date.now() + 15 * 60_000 }; limits.set(ip, limit);
          if (limits.size > 10_000) limits.delete(limits.keys().next().value);
        }
        if (++limit.count > 10) throw new HttpError(429, 'Too many login attempts; try again in 15 minutes');
        const value = await body(req);
        assert(typeof value.username === 'string' && typeof value.password === 'string' && value.password.length <= 1024, 'Invalid login');
        // Always perform password verification, even for an unknown username.
        const valid = await verifyPassword(value.password, config.passwordHash);
        if (!equal(value.username, config.adminUsername) || !valid) throw new HttpError(401, 'Incorrect username or password');
        limits.delete(ip);
        const old = cookieToken(req.headers.cookie);
        if (old) store.logout(old);
        const key = token(), csrf = token();
        store.createSession(key, csrf, Date.now() + config.sessionMs, authVersion);
        res.setHeader('set-cookie', `session=${key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${Math.floor(config.sessionMs / 1000)}${config.httpDev ? '' : '; Secure'}`);
        return json(res, 200, { ok: true });
      }
      if (req.method === 'POST' && path === '/api/logout') {
        requireAdmin(req, true); store.logout(cookieToken(req.headers.cookie));
        res.setHeader('set-cookie', `session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0${config.httpDev ? '' : '; Secure'}`);
        return json(res, 200, { ok: true });
      }
      if (req.method === 'GET' && path === '/api/admin/state') {
        const active = requireAdmin(req);
        return json(res, 200, { ...store.snapshot(), csrf: active.csrf, username: config.adminUsername });
      }
      const adminReadings = path.match(/^\/api\/admin\/devices\/([a-zA-Z0-9_-]+)\/readings$/);
      if (req.method === 'GET' && adminReadings) {
        requireAdmin(req);
        const deviceId = adminReadings[1];
        if (!store.device(deviceId)) throw new HttpError(404, 'Device not found');
        const range = url.searchParams.get('hours') ?? '24';
        assert(['1', '6', '24', '168'].includes(range), 'Hours must be 1, 6, 24, or 168');
        return json(res, 200, store.readingHistory(deviceId, Number(range)));
      }
      const adminCommand = path.match(/^\/api\/admin\/devices\/([a-zA-Z0-9_-]+)\/commands$/);
      if (req.method === 'POST' && adminCommand) {
        requireAdmin(req, true);
        const deviceId = adminCommand[1];
        if (!store.device(deviceId)) throw new HttpError(404, 'Device not found');
        const value = await body(req);
        assert(typeof value.type === 'string' && /^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$/.test(value.type), 'Invalid command type');
        assert(value.payload && typeof value.payload === 'object' && !Array.isArray(value.payload), 'Payload must be a JSON object');
        if (value.type === 'fan.level') assert(Number.isInteger(value.payload.level) && value.payload.level >= 0 && value.payload.level <= 5 && Object.keys(value.payload).length === 2, 'Level must be 0 (off) to 5, with sessionId');
        if (value.type === 'fan.curve') {
          try { curveSettings(value.payload); } catch (error) { throw new HttpError(400, error.message); }
        }
        if (['fan.command', 'ir.send', 'fan.calibrate', 'fan.level', 'fan.curve'].includes(value.type)) {
          const fan = store.fanState(deviceId);
          if (!fan || Date.now() - fan.receivedAt > 30_000 || !fan.sessionId || value.payload.sessionId !== fan.sessionId)
            throw new HttpError(409, 'Fan status is missing, stale, or changed. Wait for the device and recalibrate.');
          if (['fan.level', 'fan.curve'].includes(value.type) && fan.firmwareVersion !== 'v4') throw new HttpError(409, 'Update firmware for cyclic levels and automatic curves');
          if (value.type === 'fan.calibrate') {
            assert(value.payload.confirmOff === true && Object.keys(value.payload).length === 2, 'Confirm the fan was restarted to factory position: off');
          } else if (!fan.calibrated && !(value.type === 'fan.curve' && value.payload.enabled === false)) throw new HttpError(409, 'Calibration required: restart fan to factory position: off');
        }
        const ttl = value.ttlSeconds ?? 300;
        assert(Number.isInteger(ttl) && ttl >= 1 && ttl <= 86400, 'TTL must be between 1 and 86400 seconds');
        const command = store.enqueue(deviceId, value.type, value.payload, ttl);
        notify(deviceId);
        return json(res, 201, { id: command.id, status: command.status, expiresAt: new Date(command.expires_at).toISOString() });
      }
      const deviceRoute = path.match(/^\/api\/devices\/([a-zA-Z0-9_-]+)\/(readings|commands|fan-state)(?:\/([a-f0-9-]+)\/result)?$/);
      if (deviceRoute) {
        const [, id, route, commandId] = deviceRoute;
        requireDevice(req, id);
        if (req.method === 'POST' && route === 'fan-state' && !commandId) {
          const value = await body(req);
          assert(value.sessionId === null || (typeof value.sessionId === 'string' && /^[a-f0-9]{32}$/.test(value.sessionId)), 'Invalid fan session');
          assert(typeof value.calibrated === 'boolean' && ['on', 'off', 'unknown'].includes(value.power), 'Invalid fan estimate');
          assert(Number.isInteger(value.revision) && value.revision >= 0 && value.revision <= 0xffffffff, 'Invalid fan revision');
          assert(value.calibrated
            ? value.sessionId !== null && ((value.power === 'off' && value.speed === 0) || (value.power === 'on' && Number.isInteger(value.speed) && value.speed >= 1 && value.speed <= 5))
            : value.power === 'unknown' && value.speed === null, 'Inconsistent fan estimate');
          const state = { sessionId: value.sessionId, calibrated: value.calibrated, power: value.power, speed: value.speed, revision: value.revision };
          if (value.firmwareVersion !== undefined) {
            assert(['v3', 'v4'].includes(value.firmwareVersion), 'Invalid firmware version');
            state.firmwareVersion = value.firmwareVersion;
          }
          if (value.controlSource !== undefined) {
            assert(['reset', 'manual', 'automatic', 'calibration'].includes(value.controlSource), 'Invalid fan control source');
            state.controlSource = value.controlSource;
          }
          if (value.automatic !== undefined) {
            const automatic = value.automatic;
            try {
              state.automatic = curveSettings({ enabled: automatic.enabled, thresholdsC: automatic.thresholdsC, hysteresisC: automatic.hysteresisC });
            } catch { throw new HttpError(400, 'Invalid automatic curve state'); }
            assert(['disabled', 'waiting_for_sample', 'calibration_required', 'sensor_unavailable', 'waiting_for_device', 'cooldown', 'adjusting', 'active', 'manual_override', 'command_error', 'config_error'].includes(automatic.status), 'Invalid automatic status');
            assert(automatic.targetLevel === null || (Number.isInteger(automatic.targetLevel) && automatic.targetLevel >= 0 && automatic.targetLevel <= 5), 'Invalid automatic target');
            Object.assign(state.automatic, { status: automatic.status, targetLevel: automatic.targetLevel, minAdjustmentSeconds: 30 });
          }
          if (!store.setFanState(id, state)) throw new HttpError(409, 'Outdated fan revision');
          return json(res, 200, { ok: true });
        }
        if (req.method === 'POST' && route === 'readings' && !commandId) {
          const value = await body(req);
          assert(typeof value.temperatureC === 'number' && Number.isFinite(value.temperatureC) && value.temperatureC >= -80 && value.temperatureC <= 150, 'Invalid temperatureC');
          assert(typeof value.humidity === 'number' && Number.isFinite(value.humidity) && value.humidity >= 0 && value.humidity <= 100, 'Invalid humidity');
          if (value.readingId !== undefined) assert(typeof value.readingId === 'string' && value.readingId.length > 0 && value.readingId.length <= 100, 'Invalid readingId');
          if (value.recordedAt !== undefined) assert(typeof value.recordedAt === 'string' && value.recordedAt.length <= 40 && Number.isFinite(Date.parse(value.recordedAt)), 'Invalid recordedAt');
          const result = store.reading(id, value);
          return json(res, result.duplicate ? 200 : 201, result);
        }
        if (req.method === 'GET' && route === 'commands' && !commandId) return poll(req, res, id);
        if (req.method === 'POST' && route === 'commands' && commandId) {
          const value = await body(req);
          assert(['succeeded', 'failed'].includes(value.status), 'Status must be succeeded or failed');
          const result = store.result(id, commandId, value.status, value.result ?? null);
          if (result.code === 200) notify(id);
          return json(res, result.code, result.error ? { error: result.error } : { ok: true, duplicate: result.duplicate });
        }
        throw new HttpError(405, 'Method not allowed');
      }
      if (req.method === 'GET' && assets.has(path)) {
        if (path === '/' && !session(req)) { res.writeHead(303, { location: '/login' }); return res.end(); }
        const asset = assets.get(path);
        res.writeHead(200, { 'content-type': `${asset.type}; charset=utf-8` }); return res.end(asset.data);
      }
      throw new HttpError(404, 'Not found');
    } catch (error) {
      if (!error.status) console.error('Request failed:', error.name); // No bodies, passwords, tokens, or headers in logs.
      json(res, error.status || 500, { error: error.status ? error.message : 'Internal server error' });
    }
  };
  return { handler, close() { shuttingDown = true; for (const waiter of [...waiters.values()]) waiter.close(); } };
}
