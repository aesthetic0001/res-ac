export const presets = { quiet: [24, 26, 28, 30, 32], balanced: [22, 24, 26, 28, 30], cooling: [20, 22, 24, 26, 28] };

export function validThresholds(values) {
  return Array.isArray(values) && values.length === 5 && values.every((value, index) =>
    Number.isFinite(value) && value >= -20 && value <= 60 && (!index || value >= values[index - 1]));
}

// Carry neighbors along rather than trapping a handle between equal thresholds.
export function moveThreshold(values, index, temperature) {
  const value = Math.max(-20, Math.min(60, Math.round(temperature * 2) / 2));
  return values.map((previous, position) => position < index ? Math.min(previous, value)
    : position > index ? Math.max(previous, value) : value);
}

const editors = new WeakMap();
const svgNS = 'http://www.w3.org/2000/svg';
function element(name, attrs = {}, text) {
  const value = document.createElementNS(svgNS, name);
  for (const [key, attribute] of Object.entries(attrs)) value.setAttribute(key, attribute);
  if (text !== undefined) value.textContent = text;
  return value;
}

function createEditor(container) {
  const svg = element('svg', { role: 'group', 'aria-label': 'Editable temperature curve', 'aria-describedby': 'curve-help', class: 'curve-editor' });
  const grid = element('g', { 'aria-hidden': 'true' });
  const area = element('path', { class: 'curve-area', 'aria-hidden': 'true' });
  const steps = element('path', { class: 'curve-step-line', 'aria-hidden': 'true' });
  const guide = element('path', { class: 'curve-control-line', 'aria-hidden': 'true' });
  const selection = element('line', { class: 'curve-selection-line', 'aria-hidden': 'true' });
  const pointGroup = element('g');
  const readout = document.createElement('p');
  readout.className = 'curve-readout'; readout.setAttribute('role', 'status'); readout.setAttribute('aria-live', 'polite');
  const state = { container, svg, grid, area, steps, guide, selection, readout, points: [], values: [], selected: 0, drag: null, disabled: false, onChange: null };

  const emit = values => {
    if (values.every((value, index) => value === state.values[index])) return;
    state.values = values;
    draw(state);
    state.onChange?.([...values]);
  };
  const finish = event => {
    if (!state.drag || state.drag.pointerId !== event.pointerId) return;
    const { startValues, index } = state.drag;
    state.drag = null;
    if (event.type === 'pointercancel') emit(startValues);
    if (state.points[index].hasPointerCapture(event.pointerId)) state.points[index].releasePointerCapture(event.pointerId);
    draw(state);
  };
  for (let index = 0; index < 5; index++) {
    const point = element('g', { tabindex: '0', role: 'slider', 'aria-label': `Level ${index + 1} temperature`,
      'aria-orientation': 'horizontal', 'aria-valuemin': '-20', 'aria-valuemax': '60', 'data-curve-point': String(index) });
    point.append(element('circle', { r: 22, class: 'curve-hit', 'aria-hidden': 'true' }),
      element('circle', { r: 12, class: 'curve-halo', 'aria-hidden': 'true' }),
      element('circle', { r: 8, class: 'curve-node', 'aria-hidden': 'true' }),
      element('text', { y: 4, 'text-anchor': 'middle', class: 'curve-point-label', 'aria-hidden': 'true' }, String(index + 1)));
    point.addEventListener('focus', () => { state.selected = index; draw(state); });
    point.addEventListener('pointerdown', event => {
      if (state.disabled || state.drag || (event.pointerType === 'mouse' && event.button !== 0)) return;
      event.preventDefault();
      point.focus({ preventScroll: true });
      state.selected = index;
      state.drag = { pointerId: event.pointerId, index, startValues: [...state.values], low: state.low, high: state.high };
      point.setPointerCapture(event.pointerId);
      draw(state);
    });
    point.addEventListener('pointermove', event => {
      if (!state.drag || state.drag.pointerId !== event.pointerId) return;
      const matrix = svg.getScreenCTM();
      if (!matrix) return;
      const position = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
      const temperature = state.low + (position.x - state.left) / (state.right - state.left) * (state.high - state.low);
      emit(moveThreshold(state.drag.startValues, state.drag.index, Math.max(state.low, Math.min(state.high, temperature))));
    });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) point.addEventListener(name, finish);
    point.addEventListener('keydown', event => {
      if (state.disabled || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      state.selected = index;
      const increment = event.shiftKey ? 5 : 0.5;
      const value = event.key === 'Home' ? -20 : event.key === 'End' ? 60
        : state.values[index] + (event.key === 'ArrowRight' ? increment : -increment);
      emit(moveThreshold(state.values, index, value));
    });
    state.points.push(point); pointGroup.append(point);
  }
  svg.append(element('title', {}, 'Drag a fan-level handle left or right to set its temperature.'), grid, area, steps, guide, selection, pointGroup);
  container.replaceChildren(svg, readout);
  // Update in place so polling and number-field edits preserve pointer capture.
  state.observer = new ResizeObserver(() => {
    if (state.values.length && Math.max(280, container.clientWidth) !== state.width) draw(state);
  });
  state.observer.observe(container);
  editors.set(container, state);
  return state;
}

