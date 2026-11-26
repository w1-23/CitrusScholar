import { element } from './dom.js';

const storageKey = 'citrus:workbench:recentColors';
const normalize = (hex) => {
  if (!/^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(hex)) return '';
  const raw = hex.slice(1).toLowerCase();
  return '#' + (raw.length === 3 ? [...raw].map((letter) => letter.repeat(2)).join('') : raw);
};
const channels = (hex) => hex.slice(1).match(/../g).map((part) => Number.parseInt(part, 16));
const toHex = (rgb) => '#' + rgb.map((channel) => Math.round(channel).toString(16).padStart(2, '0')).join('');

function toHsv(rgb, previousHue) {
  const [r, g, b] = rgb.map((value) => value / 255);
  const high = Math.max(r, g, b), low = Math.min(r, g, b), span = high - low;
  let hue = previousHue;
  if (span) {
    if (high === r) hue = (g - b) / span;
    else if (high === g) hue = (b - r) / span + 2;
    else hue = (r - g) / span + 4;
    hue = ((hue * 60) + 360) % 360;
  }
  return [hue, high ? span / high : 0, high];
}

function toRgb([hue, saturation, value]) {
  const section = hue / 60;
  const span = value * saturation;
  const middle = span * (1 - Math.abs((section % 2) - 1));
  const cases = [[span, middle, 0], [middle, span, 0], [0, span, middle], [0, middle, span], [middle, 0, span], [span, 0, middle]];
  return cases[Math.floor(section) % 6].map((channel) => (channel + value - span) * 255);
}

function labeled(container, text, input) {
  const label = element('label');
  label.append(element('span', text), input);
  container.append(label);
  return input;
}

function number(max, step = 1, type = 'number') {
  const node = element('input');
  node.type = type;
  node.min = '0';
  node.max = String(max);
  node.step = String(step);
  return node;
}

function recentColors() {
  try {
    const colors = JSON.parse(localStorage.getItem(storageKey));
    return Array.isArray(colors) ? colors.map(normalize).filter(Boolean).slice(0, 8) : [];
  } catch { return []; }
}

function remember(hex) {
  try { localStorage.setItem(storageKey, JSON.stringify([hex, ...recentColors().filter((color) => color !== hex)].slice(0, 8))); }
  catch { /* The picker is usable even when browser storage is unavailable. */ }
}

