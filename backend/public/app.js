import { renderHistory } from './graphs.js';

let csrf = '';
let dashboardState = null;
let remoteSending = false;
let remoteError = '';
let remotePollUntil = 0;
let backendAvailable = false;
let refreshSequence = 0;
let updateError = '';
let calibrationUiKey = '';
const fanActions = { power: 'Power toggle', speed_down: 'Speed down', speed_up: 'Speed up' };
let historyRequest = null, plottedHistory = null;
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
  const sequence = ++refreshSequence;
  const state = await api('/api/admin/state');
  if (sequence !== refreshSequence) return;
  csrf = state.csrf; dashboardState = state; backendAvailable = true;
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
  const historySelected = el('#history-device').value;
  el('#history-device').replaceChildren(...state.devices.map(device => { const option = node('option', device.id); option.value = device.id; return option; }));
  if (state.devices.some(device => device.id === historySelected)) el('#history-device').value = historySelected;
  el('#history-device').disabled = !state.devices.length;
  el('#commands').replaceChildren(...state.commands.map(command => {
    const row = node('tr');
    const statusCell = node('td'); statusCell.append(node('span', command.status, `badge ${command.status}`));
    const details = node('td'); details.append(node('pre', JSON.stringify({ payload: command.payload, result: command.result }, null, 2)));
    row.append(node('td', date(command.created_at)), node('td', `${command.device_id}\n${command.type}\n${command.id}`), statusCell, node('td', command.attempts), details); return row;
  }));
  if (!state.commands.length) { const row = node('tr'), cell = node('td', 'No commands queued yet.'); cell.colSpan = 5; row.append(cell); el('#commands').append(row); }
  el('#logout').disabled = false; el('#send-command').disabled = !state.devices.length;
  const remoteSelect = el('#remote-device-select');
  const remoteSelected = remoteSelect.value;
  if (JSON.stringify([...remoteSelect.options].map(option => option.value)) !== JSON.stringify(state.devices.map(device => device.id))) {
    remoteSelect.replaceChildren(...state.devices.map(device => { const option = node('option', device.id); option.value = device.id; return option; }));
    if (state.devices.some(device => device.id === remoteSelected)) remoteSelect.value = remoteSelected;
  }
  renderRemote();
  await refreshHistory();
}

function renderRemote() {
  const device = dashboardState?.devices.find(device => device.id === el('#remote-device-select').value);
  const commands = dashboardState?.commands.filter(command => command.device_id === device?.id && ['fan.command', 'fan.calibrate', 'ir.send'].includes(command.type)) || [];
  const pending = commands.some(command => ['pending', 'inflight'].includes(command.status));
  const fan = device?.fanState;
  const fresh = !!fan && Date.now() - fan.receivedAt < 30_000;
  const calibrated = fresh && fan.calibrated;
  const key = `${device?.id}:${fan?.sessionId}:${calibrated}`;
  if (key !== calibrationUiKey) {
    calibrationUiKey = key; el('#confirm-fan-off').checked = false;
    el('#fan-calibration').open = !calibrated;
  }
  el('#confirm-fan-off').disabled = !backendAvailable || !fresh || !fan?.sessionId || remoteSending || pending;
  el('#calibrate-fan').disabled = el('#confirm-fan-off').disabled || !el('#confirm-fan-off').checked;
  el('#fan-estimate').textContent = !fresh ? 'Estimated state: unknown (waiting for device)' : !calibrated
    ? 'Calibration required' : fan.power === 'on' ? `Estimated: On · Speed ${fan.speed} / 5` : 'Estimated: Off';
  el('#remote-device-select').disabled = !backendAvailable || remoteSending || !dashboardState?.devices.length;
  for (const button of document.querySelectorAll('[data-fan-button]')) {
    button.disabled = !backendAvailable || !calibrated || remoteSending || pending || (button.dataset.fanButton !== 'power' && fan.power !== 'on');
  }
  el('#remote-connection').textContent = !device ? 'No devices available.' : Date.now() - Date.parse(device.lastSeen || '') < 90_000
    ? 'Device recently connected.' : 'Device has not checked in recently. Requests expire after 30 seconds.';
  const status = el('#remote-status');
  status.className = '';
  if (!backendAvailable) { status.textContent = dashboardState ? 'Cannot refresh remote status. Reconnecting…' : 'Cannot load remote. Reconnecting…'; return; }
  if (remoteSending) { status.textContent = 'Queuing button press…'; return; }
  if (remoteError) { status.textContent = remoteError; status.className = 'remote-error'; return; }
  if (!device) { status.textContent = 'Add a device to use the remote.'; return; }
  const command = commands[0];
  if (!calibrated && (!command || command.type !== 'fan.calibrate' || command.payload.sessionId !== fan?.sessionId)) {
    status.textContent = fresh ? 'Remote locked. Restart the fan to factory position: off, then confirm calibration.' : 'Remote locked while waiting for current device state.';
    return;
  }
  if (!command) { status.textContent = calibrated ? 'Ready. Aim the IR emitter at the fan.' : 'Remote locked until the fan is calibrated.'; return; }
  const action = command.type === 'fan.calibrate' ? 'Calibration' : fanActions[command.payload.button] || 'IR command';
  if (command.status === 'succeeded') {
    status.textContent = command.result?.reply === 'calibrated' ? 'Calibration confirmed by device.' : command.result?.reply === 'ir_sent' ? `${action}: IR sent. Fan response unconfirmed.` : `${action}: command completed. Fan response unconfirmed.`;
  } else if (command.status === 'failed') {
    status.textContent = `${action} failed: ${command.result?.error || 'Check Recent commands for details.'}`;
    status.className = 'remote-error';
  } else if (command.status === 'expired') {
    status.textContent = `${action} expired. Check the fan before sending another press.`;
    status.className = 'remote-error';
  } else {
    status.textContent = `${action}: ${command.status === 'inflight' ? 'delivered; waiting for transmission' : 'queued; waiting for device'}…`;
  }
}

