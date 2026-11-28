// SPDX-License-Identifier: AGPL-3.0-only
import {api} from './api.mjs?v=2';
import {nativeEditor} from './inspector-fields.mjs';
import {treeKeyAction} from './tavotto-primitives.mjs';

export const cardOrdinal = card => Number.isInteger(card.figure_ordinal) && card.figure_ordinal >= 0 ? String(card.figure_ordinal + 1) : '—';
export const cardEditable = card => card?.source_state === 'verified' && ['native','semantic'].includes(card.capability);

export const $ = id => document.getElementById(id);
const MODES = {semantic:'语义编辑',partial:'部分可编辑',layout_only:'静态 / 排版受限',preview_only:'仅预览'};
const BLOCKS = {locked:'组内存在拾取锁定对象', not_resizable:'此自动组不支持变换', incomplete:'组成员的布局数据不完整'};
export const make = (tag, text, cls) => Object.assign(document.createElement(tag), {textContent:text ?? '', className:cls ?? ''});
const hint = text => make('p', text, 'muted');

export function button(text, action, cls) {
  const el = make('button', text, cls);
  el.type = 'button';
  el.addEventListener('click', action);
  return el;
}

function rows(id, children, fallback) {
  $(id).replaceChildren(...(children.length ? children : [hint(fallback)]));
}

function item(title, caption, active, action) {
  const el = button('', action, 'row');
  const text = make('span', title, 'row-label');
  if (caption) text.append(make('small', caption));
  el.append(text);
  if (active) el.setAttribute('aria-current', 'true');
  return el;
}

export function listFigures(figures, active, open) {
  rows('figures', figures.map(f => item(f.name, `版本 ${f.revision}`, f.figure_id === active, () => open(f.figure_id))), '尚未导入图件。');
}

function solar(name) {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns,'svg');
  svg.setAttribute('class','icon'); svg.setAttribute('aria-hidden','true');
  const use = document.createElementNS(ns,'use');
  use.setAttribute('href',`static/solar.svg#ui-${name}`);
  svg.append(use);
  return svg;
}

export function listCards(cards, open) {
  rows('assets', cards.flatMap(card => {
    const src = api.thumbnail(card.thumbnail_url);
    if (!src) return [];
    const editable = cardEditable(card);
    const el = item(card.label, `图 ${cardOrdinal(card)} · ${editable ? '可编辑' : '仅预览'}`, false, () => open(card));
    el.classList.add('preview-card');
    const img = make('img');
    img.alt = '';
    img.loading = 'lazy';
    img.src = src;
    img.addEventListener('error',() => {
      img.replaceWith(solar('image'));
      el.disabled = true;
      el.querySelector('small').textContent = '渲染缩略图不可用';
    },{once:true});
    el.prepend(img);
    return [el];
  }), '还没有准备好的图件预览。使用项目旁的 ＋ 从源文件准备。');
}

