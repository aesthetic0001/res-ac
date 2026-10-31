export const presets = { quiet: [24, 26, 28, 30, 32], balanced: [22, 24, 26, 28, 30], cooling: [20, 22, 24, 26, 28] };
export function renderCurve(container, temperatures) {
  if (temperatures.some(value => !Number.isFinite(value)) || temperatures.some((value, index) => index && value <= temperatures[index - 1])) {
    container.textContent = 'Enter ascending temperatures to preview the curve.'; return;
  }
  const create = (name, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const svg = create('svg', { viewBox: '0 0 560 210', role: 'img', 'aria-label': 'Temperature curve: off below the first threshold, then levels 1 through 5.' });
  const low = temperatures[0] - 3, high = temperatures[4] + 3;
  const x = value => 38 + (value - low) / (high - low) * 500;
  const y = level => 160 - level * 27;
  for (let level = 0; level <= 5; level++) {
    svg.append(create('line', { x1: 38, x2: 538, y1: y(level), y2: y(level), class: 'chart-grid' }));
    svg.append(create('text', { x: 25, y: y(level) + 4, 'text-anchor': 'end' }, level ? String(level) : 'Off'));
  }
  let path = `M38,${y(0)}`;
  temperatures.forEach((value, index) => {
    path += ` H${x(value)} V${y(index + 1)}`;
    svg.append(create('text', { x: x(value), y: 183, 'text-anchor': 'middle' }, `${value}°`));
  });
  path += ' H538';
  svg.append(create('path', { d: path, class: 'chart-line' }));
  svg.append(create('text', { x: 288, y: 205, 'text-anchor': 'middle' }, 'Temperature (°C)'));
  container.replaceChildren(svg);
}
