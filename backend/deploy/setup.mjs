import { networkInterfaces } from 'node:os';
import { isIP } from 'node:net';
import https from 'node:https';
import { createInterface } from 'node:readline/promises';
import { mkdirSync, existsSync, writeFileSync, renameSync } from 'node:fs';
import { join } from 'node:path';
import { hashPassword, token } from '../src/auth.mjs';

export function publicIp(value) {
  if (isIP(value) === 4) {
    const [a, b, c] = value.split('.').map(Number);
    return !(a === 0 || a === 10 || a === 127 || a >= 224 || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && (b === 168 || (b === 0 && (c === 0 || c === 2)))) || (a === 100 && b >= 64 && b <= 127) || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113));
  }
  return isIP(value) === 6 && /^[23]/i.test(value) && !value.toLowerCase().startsWith('2001:db8:');
}
export function queryIp(url, family) {
  return new Promise(resolve => {
    const request = https.get(url, { family, timeout: 4000 }, response => {
      if (response.statusCode !== 200) { response.resume(); resolve(null); return; }
      let text = '';
      response.on('data', chunk => { text += chunk; if (text.length > 128) response.destroy(); });
      response.on('end', () => resolve(publicIp(text.trim()) ? text.trim() : null));
      response.on('error', () => resolve(null));
    });
    request.on('timeout', () => request.destroy());
    request.on('error', () => resolve(null));
  });
}
export async function detectIps() {
  const found = Object.values(networkInterfaces()).flat().filter(v => v && !v.internal && publicIp(v.address)).map(v => v.address);
  const external = await Promise.all([
    queryIp('https://checkip.amazonaws.com/', 4), queryIp('https://api.ipify.org/', 4), queryIp('https://api6.ipify.org/', 6)
  ]);
  return [...new Set([...found, ...external].filter(Boolean))];
}
async function secret(label) {
  if (!process.stdin.isTTY || !process.stdin.setRawMode) throw new Error('Interactive setup needs a terminal. Use the documented environment variables for unattended setup.');
  process.stdout.write(label);
  return new Promise((resolve, reject) => {
    let value = '';
    process.stdin.setRawMode(true); process.stdin.resume(); process.stdin.setEncoding('utf8');
    const finish = () => { process.stdin.off('data', receive); process.stdin.setRawMode(false); process.stdin.pause(); process.stdout.write('\n'); };
    const receive = chunk => {
      for (const char of chunk) {
        if (char === '\u0003') { finish(); reject(new Error('Setup cancelled')); return; }
        if (char === '\r' || char === '\n') { finish(); resolve(value); return; }
        if (char === '\u007f' || char === '\b') value = value.slice(0, -1);
        else if (char >= ' ') value += char;
      }
    };
    process.stdin.on('data', receive);
  });
}
async function main() {
  const directory = process.env.SETUP_DIR || '/setup';
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const file = join(directory, 'server.env');
  let rl;
  const ask = async (label, fallback = '') => {
    if (!process.stdin.isTTY) throw new Error('Missing configuration: run interactive ./deploy.sh or supply the setup environment variables.');
    rl ??= createInterface({ input: process.stdin, output: process.stdout });
    const answer = (await rl.question(label + (fallback ? ` [${fallback}]` : '') + ': ')).trim();
    return answer || fallback;
  };
  try {
    console.log('Sensor backend setup — direct HTTPS, no reverse proxy.');
    let ip = process.env.VPS_IP;
    if (!ip) {
      console.log('Detecting public IP addresses...');
      const candidates = await detectIps();
      candidates.forEach((value, i) => console.log(`  ${i + 1}. ${value}`));
      const choice = await ask('Select a number or enter the public IP to use', candidates.length === 1 ? '1' : '');
      ip = candidates[Number(choice) - 1] || choice;
    }
    if (!publicIp(ip)) throw new Error('A routable public IPv4 or IPv6 address is required for a public IP certificate.');
    const email = process.env.ACME_EMAIL || await ask('Email for the certificate account');
    if (!/^[a-zA-Z0-9._+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/.test(email)) throw new Error('Enter a valid certificate email address.');
    const port = process.env.HTTPS_PORT || await ask('Public HTTPS port', '443');
    if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535 || Number(port) === 80) throw new Error('Choose a valid HTTPS port other than port 80.');
    console.log(`The public address ${ip} must route TCP port 80 and TCP port ${port} to this Docker host.`);
    console.log('Port 80 is used only by Certbot for certificate ownership validation. Behind NAT, configure port forwarding first.');
    if (process.env.ACME_AGREE_TOS !== 'true') {
      const answer = await ask('Accept https://letsencrypt.org/repository/ terms and continue? (yes/no)', 'no');
      if (answer.toLowerCase() !== 'yes') throw new Error('Certificate terms were not accepted.');
    }
    const username = process.env.ADMIN_USERNAME || await ask('Web admin username', 'admin');
    if (!/^[a-zA-Z0-9_.-]{1,64}$/.test(username)) throw new Error('Admin username must contain only letters, numbers, dot, underscore, or hyphen.');
    const deviceId = process.env.DEVICE_ID || await ask('Device ID', 'seeeduino-cloud');
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(deviceId)) throw new Error('Invalid device ID.');
    rl?.close(); rl = null;
    let password = process.env.ADMIN_PASSWORD;
    if (!password) {
      password = await secret('Web admin password (hidden, at least 12 characters): ');
      if (password !== await secret('Confirm password: ')) throw new Error('Passwords do not match.');
    }
    if (password.length < 12 || password.length > 1024) throw new Error('Password must have 12–1024 characters.');
    let oldToken;
    if (existsSync(file)) {
      // Keep the device token when changing the IP or admin credentials.
      const { readFileSync } = await import('node:fs');
      oldToken = readFileSync(file, 'utf8').match(/^DEVICE_TOKEN=(.+)$/m)?.[1];
    }
    const values = {
      VPS_IP: ip, ACME_EMAIL: email, HTTPS_PORT: String(Number(port)), ACME_AGREE_TOS: 'true',
      ADMIN_USERNAME: username, ADMIN_PASSWORD_HASH: await hashPassword(password),
      DEVICE_ID: deviceId, DEVICE_TOKEN: process.env.DEVICE_TOKEN || oldToken || token()
    };
    if (values.DEVICE_TOKEN.length < 32 || !/^[a-zA-Z0-9_-]+$/.test(values.DEVICE_TOKEN)) throw new Error('DEVICE_TOKEN must have at least 32 URL-safe characters.');
    const temporary = file + '.tmp';
    writeFileSync(temporary, Object.entries(values).map(([k, v]) => `${k}=${v}`).join('\n') + '\n', { mode: 0o600, flag: 'w' });
    const { chmodSync } = await import('node:fs'); chmodSync(temporary, 0o600);
    renameSync(temporary, file);
    const address = isIP(ip) === 6 ? `[${ip}]` : ip;
    console.log(`Saved private configuration to ${file}.`);
    console.log(`Dashboard: https://${address}${Number(port) === 443 ? '' : ':' + Number(port)}/`);
    console.log('The device token is in the private configuration file; it has not been printed.');
  } finally { rl?.close(); }
}
if (process.argv[1] && new URL(import.meta.url).pathname === process.argv[1]) {
  main().catch(error => { console.error('Setup failed:', error.message); process.exitCode = 1; });
}
