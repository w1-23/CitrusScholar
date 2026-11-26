import { element } from './dom.js';

const ns = 'http://www.w3.org/2000/svg';
const symbols = {
  o: 'M17 10a5 5 0 1 0-10 0 5 5 0 1 0 10 0',
  s: 'M7 5h10v10H7z',
  '^': 'M12 4l6 11H6z',
  v: 'M12 16L6 5h12z',
  D: 'M12 3l6 7-6 7-6-7z',
  P: 'M10 4h4v4h4v4h-4v4h-4v-4H6V8h4z',
  X: 'M8 4l4 3 4-3 2 2-3 4 3 4-2 2-4-3-4 3-2-2 3-4-3-4z',
  '+': 'M6 10h12M12 4v12',
  x: 'M7 5l10 10m0-10L7 15',
  '.': 'M14 10a2 2 0 1 0-4 0 2 2 0 1 0 4 0',
};
const dashes = { '--': '5 3', '-.': '6 2 1 2', ':': '1 2' };

function sample(kind, option) {
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 20');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  const empty = option === 'None';
  const marker = kind === 'marker';
  path.setAttribute('d', empty ? 'M16 10a4 4 0 1 0-8 0 4 4 0 1 0 8 0m-7 3 6-6' : marker ? symbols[option] : 'M1 10h22');
  path.setAttribute('fill', marker && !empty && !['+', 'x'].includes(option) ? 'currentColor' : 'none');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.4');
  if (!marker && dashes[option]) path.setAttribute('stroke-dasharray', dashes[option]);
  svg.append(path);
  return svg;
}

export function styleGrid(form, kind, label, options, value, names) {
  const group = element('fieldset', undefined, 'style-grid');
  group.append(element('legend', label));
  if (!options.includes(value) && value != null) group.append(element('p', `当前：${String(value)}`, 'muted'));
  const grid = element('div', undefined, 'style-options');
  const radios = [];
  for (const option of options) {
    const control = element('label');
    const radio = element('input');
    radio.type = 'radio';
    radio.name = kind;
    radio.value = option;
    radio.checked = value === option;
    control.append(radio, sample(kind, option), element('span', names[option] || option));
    grid.append(control);
    radios.push(radio);
  }
  group.append(grid);
  form.append(group);
  return { events: radios, read: () => radios.find((node) => node.checked)?.value || '' };
}
