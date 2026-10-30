let csrf = '';
const el = selector => document.querySelector(selector);
const node = (tag, text, className) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value; };
const date = value => value ? new Date(value).toLocaleString() : 'Not yet seen';
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'content-type': 'application/json', 'x-csrf-token': csrf, ...options.headers } });
  if (response.status === 401) { location.assign('/login'); throw new Error('Please sign in'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
async function refresh() {
  const state = await api('/api/admin/state'); csrf = state.csrf;
  const selected = el('#device-select').value;
  el('#devices').replaceChildren(...state.devices.map(device => {
    const card = node('article', undefined, 'device');
    const lastSeenMs = device.lastSeen ? Date.parse(device.lastSeen) : 0;
    const recent = Date.now() - lastSeenMs < 90_000;
    card.append(node('h3', device.id), node('p', recent ? 'Recently connected' : 'Offline / not seen recently'));
    if (device.latest) card.append(node('p', `${device.latest.temperatureC} °C · ${device.latest.humidity}% RH`, 'measurement'), node('p', `Reading received: ${date(device.latest.receivedAt)}`));
    else card.append(node('p', 'No sensor readings yet.'));
    card.append(node('p', `Last contact: ${date(device.lastSeen)}`)); return card;
  }));
  el('#device-select').replaceChildren(...state.devices.map(device => { const option = node('option', device.id); option.value = device.id; return option; }));
  if (state.devices.some(device => device.id === selected)) el('#device-select').value = selected;
  el('#commands').replaceChildren(...state.commands.map(command => {
    const row = node('tr');
    const statusCell = node('td'); statusCell.append(node('span', command.status, `badge ${command.status}`));
    const details = node('td'); details.append(node('pre', JSON.stringify({ payload: command.payload, result: command.result }, null, 2)));
    row.append(node('td', date(command.created_at)), node('td', `${command.device_id}\n${command.type}\n${command.id}`), statusCell, node('td', command.attempts), details); return row;
  }));
  if (!state.commands.length) { const row = node('tr'), cell = node('td', 'No commands queued yet.'); cell.colSpan = 5; row.append(cell); el('#commands').append(row); }
  el('#logout').disabled = false; el('#send-command').disabled = !state.devices.length;
}
el('#command-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.target); el('#send-command').disabled = true;
  try {
    const payload = JSON.parse(form.get('payload'));
    const result = await api(`/api/admin/devices/${encodeURIComponent(form.get('deviceId'))}/commands`, { method: 'POST', body: JSON.stringify({ type: form.get('type'), payload, ttlSeconds: Number(form.get('ttlSeconds')) }) });
    el('#message').textContent = `Queued command ${result.id}.`; await refresh();
  } catch (error) { el('#message').textContent = error.message; }
  finally { el('#send-command').disabled = false; }
});
el('#logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST', body: '{}' }); location.assign('/login'); }
  catch (error) { el('#message').textContent = error.message; }
});
async function update() {
  try { await refresh(); if (el('#message').textContent === 'Loading devices…') el('#message').textContent = 'Connected to backend.'; }
  catch (error) { el('#message').textContent = error.message; }
  finally { setTimeout(update, 5000); }
}
update();
