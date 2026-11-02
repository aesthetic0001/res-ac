import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createGrafanaProxy } from '../src/grafana-proxy.mjs';
async function fixture(t, handler) {
  const sockets = new Set();
  const track = socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); };
  const upstream = http.createServer(handler);
  upstream.on('connection', track);
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const proxy = createGrafanaProxy(`http://127.0.0.1:${upstream.address().port}`);
  const server = http.createServer((req, res) => { if (!proxy.handle(req, res)) { res.writeHead(404); res.end(); } });
  server.on('connection', track);
  server.on('upgrade', (req, socket, head) => proxy.upgrade(req, socket, head));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { proxy.close(); for (const socket of sockets) socket.destroy(); server.closeAllConnections(); upstream.closeAllConnections(); await Promise.all([new Promise(resolve => server.close(resolve)),new Promise(resolve => upstream.close(resolve))]); });
  return { upstream, server, base: `http://127.0.0.1:${server.address().port}` };
}
test('proxies Grafana paths, bodies, authentication, cookies and trusted forwarding headers', async t => {
  const f = await fixture(t, async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    res.setHeader('set-cookie', 'grafana_session=example; Path=/grafana; Secure; HttpOnly; SameSite=Strict');
    res.end(JSON.stringify({ path: req.url, method: req.method, headers: req.headers, body }));
  });
  const response = await fetch(f.base + '/grafana/login?next=dashboard', { method: 'POST', headers: {
    'content-type': 'application/json', authorization: 'Basic test', cookie: 'session=backend-private; grafana_session=grafana-private',
    'x-forwarded-proto': 'http', 'x-forwarded-user': 'admin', 'x-auth-request-user': 'admin', 'x-real-ip': 'spoofed'
  }, body: '{"user":"test"}' });
  const value = await response.json();
  assert.equal(value.path, '/grafana/login?next=dashboard'); assert.equal(value.method, 'POST');
  assert.equal(value.body, '{"user":"test"}'); assert.equal(value.headers.authorization, 'Basic test');
  assert.equal(value.headers['x-forwarded-proto'], 'https'); assert.equal(value.headers['x-real-ip'], '127.0.0.1');
  assert.equal(value.headers['x-forwarded-user'], undefined); assert.equal(value.headers['x-auth-request-user'], undefined);
  assert.ok(!value.headers.cookie.includes('backend-private')); assert.ok(value.headers.cookie.includes('grafana-private'));
  assert.match(response.headers.get('set-cookie'), /Secure; HttpOnly; SameSite=Strict/);
  assert.equal((await fetch(f.base + '/grafanaevil')).status, 404);
  const redirect = await fetch(f.base + '/grafana?orgId=1', { redirect: 'manual' });
  assert.equal(redirect.status, 308); assert.equal(redirect.headers.get('location'), '/grafana/?orgId=1');
});
test('returns a bounded 502 when Grafana is unavailable', async t => {
  const f = await fixture(t, (req, res) => res.end());
  await new Promise(resolve => f.upstream.close(resolve));
  const response = await fetch(f.base + '/grafana/api/health');
  assert.equal(response.status, 502); assert.equal(await response.text(), 'Grafana is temporarily unavailable.');
});
test('forwards upgrade traffic for Grafana Live and closes both sockets', async t => {
  const f = await fixture(t, (req, res) => res.end());
  f.upstream.on('upgrade', (req, socket) => {
    assert.equal(req.url, '/grafana/api/live/ws');
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n');
    socket.on('data', data => socket.write(data));
  });
  await new Promise((resolve, reject) => {
    const request = http.request(f.base + '/grafana/api/live/ws', { headers: { connection: 'Upgrade', upgrade: 'websocket' } });
    request.on('error', reject);
    request.on('upgrade', (res, socket) => {
      socket.on('error', reject);
      socket.once('data', data => { assert.equal(data.toString(), 'round-trip'); socket.destroy(); resolve(); });
      socket.write('round-trip');
    });
    request.end();
  });
});
test('rejects non-HTTP origins and credentials in proxy configuration', () => {
  for (const target of ['https://grafana:3000', 'http://user:pass@grafana:3000', 'http://grafana:3000/path'])
    assert.throws(() => createGrafanaProxy(target));
});
