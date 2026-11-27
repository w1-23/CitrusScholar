// SPDX-License-Identifier: AGPL-3.0-only
import {api} from './api.mjs';

export const $ = id => document.getElementById(id);
const MODES = {semantic:'语义编辑',partial:'部分可编辑',layout_only:'静态 / 排版受限',preview_only:'仅预览'};
const PROPS = {text:'文字',color:'颜色',fontsize:'字号',linewidth:'线宽',visible:'可见',position:'位置',alpha:'不透明度',label:'标签'};
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
  const text = make('span', title);
  if (caption) text.append(make('small', caption));
  el.append(text);
  if (active) el.setAttribute('aria-current', 'true');
  return el;
}

export function listFigures(figures, active, open) {
  rows('figures', figures.map(f => item(f.name, `版本 ${f.revision}`, f.figure_id === active, () => open(f.figure_id))), '尚未导入图件。');
}

export function listAssets(assets, sourceRefs, open) {
  rows('assets', assets.map(asset => {
    const recommended = sourceRefs.has(asset.asset_ref) ? ' · 来自本次会话' : '';
    const el = item(asset.name, `${asset.format.toUpperCase()} · ${MODES[asset.editability] ?? '未标注能力'}${recommended}`, false, () => open(asset));
    if (/^(png|svg|jpe?g)$/i.test(asset.format) && asset.asset_ref.startsWith('library:')) {
      const img = make('img');
      img.alt = '';
      img.loading = 'lazy';
      img.src = api.assetPreview(asset.asset_ref);
      img.addEventListener('error', () => img.remove(), {once:true});
      el.prepend(img);
    }
    return el;
  }), '没有符合条件的已授权资料。独立模式可从本地文件导入。');
}

export function listObjects(doc, selection, locks, onSelect) {
  const list = [];
  const add = (kind, title, objects, key, caption) => {
    if (!objects.length) return;
    list.push(make('h3', title, 'subsection'), ...objects.map(obj => {
      const id = obj[key];
      const label = obj.label || obj.name || id;
      return item(label, `${caption(obj)}${locks.has(id) ? ' · 已锁定拾取' : ''}`, selection?.kind === kind && selection.id === id, () => onSelect(kind, id));
    }));
  };
  add('group', '自动分组', doc?.native_manifest?.groups ?? [], 'gid', obj => `${obj.members.length} 个成员 · ${obj.gid}`);
  add('native', '原生对象', doc?.native_manifest?.elements ?? [], 'gid', obj => `${obj.role || '对象'} · ${obj.gid}`);
  if (!doc?.native_manifest) {
    add('text', '图件对象', doc?.objects ?? [], 'id', obj => `${obj.kind} · ${obj.panel_id ?? '图件'}`);
  }
  rows('object-list', list, doc ? '此图件没有可列出的对象。' : '打开图件后可在这里选取对象。');
}

export function listHistory(versions, doc, head, actions) {
  rows('version-list', versions.map(version => {
    const label = make('span', `版本 ${version.revision}`);
    const date = new Date(typeof version.created_at === 'string' ? version.created_at.replace(' ', 'T') + (version.created_at.includes('T') ? '' : 'Z') : version.created_at * 1000);
    if (!Number.isNaN(date.getTime())) label.append(make('small', date.toLocaleString('zh-CN')));
    const active = version.revision === doc?.revision;
    const latest = version.revision === head;
    const open = button(active ? '正在查看' : latest ? '查看最新' : '查看', () => actions.view(version.revision));
    open.disabled = active;
    const row = make('div', '', 'version');
    row.append(label, open);
    if (!latest) row.append(button('恢复', () => actions.restore(version.revision)));
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
  const {label, input} = field('锁定画布拾取与自动组变换', locked);
  input.addEventListener('change', () => callback(input.checked));
  return label;
}

function nativeEditor(pane, object, write, onSave) {
  const props = object.editable ?? [];
  if (!props.length) { pane.append(hint('此原生对象没有公开的可编辑属性。')); return; }
  const form = make('form', '', 'form-fields');
  const fields = props.map(({prop, value}) => {
    const control = field(`${PROPS[prop] ?? prop}${PROPS[prop] ? ` · ${prop}` : ''}`, value, true);
    control.input.disabled = !write;
    form.append(control.label);
    return {control, value, prop};
  });
  if (write) form.append(Object.assign(make('button', '保存对象修改', 'primary'), {type:'submit'}));
  form.addEventListener('submit', e => {
    e.preventDefault();
    try {
      const patches = fields.flatMap(({control, value, prop}) => {
        const parsed = control.read();
        return JSON.stringify(parsed) === JSON.stringify(value) ? [] : [{gid:object.gid, prop, value:parsed, ...(object.identity ? {identity:object.identity} : {})}];
      });
      onSave(patches, form);
    } catch {
      let message = form.querySelector('.form-error');
      if (!message) { message = make('p', '', 'form-error'); message.setAttribute('role', 'alert'); form.append(message); }
      message.textContent = '属性值必须符合所示 JSON 格式。';
    }
  });
  pane.append(form);
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
        if (id !== 'figure') pane.append(lockToggle(locks.has(id), locked => actions.lock(id, locked)));
        nativeEditor(pane, object, write && doc.native_editability !== 'preview_only', actions.nativeSave);
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