function colorDialog(trigger, field, name) {
  const dialog = element('dialog', undefined, 'color-dialog');
  dialog.setAttribute('aria-label', `${name}色板`);
  const content = element('div', undefined, 'color-dialog-content');
  content.append(element('h3', `${name}色板`));
  const visual = element('div', undefined, 'color-space');
  visual.setAttribute('aria-hidden', 'true');
  const indicator = element('span');
  visual.append(indicator);
  content.append(visual);
  const sliders = element('div', undefined, 'color-sliders');
  const slider = (label, max, step) => labeled(sliders, label, number(max, step, 'range'));
  const hue = slider('色相', 359, 1);
  const saturation = slider('饱和度', 100, 1);
  const value = slider('明度', 100, 1);
  content.append(sliders);
  const inputs = element('div', undefined, 'color-inputs');
  const hex = labeled(inputs, 'HEX', element('input'));
  hex.maxLength = 7;
  hex.pattern = '#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})';
  hex.required = true;
  const rgb = ['R', 'G', 'B'].map((channel) => labeled(inputs, channel, number(255)));
  content.append(inputs);
  const comparison = element('div', undefined, 'color-comparison');
  const oldColor = element('output');
  const newColor = element('output');
  for (const [caption, output] of [['原颜色', oldColor], ['新颜色', newColor]]) {
    const label = element('div', caption);
    output.setAttribute('aria-label', caption);
    output.style.setProperty('--chip', 'transparent');
    label.append(output);
    comparison.append(label);
  }
  content.append(comparison);
  const recent = element('div', undefined, 'recent-colors');
  recent.setAttribute('role', 'group');
  recent.setAttribute('aria-label', '最近使用的颜色');
  content.append(recent);
  const buttons = element('div', undefined, 'picker-actions');
  const cancel = element('button', '取消');
  const accept = element('button', '使用此颜色', 'primary');
  cancel.type = accept.type = 'button';
  buttons.append(cancel, accept);
  content.append(buttons);
  dialog.append(content);
  let hsv = [0, 0, 0], current = '#000000';

  function update(rgbValue, skip = '') {
    current = toHex(rgbValue);
    if (skip !== 'hex') hex.value = current;
    rgb.forEach((channel, index) => {
      if (skip !== 'rgb') channel.value = Math.round(rgbValue[index]);
    });
    hue.value = Math.round(hsv[0]) % 360;
    saturation.value = Math.round(hsv[1] * 100);
    value.value = Math.round(hsv[2] * 100);
    hue.setAttribute('aria-valuetext', `${hue.value} 度`);
    for (const control of [saturation, value]) control.setAttribute('aria-valuetext', `${control.value}%`);
    visual.style.setProperty('--hue', `hsl(${hsv[0]} 100% 50%)`);
    indicator.style.left = `${hsv[1] * 100}%`;
    indicator.style.top = `${(1 - hsv[2]) * 100}%`;
    newColor.textContent = current;
    newColor.style.setProperty('--chip', current);
    accept.disabled = false;
  }
  function fromHex(color, skip) {
    const values = channels(color);
    hsv = toHsv(values, hsv[0]);
    update(values, skip);
  }
  trigger.addEventListener('click', () => {
    const old = normalize(field.value);
    oldColor.textContent = old || '未设置统一值';
    oldColor.style.setProperty('--chip', old || 'transparent');
    hsv = [0, 0, 0];
    fromHex(old || '#000000');
    const colors = recentColors();
    recent.hidden = !colors.length;
    recent.replaceChildren(element('span', '最近使用'));
    for (const color of colors) {
      const swatch = element('button', color);
      swatch.type = 'button';
      swatch.style.setProperty('--chip', color);
      swatch.addEventListener('click', () => fromHex(color));
      recent.append(swatch);
    }
    dialog.showModal();
    hue.focus();
  });
  for (const control of [hue, saturation, value]) {
    control.addEventListener('input', () => {
      hsv = [hue.valueAsNumber, saturation.valueAsNumber / 100, value.valueAsNumber / 100];
      update(toRgb(hsv));
    });
  }
  hex.addEventListener('input', () => {
    const color = normalize(hex.value);
    if (color) fromHex(color, 'hex');
    else accept.disabled = true;
  });
  for (const input of rgb) {
    input.required = true;
    input.addEventListener('input', () => {
      if (rgb.some((channel) => !channel.checkValidity())) { accept.disabled = true; return; }
      const values = rgb.map((channel) => channel.valueAsNumber);
      hsv = toHsv(values, hsv[0]);
      update(values, 'rgb');
    });
  }
  function position(event) {
    const rect = visual.getBoundingClientRect();
    const fraction = (position, min, size) => Math.max(0, Math.min(1, (position - min) / size));
    hsv = [hsv[0], fraction(event.clientX, rect.left, rect.width), 1 - fraction(event.clientY, rect.top, rect.height)];
    update(toRgb(hsv));
  }
  visual.addEventListener('pointerdown', (event) => {
    if (!event.isPrimary || event.button !== 0) return;
    visual.setPointerCapture(event.pointerId);
    position(event);
  });
  visual.addEventListener('pointermove', (event) => {
    if (visual.hasPointerCapture(event.pointerId)) position(event);
  });
  cancel.addEventListener('click', () => dialog.close());
  accept.addEventListener('click', () => {
    if (accept.disabled) return;
    if (field.value !== current) {
      field.value = current;
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
    remember(current);
    dialog.close();
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation();
    dialog.close();
  });
  dialog.addEventListener('close', () => trigger.focus());
  return dialog;
}

export function colorControl(parent, label, value, field) {
  const root = element('div', undefined, 'color-control');
  const hex = element('input');
  hex.placeholder = '#RRGGBB';
  hex.maxLength = 7;
  hex.pattern = '#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})';
  hex.value = normalize(value);
  const open = element('button', '打开色板');
  open.type = 'button';
  open.setAttribute('aria-label', `打开${label}色板`);
  open.setAttribute('aria-haspopup', 'dialog');
  const chip = () => open.style.setProperty('--chip', normalize(hex.value) || 'transparent');
  hex.addEventListener('input', chip);
  hex.addEventListener('change', chip);
  chip();
  field(root, label, hex);
  root.append(open, colorDialog(open, hex, label));
  parent.append(root);
  return { root, input: hex, open };
}