const expanded = new Map();
let treeFigure;
export function listObjects(doc, selection, locks, onSelect) {
  if (treeFigure !== doc?.figure_id) {
    treeFigure = doc?.figure_id;
    expanded.clear();
  }
  const oldFocus = $('object-list').contains(document.activeElement) ? document.activeElement.dataset.treeId : null;
  const root = make('ul','','tree');
  root.setAttribute('role','tree');
  root.setAttribute('aria-label','原生对象与分组');
  const native = doc?.native_manifest;
  const query = $('object-search').value.trim().toLowerCase();
  const matches = obj => !query || [obj.gid,obj.id,obj.role,obj.kind,obj.label,obj.name].some(part => typeof part === 'string' && part.toLowerCase().includes(query));
  const elements = new Map((native?.elements ?? []).map(element => [element.gid,element]));
  const placed = new Set();
  const keyOf = (kind,id) => `${kind}:${id}`;
  const row = (kind,object,level,children = []) => {
    const id = object.gid ?? object.id;
    const key = keyOf(kind,id);
    const open = !!query || (expanded.get(key) ?? true);
    const li = make('li');
    const el = make('div','','tree-row');
    el.setAttribute('role','treeitem'); el.setAttribute('aria-level',String(level));
    el.setAttribute('aria-selected',String(selection?.kind === kind && selection.id === id));
    el.dataset.treeId = key;
    el.tabIndex = -1;
    li.setAttribute('role','none');
    li.append(el);
    if (children.length) {
      const toggle = make('span','','tree-disclose');
      toggle.setAttribute('aria-hidden','true');
      toggle.append(solar('chevron-right'));
      const branch = make('ul');
      branch.setAttribute('role','group');
      branch.id = `tree-group-${root.querySelectorAll('[role=group]').length}`;
      branch.hidden = !open;
      branch.append(...children);
      el.setAttribute('aria-owns',branch.id);
      el.setAttribute('aria-expanded',String(open));
      el.prepend(toggle);
      li.append(branch);
      el.treeToggle = () => {
        const next = el.getAttribute('aria-expanded') !== 'true';
        expanded.set(key,next);
        el.setAttribute('aria-expanded',String(next));
        branch.hidden = !next;
      };
      toggle.addEventListener('click',e => { e.stopPropagation(); el.focus(); el.treeToggle(); });
    }
    el.append(solar(kind === 'group' ? 'layers' : ['title','text','axis_label','legend_text'].includes(object.role) ? 'text' : object.role === 'axes' ? 'axis' : 'file'), make('span',object.label || object.name || id));
    if (locks.has(id)) el.append(make('small','已锁定'));
    el.addEventListener('click',() => { el.focus(); onSelect(kind,id); });
    el.addEventListener('focus',() => {
      for (const other of root.querySelectorAll('[role=treeitem]')) other.tabIndex = other === el ? 0 : -1;
    });
    el.addEventListener('keydown',event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(kind,id); return; }
      const visibles = [...root.querySelectorAll('[role=treeitem]')].filter(item => !item.closest('[hidden]'));
      const rows = visibles.map(item => ({level:Number(item.getAttribute('aria-level')),expanded:item.hasAttribute('aria-expanded') ? item.getAttribute('aria-expanded') === 'true' : undefined}));
      const action = treeKeyAction(event.key,rows,visibles.indexOf(el));
      if (!action) return;
      event.preventDefault();
      if (action.type === 'focus') visibles[action.index].focus(); else el.treeToggle?.();
    });
    return li;
  };
  for (const group of native?.groups ?? []) {
    const members = group.members.flatMap(id => {
      if (!elements.has(id) || placed.has(id) || (!matches(group) && !matches(elements.get(id)))) return [];
      placed.add(id);
      return [row('native',elements.get(id),2)];
    });
    if (members.length || matches(group)) root.append(row('group',group,1,members));
  }
  if (native) {
    for (const [id,element] of elements) if (!placed.has(id) && matches(element)) root.append(row('native',element,1));
  } else {
    for (const object of doc?.objects ?? []) if (matches(object)) root.append(row('text',object,1));
  }
  rows('object-list', root.childElementCount ? [root] : [], doc ? query ? '没有匹配的对象。' : '此图件没有可列出的对象。' : '打开图件后可在这里选取对象。');
  const items = [...root.querySelectorAll('[role=treeitem]')].filter(item => !item.closest('[hidden]'));
  const focus = items.find(item => item.dataset.treeId === oldFocus);
  const active = focus ?? items.find(item => item.getAttribute('aria-selected') === 'true') ?? items[0];
  if (active) active.tabIndex = 0;
  if (focus) focus.focus({preventScroll:true});
}

export function listHistory(versions, doc, head, actions) {
  rows('version-list', versions.map(version => {
    const label = make('span', version.name || `版本 ${version.revision}`);
    if (version.name) label.append(make('small',`版本 ${version.revision}`));
    const date = new Date(typeof version.created_at === 'string' ? version.created_at.replace(' ', 'T') + (version.created_at.includes('T') ? '' : 'Z') : version.created_at * 1000);
    if (!Number.isNaN(date.getTime())) label.append(make('small', date.toLocaleString('zh-CN')));
    const active = version.revision === doc?.revision;
    const latest = version.revision === head;
    const open = button(active ? '正在查看' : latest ? '查看最新' : '查看', () => actions.view(version.revision));
    open.disabled = active;
    const row = make('div', '', 'version');
    const controls = make('div','','actions');
    controls.append(open,button('命名',() => actions.name(version)));
    if (!latest) controls.append(button('恢复',() => actions.restore(version.revision)));
    row.append(label,controls);
    return row;
  }), doc ? '暂无可显示的历史。' : '尚未打开图件。');
}

function field(labelText, value, jsonValue = false) {
  const label = make('label', labelText);
  const type = typeof value;
  const input = make(type === 'string' ? 'textarea' : 'input');
  if (type === 'string') { input.rows = 2; input.value = value; }
  else if (type === 'boolean') { input.type = 'checkbox'; input.checked = value; }
  else if (type === 'number') { input.type = 'number'; input.step = 'any'; input.required = true; input.value = value; }
  else { input.value = JSON.stringify(value ?? null); input.required = true; }
  label.append(input);
  if (jsonValue && type === 'object') label.append(make('small', '使用 JSON 表示数组、对象或空值。'));
  const read = () => type === 'boolean' ? input.checked : type === 'number' ? input.valueAsNumber : type === 'string' ? input.value : JSON.parse(input.value);
  return {label, input, read};
}

