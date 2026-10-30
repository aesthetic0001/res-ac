import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { randomUUID } from 'node:crypto';
import { digest } from './auth.mjs';
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export class Store {
  constructor(path, devices = []) {
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, last_seen INTEGER);
      CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id), reading_id TEXT NOT NULL,
        temperature_c REAL NOT NULL, humidity REAL NOT NULL, recorded_at TEXT, received_at INTEGER NOT NULL, UNIQUE(device_id,reading_id));
      CREATE INDEX IF NOT EXISTS reading_device_time ON readings(device_id,received_at DESC);
      CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES devices(id),type TEXT NOT NULL,payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,lease_until INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,result TEXT,completed_at INTEGER);
      CREATE INDEX IF NOT EXISTS command_device_time ON commands(device_id,created_at);
      CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires_at INTEGER NOT NULL,auth_version TEXT NOT NULL);`);
    for (const device of devices) this.db.prepare('INSERT INTO devices(id,token_hash) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash').run(device.id, digest(device.token));
  }
  close() { this.db.close(); }
  device(id) { return this.db.prepare('SELECT * FROM devices WHERE id=?').get(id); }
  seen(id) { this.db.prepare('UPDATE devices SET last_seen=? WHERE id=?').run(Date.now(), id); }
  reading(id, value) {
    const result = this.db.prepare('INSERT OR IGNORE INTO readings(device_id,reading_id,temperature_c,humidity,recorded_at,received_at) VALUES(?,?,?,?,?,?)')
      .run(id, value.readingId || randomUUID(), value.temperatureC, value.humidity, value.recordedAt || null, Date.now());
    return { duplicate: result.changes === 0 };
  }
  expire(now = Date.now()) {
    this.db.prepare("UPDATE commands SET status='expired',lease_until=NULL WHERE status IN ('pending','inflight') AND expires_at<=?").run(now);
    this.db.prepare('DELETE FROM sessions WHERE expires_at<=?').run(now);
  }
  enqueue(deviceId, type, payload, ttlSeconds) {
    const now = Date.now(), id = randomUUID();
    this.db.prepare('INSERT INTO commands(id,device_id,type,payload,created_at,expires_at) VALUES(?,?,?,?,?,?)')
      .run(id, deviceId, type, canonical(payload), now, now + ttlSeconds * 1000);
    return this.command(id);
  }
  command(id) { return this.db.prepare('SELECT * FROM commands WHERE id=?').get(id); }
  claim(deviceId, leaseMs) {
    const now = Date.now();
    this.expire(now);
    // Preserve device command order until the first command completes or expires.
    const command = this.db.prepare("SELECT * FROM commands WHERE device_id=? AND status IN ('pending','inflight') ORDER BY created_at,rowid LIMIT 1").get(deviceId);
    if (!command || (command.status === 'inflight' && command.lease_until > now)) return null;
    this.db.prepare("UPDATE commands SET status='inflight',attempts=attempts+1,lease_until=? WHERE id=?")
      .run(Math.min(now + leaseMs, command.expires_at), command.id);
    return { id: command.id, type: command.type, payload: JSON.parse(command.payload),
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
  createSession(key, csrf, expiresAt, authVersion) { this.db.prepare('INSERT INTO sessions VALUES(?,?,?,?)').run(digest(key), csrf, expiresAt, authVersion); }
  session(key) { return this.db.prepare('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?').get(digest(key), Date.now()); }
  logout(key) { this.db.prepare('DELETE FROM sessions WHERE token_hash=?').run(digest(key)); }
  snapshot() {
    this.expire();
    const devices = this.db.prepare('SELECT id,last_seen FROM devices ORDER BY id').all().map(device => ({
      id: device.id, lastSeen: device.last_seen ? new Date(device.last_seen).toISOString() : null,
      latest: this.db.prepare('SELECT temperature_c AS temperatureC,humidity,recorded_at AS recordedAt,received_at AS receivedAt FROM readings WHERE device_id=? ORDER BY id DESC LIMIT 1').get(device.id) || null
    }));
    const commands = this.db.prepare('SELECT * FROM commands ORDER BY created_at DESC,rowid DESC LIMIT 100').all().map(({ lease_until, ...c }) => ({ ...c, payload: JSON.parse(c.payload), result: c.result === null ? null : JSON.parse(c.result) }));
    return { devices, commands };
  }
}