el('#remote-device-select').addEventListener('change', () => { remoteError = ''; renderRemote(); });
el('#confirm-fan-off').addEventListener('change', renderRemote);
async function queueFanCommand(type, payload) {
  const deviceId = el('#remote-device-select').value;
  const device = dashboardState.devices.find(device => device.id === deviceId);
  payload = { ...payload, sessionId: device.fanState.sessionId };
  remoteSending = true; remoteError = ''; renderRemote();
  try {
    const queued = await api(`/api/admin/devices/${encodeURIComponent(deviceId)}/commands`, {
      method: 'POST', body: JSON.stringify({ type, payload, ttlSeconds: 30 })
    });
    if (!dashboardState.commands.some(command => command.id === queued.id)) dashboardState.commands.unshift({
      id: queued.id, device_id: deviceId, type, payload, status: queued.status, result: null
    });
    remotePollUntil = Date.now() + 35_000;
    await refresh();
  } catch (error) {
    remoteError = `${error.message}. Check Recent commands before retrying.`;
  } finally {
    remoteSending = false; renderRemote();
  }
}
el('#calibration-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (el('#calibrate-fan').disabled || remoteSending) return;
  await queueFanCommand('fan.calibrate', { confirmOff: true });
});
for (const button of document.querySelectorAll('[data-fan-button]')) {
  button.addEventListener('click', async () => {
    if (button.disabled || remoteSending) return;
    await queueFanCommand('fan.command', { button: button.dataset.fanButton });
  });
}
async function refreshHistory(clear = false) {
  historyRequest?.abort();
  const request = new AbortController(); historyRequest = request;
  const device = el('#history-device').value;
  if (clear || !device) {
    plottedHistory = null; el('#history-charts').hidden = true;
    el('#history-message').textContent = device ? 'Loading reading history…' : 'No devices configured yet.';
  }
  if (!device) return;
  try {
    const history = await api(`/api/admin/devices/${encodeURIComponent(device)}/readings?hours=${el('#history-range').value}`, { signal: request.signal });
    if (request.signal.aborted) return;
    plottedHistory = history.readings.length ? history : null;
    el('#history-charts').hidden = !plottedHistory;
    el('#history-message').textContent = plottedHistory
      ? `${history.sampleCount.toLocaleString()} readings · plotted in ${history.bucketSeconds < 60 ? `${history.bucketSeconds}-second` : `${Number((history.bucketSeconds / 60).toFixed(1))}-minute`} intervals. Updated ${new Date().toLocaleTimeString()}.`
      : 'No readings in this time range. New readings will appear automatically.';
    if (plottedHistory) renderHistory(plottedHistory);
  } catch (error) {
    if (request.signal.aborted) return;
    plottedHistory = null; el('#history-charts').hidden = true;
    el('#history-message').textContent = `Could not load reading history: ${error.message}. Retrying automatically.`;
  }
}
el('#history-device').addEventListener('change', () => refreshHistory(true));
el('#history-range').addEventListener('change', () => refreshHistory(true));
let resizeTimer;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (plottedHistory) renderHistory(plottedHistory); }, 150);
});
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
  try {
    await refresh();
    if (el('#message').textContent === 'Loading devices…' || (updateError && el('#message').textContent === updateError)) el('#message').textContent = 'Connected to backend.';
    updateError = '';
  }
  catch (error) { backendAvailable = false; renderRemote(); updateError = error.message; el('#message').textContent = updateError; }
  finally { setTimeout(update, Date.now() < remotePollUntil ? 1000 : 5000); }
}
update();
