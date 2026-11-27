import { element } from './dom.js';
import { colorControl } from './color-picker.js?v=1';
import { styleGrid } from './style-grid.js?v=1';

const labels = {
  text: '文字', label: '名称', color: '系列颜色', facecolor: '填充颜色', edgecolor: '边框颜色',
  linewidth: '线宽', linestyle: '线型', marker: '标记形状', markersize: '标记大小',
  markerfacecolor: '标记填充', markeredgecolor: '标记边框颜色', markeredgewidth: '标记边框宽度',
  fontsize: '字号', fontfamily: '字体', fontweight: '字重', fontstyle: '字形', horizontalalignment: '对齐',
  loc: '图例位置', ncols: '图例列数', offset_px: '位置偏移（SVG 点）', alpha: '不透明度',
  cmap: '热图色带', visible: '显示', frameon: '显示图例边框', transparent: '透明背景（SVG / PNG）', capsize: '误差棒端帽大小',
};
const propertyOrder = Object.keys(labels);
const selections = {
  linestyle: { '-': '实线', '--': '虚线', '-.': '点划线', ':': '点线', None: '无线条' },
  marker: { o: '圆形', s: '方形', '^': '向上三角', v: '向下三角', D: '菱形',
    P: '实心十字', X: '实心叉', '+': '十字', x: '叉号', '.': '圆点', None: '无标记' },
  fontweight: { normal: '常规', light: '细体', medium: '中等', semibold: '半粗', bold: '粗体', heavy: '特粗', black: '最粗' },
  fontstyle: { normal: '正体', italic: '斜体', oblique: '倾斜' },
  horizontalalignment: { left: '左对齐', center: '居中', right: '右对齐' },
  loc: { best: '自动', 'upper right': '右上', 'upper left': '左上', 'lower left': '左下',
    'lower right': '右下', right: '右侧', 'center left': '左中', 'center right': '右中',
    'lower center': '底部居中', 'upper center': '顶部居中', center: '居中' },
};
const numeric = {
  linewidth: [.2, 8, 'any'], markersize: [1, 24, .1], markeredgewidth: [0, 8, .1],
  fontsize: [5, 24, .1], alpha: [0, 1, .05], ncols: [1, 6, 1], capsize: [0, 24, .1],
};
const color = (value) => typeof value === 'string' && /^#[\da-f]{6}$/i.test(value);

function select(options, value, names = {}) {
  const node = element('select');
  if (!options.includes(value)) {
    const unset = element('option', value == null ? '未设置统一值' : String(value));
    unset.value = '';
    node.append(unset);
  }
  for (const option of options) {
    const entry = element('option', names[option] ?? option);
    entry.value = option;
    node.append(entry);
  }
  node.value = options.includes(value) ? value : '';
  return node;
}

function field(parent, label, input) {
  const wrapper = element('label');
  input.setAttribute('aria-label', label);
  if (input.type === 'checkbox') wrapper.className = 'check';
  wrapper.append(element('span', label), input);
  parent.append(wrapper);
  return input;
}

function numberInput(range, value, optional = true) {
  const input = element('input');
  input.type = 'number';
  [input.min, input.max, input.step] = range;
  input.value = value ?? '';
  input.placeholder = '未设置统一值';
  input.required = !optional;
  return input;
}

function markerFill(parent, value, modes) {
  const box = element('fieldset', undefined, 'fill-options');
  box.append(element('legend', labels.markerfacecolor));
  const current = color(value) ? 'custom' : value;
  const mode = field(box, '填充方式', select(modes, current, { auto: '跟随系列', none: '无填充', custom: '自定义' }));
  const { root, input, open } = colorControl(box, '标记自定义颜色', value, field);
  const show = () => {
    const custom = mode.value === 'custom';
    input.required = custom;
    input.disabled = !custom;
    if (custom && !input.value) input.value = '#ffffff';
    open.disabled = !custom;
    root.hidden = !custom;
  };
  mode.addEventListener('change', show);
  show();
  parent.append(box);
  return { root: box, read: () => mode.value === 'custom' ? input.value : mode.value, events: [mode, input] };
}

function addControl(form, prop, value, snapshot, group) {
  const name = prop === 'color' && !['series', 'sync'].includes(form.dataset.kind) ? '颜色' : labels[prop];
  let input;
  if (prop === 'offset_px') {
    const box = element('fieldset');
    box.append(element('legend', name));
    const x = field(box, '水平', numberInput([-200, 200, .1], value?.[0] ?? 0, false));
    const y = field(box, '垂直', numberInput([-200, 200, .1], value?.[1] ?? 0, false));
    form.append(box);
    return { events: [x, y], read: () => [x.valueAsNumber, y.valueAsNumber] };
  }
  if (prop === 'markerfacecolor') return markerFill(form, value, (group.line_marker_fill || group.members?.every((member) => member.line_marker_fill)) ? ['auto', 'none', 'custom'] : snapshot.marker_fill_states);
  if (prop === 'marker' || prop === 'linestyle') {
    const options = (snapshot.options[prop] || []).filter((option) => group.kind !== 'errorbar' || option !== 'None');
    return styleGrid(form, prop, name, options, value, selections[prop]);
  }
  if (prop.endsWith('color')) {
    input = colorControl(form, name, value, field).input;
  } else {
    if (['visible', 'frameon', 'transparent'].includes(prop)) {
      input = element('input');
      input.type = 'checkbox';
      input.indeterminate = value === undefined;
      input.checked = value === true;
    } else if (numeric[prop]) input = numberInput(numeric[prop], value);
    else if (snapshot.options[prop]) input = select(snapshot.options[prop], value, selections[prop]);
    else {
      input = element('input');
      input.maxLength = 2000;
      input.value = value ?? '';
    }
    field(form, name, input);
  }
  input.name = prop;
  return { events: [input], read: () => input.type === 'checkbox' ? input.checked :
    input.type === 'number' ? input.valueAsNumber : input.value };
}