function draw(state) {
  const { values, svg, grid } = state;
  if (!values.length) return;
  const width = Math.max(280, state.container.clientWidth), height = 340;
  const left = 48, right = width - 24, top = 32, bottom = 278;
  const low = state.drag?.low ?? Math.max(-20, Math.min(15, Math.floor((values[0] - 5) / 5) * 5));
  const high = state.drag?.high ?? Math.min(60, Math.max(35, Math.ceil((values[4] + 5) / 5) * 5));
  Object.assign(state, { low, high, left, right, width });
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.classList.toggle('is-dragging', !!state.drag);
  svg.classList.toggle('is-disabled', state.disabled);
  const x = temperature => left + (temperature - low) / (high - low) * (right - left);
  const y = level => bottom - level * (bottom - top) / 5;
  const lines = [element('rect', { x: left, y: top, width: right - left, height: bottom - top, class: 'curve-plot' })];
  for (let level = 0; level <= 5; level++) {
    lines.push(element('line', { x1: left, x2: right, y1: y(level), y2: y(level), class: 'chart-grid' }),
      element('text', { x: left - 10, y: y(level) + 4, 'text-anchor': 'end' }, level ? String(level) : 'Off'));
  }
  const interval = Math.max(5, Math.ceil((high - low) / (width < 450 ? 4 : 8) / 5) * 5);
  for (let value = Math.ceil(low / interval) * interval; value <= high; value += interval) {
    lines.push(element('line', { x1: x(value), x2: x(value), y1: top, y2: bottom, class: 'chart-grid' }),
      element('text', { x: x(value), y: bottom + 24, 'text-anchor': 'middle' }, String(value)));
  }
  lines.push(element('text', { x: left, y: 17 }, 'Fan level'),
    element('text', { x: (left + right) / 2, y: 331, 'text-anchor': 'middle' }, 'Temperature (°C)'));
  grid.replaceChildren(...lines);
  let path = `M${left},${bottom}`;
  values.forEach((temperature, index) => path += ` H${x(temperature)} V${y(index + 1)}`);
  path += ` H${right}`;
  state.steps.setAttribute('d', path);
  state.area.setAttribute('d', `${path} V${bottom} H${left} Z`);
  state.guide.setAttribute('d', values.map((value, index) => `${index ? 'L' : 'M'}${x(value)},${y(index + 1)}`).join(' ') + ` H${right}`);
  const selectedTemperature = values[state.selected];
  for (const [key, value] of Object.entries({ x1: x(selectedTemperature), x2: x(selectedTemperature), y1: top, y2: bottom })) state.selection.setAttribute(key, value);
  state.points.forEach((point, index) => {
    point.setAttribute('transform', `translate(${x(values[index])},${y(index + 1)})`);
    point.setAttribute('aria-valuenow', values[index]);
    point.setAttribute('aria-valuetext', `${values[index]} degrees Celsius`);
    point.setAttribute('aria-disabled', String(state.disabled));
    point.classList.toggle('is-selected', state.selected === index);
  });
  const highest = values.lastIndexOf(selectedTemperature) + 1;
  state.readout.textContent = state.invalid ? 'Temperatures must be nondecreasing, from -20 to 60 °C. Equal thresholds are allowed.'
    : `Level ${state.selected + 1} · ${selectedTemperature} °C${values.filter(value => value === selectedTemperature).length > 1 ? ` · At this temperature, level ${highest} wins.` : ''}`;
}

export function renderCurve(container, temperatures, onChange, { disabled = false } = {}) {
  let state = editors.get(container);
  if (!state) state = createEditor(container);
  state.onChange = onChange;
  const valid = validThresholds(temperatures);
  state.invalid = !valid;
  state.disabled = disabled || !valid;
  if (valid) state.values = [...temperatures];
  if (state.values.length) draw(state);
  else state.readout.textContent = 'Enter five nondecreasing temperatures from -20 to 60 °C.';
}
