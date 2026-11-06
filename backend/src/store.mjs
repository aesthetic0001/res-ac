import { DatabaseSync } from './sqlite.mjs';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { digest } from './auth.mjs';
import { commandPayloadAttributes } from './observability.mjs';
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export class Store {
  constructor(path, devices = [], observability) {
    this.observability = observability;
    this.commitObservers = null;
    this.presence = new Map();
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, last_seen INTEGER);
      CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), reading_id TEXT NOT NULL,
        temperature_c REAL NOT NULL, humidity REAL NOT NULL, recorded_at TEXT, received_at INTEGER NOT NULL, UNIQUE(device_id,reading_id));
      CREATE INDEX IF NOT EXISTS reading_device_time ON readings(device_id,received_at DESC);
      CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id),type TEXT NOT NULL,payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,lease_until INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,result TEXT,completed_at INTEGER);
      CREATE INDEX IF NOT EXISTS command_device_time ON commands(device_id,created_at);
      CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,auth_version TEXT NOT NULL);`);
    this.db.exec(`CREATE TABLE IF NOT EXISTS fan_states(device_id TEXT PRIMARY KEY REFERENCES devices(id), state TEXT NOT NULL, received_at INTEGER NOT NULL);`);
    this.db.exec(`CREATE TABLE IF NOT EXISTS command_handoffs(previous_id TEXT PRIMARY KEY REFERENCES commands(id) ON DELETE CASCADE,
      next_id TEXT UNIQUE REFERENCES commands(id) ON DELETE CASCADE);`);
    if (!this.db.prepare('PRAGMA table_info(commands)').all().some(column => column.name === 'traceparent'))
      this.db.exec('ALTER TABLE commands ADD COLUMN traceparent TEXT');
    if (observability) {
      for (const name of ['transaction', 'device', 'seen', 'fanState', 'setFanState', 'reading', 'readingHistory', 'enqueue', 'command', 'claim', 'result', 'resultAndClaim', 'expire', 'createSession', 'session', 'logout', 'snapshot']) {
        const original = this[name].bind(this);
        this[name] = (...args) => observability.run(`db.${name}`, { 'db.system.name': 'sqlite' }, () => original(...args));
      }
    }
    for (const device of devices) this.db.prepare('INSERT INTO devices(id,token_hash) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash').run(device.id, digest(device.token));
  }
  close() {
    const pending = [...this.presence].filter(([, state]) => state.latest !== state.persisted);
    if (pending.length) this.transaction(() => {
      for (const [id, state] of pending)
        this.db.prepare('UPDATE devices SET last_seen=? WHERE id=?').run(state.latest, id);
    });
    this.db.close();
  }
  transaction(fn) {
    if (fn.constructor.name === 'AsyncFunction') throw new TypeError('SQLite transactions require synchronous callbacks');
    const execute = () => {
      const result = fn();
      if (result && typeof result.then === 'function') throw new TypeError('SQLite transactions cannot return promises');
      return result;
    };
    if (this.commitObservers !== null) return execute();
    this.db.exec('BEGIN IMMEDIATE');
    this.commitObservers = [];
    let result, observers;
    try {
      result = execute();
      this.db.exec('COMMIT');
      observers = this.commitObservers;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    } finally { this.commitObservers = null; }
    // Optional diagnostics must not prevent delivery of a committed command.
    for (const observe of observers) { try { observe(); } catch {} }
    return result;
  }
  afterCommit(fn) {
    if (this.commitObservers !== null) this.commitObservers.push(fn);
    else fn();
  }
  device(id) {
    const row = this.db.prepare('SELECT * FROM devices WHERE id=?').get(id);
    if (row && this.presence.has(id)) row.last_seen = this.presence.get(id).latest;
    return row;
  }
  seen(id) {
    let state = this.presence.get(id);
    if (!state) {
      const row = this.db.prepare('SELECT last_seen FROM devices WHERE id=?').get(id);
      if (!row) return;
      state = { latest: row.last_seen, persisted: row.last_seen };
      this.presence.set(id, state);
    }
    const now = Date.now();
    state.latest = now;
    // Presence stays exact in the live API. Persist at most once per five
    // seconds of incoming traffic; command/result commits remain fully durable.
    if (state.persisted === null || now < state.persisted || now - state.persisted >= 5000) {
      this.db.prepare('UPDATE devices SET last_seen=? WHERE id=?').run(now, id);
      this.afterCommit(() => { state.persisted = now; });
    }
  }
  fanState(id) {
    const row = this.db.prepare('SELECT state,received_at FROM fan_states WHERE device_id=?').get(id);
    return row ? { ...JSON.parse(row.state), receivedAt: row.received_at } : null;
  }
  setFanState(id, state) {
    const previous = this.fanState(id);
    if (previous?.sessionId === state.sessionId && previous.revision > state.revision) return false;
    this.db.prepare('INSERT INTO fan_states(device_id,state,received_at) VALUES(?,?,?) ON CONFLICT(device_id) DO UPDATE SET state=excluded.state,received_at=excluded.received_at')
      .run(id, canonical(state), Date.now());
    return true;
  }
  reading(id, value) {
    const result = this.db.prepare('INSERT OR IGNORE INTO readings(device_id,reading_id,temperature_c,humidity,recorded_at,received_at) VALUES(?,?,?,?,?,?)')
      .run(id, value.readingId || randomUUID(), value.temperatureC, value.humidity, value.recordedAt || null, Date.now());
    return { duplicate: result.changes === 0 };
  }
  readingHistory(deviceId, hours, now = Date.now()) {
    const from = now - hours * 3600_000;
    // Bound the chart response without discarding older samples in the window.
    // Receipt time is indexed and reliable even when a device clock is unset.
    const bucketMs = Math.ceil(hours * 3600 / 600) * 1000;
    const readings = this.db.prepare(`SELECT CAST(AVG(received_at) AS INTEGER) AS receivedAt,
      AVG(temperature_c) AS temperatureC, AVG(humidity) AS humidity, COUNT(*) AS sampleCount
      FROM readings WHERE device_id=? AND received_at>=? AND received_at<?
      GROUP BY CAST((received_at - ?) / ? AS INTEGER) ORDER BY receivedAt`)
      .all(deviceId, from, now, from, bucketMs);
    return { from, to: now, bucketSeconds: bucketMs / 1000,
      sampleCount: readings.reduce((sum, reading) => sum + reading.sampleCount, 0), readings };
  }
  expire(now = Date.now()) {
    this.db.prepare("UPDATE commands SET status='expired',lease_until=NULL WHERE status IN ('pending','inflight') AND expires_at<=?").run(now);
    this.db.prepare('DELETE FROM sessions WHERE expires_at<=?').run(now);
  }
  enqueue(deviceId, type, payload, ttlSeconds) {
    const now = Date.now(), id = randomUUID();
    this.db.prepare('INSERT INTO commands(id,device_id,type,payload,created_at,expires_at,traceparent) VALUES(?,?,?,?,?,?,?)')
      .run(id, deviceId, type, canonical(payload), now, now + ttlSeconds * 1000, this.observability?.currentTraceparent() || null);
    return this.command(id);
  }
  command(id) { return this.db.prepare('SELECT * FROM commands WHERE id=?').get(id); }
  claim(deviceId, leaseMs, transport = 'poll') {
    const now = Date.now();
    this.expire(now);
    // Preserve device command order until the first command completes or expires.
    const command = this.db.prepare("SELECT * FROM commands WHERE device_id=? AND status IN ('pending','inflight') ORDER BY created_at,rowid LIMIT 1").get(deviceId);
    if (!command || (command.status === 'inflight' && command.lease_until > now)) return null;
    return this.lease(command, leaseMs, now, transport);
  }
  lease(command, leaseMs, now, transport = 'poll') {
    if (!command.traceparent && this.observability) {
      command.traceparent = this.observability.currentTraceparent();
      this.db.prepare('UPDATE commands SET traceparent=? WHERE id=?').run(command.traceparent, command.id);
    }
    this.db.prepare("UPDATE commands SET status='inflight',attempts=attempts+1,lease_until=? WHERE id=?")
      .run(Math.min(now + leaseMs, command.expires_at), command.id);
    if (command.traceparent && this.observability) {
      const attributes = { 'command.id': command.id, 'command.type': command.type, 'command.source': 'manual',
        ...commandPayloadAttributes(command.type, JSON.parse(command.payload)), outcome: 'ok' };
      this.afterCommit(() => {
        if (!command.attempts) {
          this.observability.record('command.queue_wait', command.traceparent, command.created_at, now - command.created_at, attributes);
          this.observability.queueTime.observe({ device: command.device_id }, (now - command.created_at) / 1000);
        }
        this.observability.record('command.delivery', command.traceparent, now, 0,
          { ...attributes, 'delivery.attempt': command.attempts + 1, 'delivery.transport': transport,
            'command.age_ms': now - command.created_at });
      });
    }
    return this.commandValue(command);
  }
  commandValue(command) {
    return { ...(command.traceparent ? { traceparent: command.traceparent } : {}), id: command.id, type: command.type, payload: JSON.parse(command.payload),
      createdAt: new Date(command.created_at).toISOString(), expiresAt: new Date(command.expires_at).toISOString() };
  }
  result(deviceId, id, status, value) {
    this.expire();
    const command = this.command(id);
    if (!command || command.device_id !== deviceId) return { code: 404, error: 'Command not found' };
    const serialized = canonical(value);
    if (command.status === 'succeeded' || command.status === 'failed') {
      return command.status === status && command.result === serialized
        ? { code: 200, duplicate: true } : { code: 409, error: 'Command already has a different final result' };
    }
    if (command.status !== 'inflight') return { code: 409, error: 'Command has not been delivered or has expired' };
    this.db.prepare('UPDATE commands SET status=?,result=?,completed_at=?,lease_until=NULL WHERE id=?').run(status, serialized, Date.now(), id);
    return { code: 200, duplicate: false };
  }
  resultAndClaim(deviceId, id, status, value, leaseMs, allowClaim = true) {
    const execute = () => {
      const result = this.result(deviceId, id, status, value);
      if (result.code !== 200) return result;
      const previous = this.db.prepare('SELECT next_id FROM command_handoffs WHERE previous_id=?').get(id);
      let next = null;
      if (previous?.next_id && allowClaim) {
        // A lost response must recover this same handoff, including after restart.
        const command = this.command(previous.next_id), now = Date.now();
        if (command?.device_id === deviceId && command.status === 'inflight' && command.expires_at > now) {
          next = command.lease_until > now ? this.commandValue(command) : this.lease(command, leaseMs, now, 'result_retry');
        }
      } else if (!previous && !result.duplicate) {
        next = allowClaim ? this.claim(deviceId, leaseMs, 'result') : null;
        // The durable final-result row seals an empty decision: duplicates never
        // claim without a mapping. Only an actual next lease needs another row.
        if (next) this.db.prepare('INSERT INTO command_handoffs(previous_id,next_id) VALUES(?,?)').run(id, next.id);
      }
      return { ...result, resultId: id, commands: next ? [next] : [] };
    };
    // The route already groups presence with this operation. Avoid recording
    // an inner transaction span that did not perform another durable commit.
    return this.commitObservers !== null ? execute() : this.transaction(execute);
  }
  createSession(key, csrf, expiresAt, authVersion) { this.db.prepare('INSERT INTO sessions VALUES(?,?,?,?)').run(digest(key), csrf, expiresAt, authVersion); }
  session(key) { return this.db.prepare('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?').get(digest(key), Date.now()); }
  logout(key) { this.db.prepare('DELETE FROM sessions WHERE token_hash=?').run(digest(key)); }
  snapshot() {
    this.expire();
    const devices = this.db.prepare('SELECT id,last_seen FROM devices ORDER BY id').all().map(device => ({
      id: device.id, lastSeen: (this.presence.get(device.id)?.latest ?? device.last_seen) != null
        ? new Date(this.presence.get(device.id)?.latest ?? device.last_seen).toISOString() : null, fanState: this.fanState(device.id),
      latest: this.db.prepare('SELECT temperature_c AS temperatureC,humidity,recorded_at AS recordedAt,received_at AS receivedAt FROM readings WHERE device_id=? ORDER BY id DESC LIMIT 1').get(device.id) || null
    }));
    const commands = this.db.prepare('SELECT * FROM commands ORDER BY created_at DESC,rowid DESC LIMIT 100').all().map(({ lease_until, ...c }) => ({ ...c, payload: JSON.parse(c.payload), result: c.result === null ? null : JSON.parse(c.result) }));
    return { devices, commands };
  }
}