export function renderInspector(container, snapshot, group, values, update) {
  container.replaceChildren();
  if (!group) {
    container.append(element('p', '选择对象组以查看可用属性。', 'muted'));
    return;
  }
  const members = group.members || [group];
  const batch = group.kind === 'category';
  container.append(element('p', batch ? `批量属性 · ${members.length} 个对象` : '单项属性', 'property-eyebrow'));
  container.append(element('h3', values.label || group.label));
  if (batch) container.append(element('p', '批量修改此类别的共有属性。展开对象组可单独编辑。', 'muted'));
  if (snapshot.read_only_reason || group.reason) container.append(element('p', snapshot.read_only_reason || group.reason, 'muted'));
  const properties = propertyOrder.filter((prop) => group.properties.includes(prop));
  if (!properties.length) {
    if (!snapshot.read_only_reason && !group.reason) container.append(element('p', '此组没有可编辑的样式。', 'muted'));
    return;
  }
  const form = element('form');
  form.dataset.kind = group.styleKind || group.kind;
  const controls = new Map();
  const touched = new Set();
  const apply = element('button', batch ? '应用到类别' : '应用到此组', 'primary');
  apply.disabled = true;
  for (const prop of properties) {
    const control = addControl(form, prop, values[prop], snapshot, group);
    controls.set(prop, control);
    for (const input of control.events) {
      for (const event of ['input', 'change']) input.addEventListener(event, () => {
        touched.add(prop);
        apply.disabled = false;
      });
    }
  }
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const entries = [];
    for (const prop of touched) {
      const value = controls.get(prop).read();
      if (typeof value === 'number' && !Number.isFinite(value)) continue;
      if (value === '' && !['text', 'label'].includes(prop)) continue;
      if (JSON.stringify(value) !== JSON.stringify(values[prop])) {
        for (const member of members) entries.push({ group_id: member.id, property: prop, value });
      }
    }
    if (entries.length) update(entries);
    else apply.disabled = true;
  });
  form.append(apply);
  container.append(form, ...form.querySelectorAll('dialog'));
}

export function renderTree(tree, groups, selected, expanded, choose, values) {
  const childGroups = new Map();
  for (const group of groups) {
    if (!childGroups.has(group.parent)) childGroups.set(group.parent, []);
    childGroups.get(group.parent).push(group);
  }
  const focused = tree.contains(document.activeElement);
  tree.replaceChildren();
  const visible = [];
  const nodes = new Map();
  function append(parent, key, parentExpanded = true) {
    for (const group of childGroups.get(key) || []) {
      const item = element('li');
      item.setAttribute('role', 'treeitem');
      item.dataset.group = group.id;
      item.dataset.kind = group.kind;
      item.setAttribute('aria-selected', String(group.id === selected));
      item.tabIndex = group.id === selected ? 0 : -1;
      nodes.set(group.id, item);
      if (parentExpanded) visible.push(item);
      const row = element('div', undefined, 'tree-row');
      const branch = childGroups.has(group.id);
      const open = expanded.has(group.id);
      if (branch) item.setAttribute('aria-expanded', String(open));
      const arrow = element('span', branch ? open ? '▾' : '▸' : '', 'tree-arrow');
      arrow.setAttribute('aria-hidden', 'true');
      row.append(arrow);
      const style = values(group);
      const caption = style.label || group.label;
      const paint = [style.facecolor, style.color, style.edgecolor].find(color);
      if (paint) {
        const swatch = element('span', undefined, 'series-swatch');
        swatch.setAttribute('aria-hidden', 'true');
        swatch.style.color = paint;
        if (style.linestyle === '--') swatch.style.borderTopStyle = 'dashed';
        if (style.linestyle === ':') swatch.style.borderTopStyle = 'dotted';
        if (style.visible === false) swatch.style.opacity = '.35';
        row.append(swatch);
      }
      row.append(element('span', caption, 'tree-name'));
      if (group.members) row.append(element('span', String(group.members.length), 'tree-count'));
      item.setAttribute('aria-label', caption);
      item.append(row);
      if (branch) {
        const list = element('ul');
        list.setAttribute('role', 'group');
        list.hidden = !open;
        append(list, group.id, parentExpanded && open);
        item.append(list);
      }
      row.addEventListener('click', (event) => {
        const expand = event.target === arrow && branch;
        if (expand) {
          if (open) expanded.delete(group.id);
          else expanded.add(group.id);
        }
        choose(group.id, true, !expand);
      });
      item.addEventListener('keydown', (event) => {
        if (event.target !== item) return;
        const index = visible.indexOf(item);
        let next;
        switch (event.key) {
          case 'ArrowDown': next = visible[index + 1]; break;
          case 'ArrowUp': next = visible[index - 1]; break;
          case 'Home': next = visible[0]; break;
          case 'End': next = visible.at(-1); break;
          case 'ArrowLeft':
            if (branch && open) { expanded.delete(group.id); choose(group.id, true, false); }
            else next = nodes.get(group.parent);
            break;
          case 'ArrowRight':
            if (branch && !open) { expanded.add(group.id); choose(group.id, true, false); }
            else if (branch) next = nodes.get(childGroups.get(group.id)[0].id);
            break;
          case 'Enter':
          case ' ': choose(group.id, true, true); break;
          default: return;
        }
        event.preventDefault();
        if (next) choose(next.dataset.group, true, false);
      });
      parent.append(item);
    }
  }
  append(tree, null);
  const current = nodes.get(selected) || visible[0];
  if (current) current.tabIndex = 0;
  if (focused && current) current.focus({ preventScroll: true });
  return current;
}