function lockToggle(locked, callback) {
  const {label, input} = field('锁定拾取与组变换', locked);
  label.classList.add('inspector-lock');
  input.addEventListener('change', () => callback(input.checked));
  return label;
}

function textEditor(pane, object, write, onSave) {
  const text = object.properties.text;
  if (typeof text !== 'string') { pane.append(hint('无文字属性。')); return; }
  const form = make('form', '', 'form-fields');
  const {label, input, read} = field('文字内容', text);
  input.maxLength = 500;
  input.disabled = !write;
  form.append(label);
  if (write) form.append(Object.assign(make('button', '保存文字', 'primary'), {type:'submit'}));
  form.addEventListener('submit', e => { e.preventDefault(); onSave({[object.id]:{text:read()}}, form); });
  pane.append(form);
}

function groupEditor(pane, data, write, actions) {
  if (!data) { pane.append(hint('正在验证组成员及变换能力…')); return; }
  const {group, resolution} = data;
  pane.append(hint('服务器识别的自动组。当前不支持手工创建或拆分项目级图层。'));
  for (const id of group.selectable_member_gids) pane.append(item(id, '查看组成员', false, () => actions.select('native', id)));
  const blocked = resolution.blocked[group.gid];
  if (!write || blocked || !resolution.expanded_gids.length) {
    pane.append(hint(blocked ? BLOCKS[blocked] ?? blocked : '此版本无法修改组。'));
    return;
  }
  for (const [kind, title, specs] of [
    ['move', '整体移动', [['dx_mm','向右（mm）',0,-400,400],['dy_mm','向下（mm）',0,-400,400]]],
    ['scale', '等比缩放', [['factor','缩放倍率',1,.05,10]]],
  ]) {
    const form = make('form', '', 'form-fields subsection');
    const controls = specs.map(([name, title, value, min, max]) => {
      const {label, input} = field(title, value);
      Object.assign(input, {min, max});
      form.append(label);
      return [name, input];
    });
    form.append(Object.assign(make('button', title), {type:'submit'}));
    form.addEventListener('submit', e => {
      e.preventDefault();
      actions.transform(group.gid, kind, Object.fromEntries(controls.map(([name, input]) => [name, input.valueAsNumber])), form);
    });
    pane.append(form);
  }
}

export function inspector(doc, selected, group, historical, locks, actions) {
  const pane = $('properties');
  pane.replaceChildren();
  $('mode').hidden = !doc;
  $('mode').textContent = MODES[doc?.editability] ?? '';
  if (doc) {
    const write = !historical && doc.editability !== 'preview_only';
    const {kind, id} = selected ?? {};
    if (historical) pane.append(make('p', '正在查看历史快照。恢复后会创建新版本。', 'disabled-note'));
    if (kind === 'group') {
      pane.append(make('h3', group?.group.label || id));
      groupEditor(pane, group, write, actions);
    } else if (kind === 'native') {
      const object = doc.native_manifest.elements.find(o => o.gid === id);
      if (object) {
        pane.append(make('h3', object.label || id));
        nativeEditor(pane, object, write && doc.native_editability !== 'preview_only', actions.nativeSave);
        if (id !== 'figure') pane.append(lockToggle(locks.has(id), locked => actions.lock(id, locked)));
      }
    } else if (kind === 'text') {
      const object = doc.objects.find(o => o.id === id);
      if (object) {
        pane.append(make('h3', object.label || id));
        textEditor(pane, object, write && object.editable.includes('text'), actions.textSave);
      }
    } else pane.append(hint('从对象列表或画布选取。此处只显示服务公开的属性。'));
  } else pane.append(hint('打开图件后，这里将显示版本对应的属性。'));
  const notes = $('limitations');
  notes.replaceChildren(make('h3', '功能边界', 'subsection'));
  const items = [ ...(doc?.limitations ?? []), '不支持多图页面排版、手工图层与分组、项目包或源脚本写回。' ];
  if (doc && !doc.native_manifest) items.push('当前图件不支持画布精确拾取或自动组变换。');
  if (doc?.editability === 'preview_only') items.push('当前图件仅预览，不能保存编辑。');
  const ul = make('ul');
  ul.append(...items.map(note => make('li', note)));
  notes.append(ul);
}
