import http from 'node:http';

const hopHeaders = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'];
function cleanHeaders(headers) {
  const result = { ...headers };
  for (const name of [...hopHeaders, ...(headers.connection || '').split(',').map(value => value.trim().toLowerCase())]) delete result[name];
  return result;
}
export function createGrafanaProxy(target, { protocol = 'https' } = {}) {
  const upstream = new URL(target);
  if (upstream.protocol !== 'http:' || upstream.username || upstream.password || upstream.pathname !== '/' || upstream.search || upstream.hash)
    throw new Error('GRAFANA_PROXY_URL must be an internal HTTP origin');
  const sockets = new Set();
  const matches = req => (req.url || '').split('?')[0].startsWith('/grafana/');
  const headers = req => {
    const result = cleanHeaders(req.headers);
    for (const name of Object.keys(result)) if (name.startsWith('x-forwarded-') || name.startsWith('x-auth-request-')) delete result[name];
    // Keep the sensor dashboard session scoped to its own backend.
    if (result.cookie) result.cookie = result.cookie.split(';').filter(value => value.trim().split('=')[0] !== 'session').join(';');
    result['x-forwarded-proto'] = protocol;
    result['x-forwarded-host'] = req.headers.host;
    result['x-forwarded-for'] = req.socket.remoteAddress;
    result['x-real-ip'] = req.socket.remoteAddress;
    return result;
  };
  const request = (req, extraHeaders = {}) => http.request({ hostname: upstream.hostname, port: upstream.port || 80,
    method: req.method, path: req.url, headers: { ...headers(req), ...extraHeaders } });
  const unavailable = res => {
    if (res.destroyed || res.writableEnded) return;
    if (res.headersSent) return res.destroy();
    res.writeHead(502, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
    res.end('Grafana is temporarily unavailable.');
  };
  return {
    handle(req, res) {
      const [path, query] = (req.url || '').split('?');
      if (path === '/grafana') { res.writeHead(308, { location: '/grafana/' + (query ? '?' + query : '') }); res.end(); return true; }
      if (!matches(req)) return false;
      const proxy = request(req);
      proxy.setTimeout(30_000, () => proxy.destroy());
      proxy.on('error', () => unavailable(res));
      proxy.on('response', response => {
        res.writeHead(response.statusCode, cleanHeaders(response.headers));
        response.on('error', () => res.destroy()); response.pipe(res);
      });
      req.once('aborted', () => proxy.destroy());
      res.once('close', () => proxy.destroy());
      req.pipe(proxy);
      return true;
    },
    upgrade(req, socket, head) {
      if (!matches(req) || req.headers.upgrade?.toLowerCase() !== 'websocket') { socket.destroy(); return; }
      const proxy = request(req, { connection: 'Upgrade', upgrade: 'websocket' });
      proxy.setTimeout(10_000, () => proxy.destroy());
      const track = value => { sockets.add(value); value.once('close', () => sockets.delete(value)); };
      track(socket);
      socket.on('error', () => proxy.destroy()); socket.once('close', () => proxy.destroy());
      proxy.on('error', () => { if (!socket.destroyed) socket.end('HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n'); });
      proxy.on('response', response => {
        socket.write(`HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\nConnection: close\r\n\r\n`);
        response.pipe(socket);
      });
      proxy.on('upgrade', (response, remote, remoteHead) => {
        remote.setTimeout(0); track(remote);
        remote.on('error', () => socket.destroy()); socket.on('error', () => remote.destroy());
        remote.once('close', () => socket.destroy()); socket.once('close', () => remote.destroy());
        socket.write(`HTTP/1.1 ${response.statusCode} ${response.statusMessage}\r\n`);
        for (let i = 0; i < response.rawHeaders.length; i += 2) socket.write(`${response.rawHeaders[i]}: ${response.rawHeaders[i + 1]}\r\n`);
        socket.write('\r\n');
        if (remoteHead.length) socket.write(remoteHead);
        if (head.length) remote.write(head);
        remote.pipe(socket); socket.pipe(remote);
      });
      proxy.end();
    },
    close() { for (const socket of sockets) socket.destroy(); }
  };
}
