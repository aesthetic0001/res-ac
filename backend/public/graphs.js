const svgNS = 'http://www.w3.org/2000/svg';
const element = (tag, attributes = {}, text) => {
  const node = document.createElementNS(svgNS, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
};
const number = value => Number(value.toFixed(1)).toLocaleString();

function chart(container, history, key, label, unit) {
  const { readings, from, to, bucketSeconds } = history;
  const width = Math.max(300, container.clientWidth), height = 250;
  const left = 48, right = width - 16, top = 16, bottom = height - 42;
  const values = readings.map(reading => reading[key]);
  let low = key === 'humidity' ? 0 : Math.min(...values);
  let high = key === 'humidity' ? 100 : Math.max(...values);
  if (key !== 'humidity') {
    const padding = Math.max((high - low) * 0.15, 1);
    low -= padding; high += padding;
  }
  const x = timestamp => left + (timestamp - from) / (to - from) * (right - left);
  const y = value => bottom - (value - low) / (high - low) * (bottom - top);
  const svg = element('svg', { viewBox: `0 0 ${width} ${height}`, tabindex: '0', role: 'img',
    'aria-label': `${label} over time. Use left and right arrow keys to inspect values.` });
  svg.append(element('title', {}, `${label} over time`));
  for (let i = 0; i <= 4; i++) {
    const value = low + (high - low) * i / 4;
    svg.append(element('line', { x1: left, x2: right, y1: y(value), y2: y(value), class: 'chart-grid' }),
      element('text', { x: left - 8, y: y(value) + 4, 'text-anchor': 'end' }, number(value)));
  }
  const ticks = width < 450 ? 2 : 4;
  for (let i = 0; i <= ticks; i++) {
    const timestamp = from + (to - from) * i / ticks;
    const time = new Date(timestamp).toLocaleString(undefined, {
      ...(to - from > 86400_000 ? { month: 'short', day: 'numeric' } : {}), hour: '2-digit', minute: '2-digit'
    });
    svg.append(element('text', { x: x(timestamp), y: bottom + 26,
      'text-anchor': i === 0 ? 'start' : i === ticks ? 'end' : 'middle' }, time));
  }
  const maxGap = Math.max(bucketSeconds * 3000, 60_000);
  let path = '';
  readings.forEach((reading, index) => {
    const disconnected = !index || reading.receivedAt - readings[index - 1].receivedAt > maxGap;
    path += `${disconnected ? 'M' : 'L'}${x(reading.receivedAt).toFixed(2)},${y(reading[key]).toFixed(2)} `;
  });
  svg.append(element('path', { d: path, class: 'chart-line' }));
  for (const reading of readings) {
    svg.append(element('circle', { cx: x(reading.receivedAt), cy: y(reading[key]), r: 2, class: 'chart-dot' }));
  }
  const marker = element('circle', { r: 5, class: 'chart-marker' });
  svg.append(marker);
  const detail = document.createElement('p'); detail.className = 'chart-detail';
  detail.setAttribute('aria-live', 'polite');
  let selected = readings.length - 1;
  const inspect = index => {
    selected = Math.max(0, Math.min(index, readings.length - 1));
    const reading = readings[selected];
    marker.setAttribute('cx', x(reading.receivedAt)); marker.setAttribute('cy', y(reading[key]));
    detail.textContent = `${new Date(reading.receivedAt).toLocaleString()} · ${number(reading[key])}${unit}` +
      (reading.sampleCount > 1 ? ` · average of ${reading.sampleCount} readings` : '');
  };
  svg.addEventListener('pointermove', event => {
    const bounds = svg.getBoundingClientRect();
    const timestamp = from + ((event.clientX - bounds.left) * width / bounds.width - left) / (right - left) * (to - from);
    let nearest = 0;
    readings.forEach((reading, index) => {
      if (Math.abs(reading.receivedAt - timestamp) < Math.abs(readings[nearest].receivedAt - timestamp)) nearest = index;
    });
    inspect(nearest);
  });
  svg.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    inspect(event.key === 'Home' ? 0 : event.key === 'End' ? readings.length - 1 : selected + (event.key === 'ArrowLeft' ? -1 : 1));
  });
  container.replaceChildren(svg, detail);
  inspect(selected);
}

export function renderHistory(history) {
  chart(document.querySelector('#temperature-chart'), history, 'temperatureC', 'Temperature', ' °C');
  chart(document.querySelector('#humidity-chart'), history, 'humidity', 'Relative humidity', '% RH');
}
