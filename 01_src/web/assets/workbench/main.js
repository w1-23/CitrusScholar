import { byId, element } from './dom.js';
import { renderInspector, renderTree } from './controls.js?v=2';
import { CanvasView, imageAsset } from './canvas.js?v=2';
import { initializeAssets, initializeOpenDialog } from './library.js?v=2';

const api = '/api/v2/figure-workbench';
const state = { project: null, selected: null, expanded: new Set(), snapshots: [[]], cursor: 0,
  epoch: 0, draft: 0, timer: null, flight: null, saving: false, previewOk: true };
const starting = new URLSearchParams(location.search);
const path = (id) => `${api}/projects/${encodeURIComponent(id)}`;
const revisionUrl = (project, suffix = '') => `${path(project.figure_id)}${suffix}?revision=${project.render_version}`;
const commands = () => state.snapshots[state.cursor];
const commandKey = (item) => JSON.stringify([item.group_id, item.property]);
const changed = () => commands().length > 0;
const status = (message, error = false) => {
  byId('status').textContent = message;
  byId('status').dataset.error = String(error);
};
const canvas = new CanvasView((id) => selectGroup(id, false, true), (id, x, y) => {
  if (state.saving) return;
  const group = state.project?.groups.find((entry) => entry.id === id);
  if (!group?.properties.includes('offset_px')) return;
  const previous = effective(group).offset_px || [0, 0];
  const bound = (n) => Math.max(-200, Math.min(200, Math.round(n * 10) / 10));
  applyChanges([{ group_id: id, property: 'offset_px', value: [bound(previous[0] + x), bound(previous[1] + y)] }]);
});

async function request(url, init = {}) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init });
  if (response.ok) return response;
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail;
  const error = new Error(typeof detail === 'string' ? detail : detail?.message || `请求失败（${response.status}）`);
  error.status = response.status;
  throw error;
}

function post(data) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
}

function editUrl(project, operation) { return `${path(project.figure_id)}/${operation}`; }

function effective(group) {
  const values = { ...group.values };
  for (const entry of commands()) if (entry.group_id === group.id) values[entry.property] = entry.value;
  return values;
}

function openDrawer(side, open) {
  const sidebar = byId(side === 'left' ? 'groups' : 'inspector');
  const button = byId(side === 'left' ? 'toggleGroups' : 'toggleInspector');
  if (open && matchMedia('(max-width: 920px)').matches) {
    const other = side === 'left' ? 'right' : 'left';
    openDrawer(other, false);
  }
  sidebar.classList.toggle('open', open);
  button.setAttribute('aria-expanded', String(open));
}

function selectGroup(id, focus, reveal) {
  const group = state.project?.groups.find((entry) => entry.id === id);
  if (!group) return;
  state.selected = id;
  let ancestor = group.parent;
  while (ancestor) {
    state.expanded.add(ancestor);
    ancestor = state.project.groups.find((entry) => entry.id === ancestor)?.parent;
  }
  if (reveal && !focus) openDrawer('right', true);
  renderGroups(focus);
  renderProperties();
  canvas.focus(id);
}

function renderGroups(focus = false) {
  const tree = byId('groupTree');
  const groups = state.project?.groups || [];
  tree.hidden = !groups.length;
  byId('groupsEmpty').hidden = !!groups.length;
  const current = renderTree(tree, groups, state.selected, state.expanded, selectGroup, effective);
  if (focus && current) current.focus();
}

function renderProperties() {
  const group = state.project?.groups.find((entry) => entry.id === state.selected);
  renderInspector(byId('propertyPanel'), state.project, group, group ? effective(group) : {}, applyChanges);
  const form = byId('propertyPanel').querySelector('form');
  if (form && state.saving) for (const field of form.elements) field.disabled = true;
}

function renderToolbar() {
  const { project, saving, previewOk } = state;
  byId('undo').disabled = saving || state.cursor === 0;
  byId('redo').disabled = saving || state.cursor + 1 >= state.snapshots.length;
  byId('save').disabled = saving || !changed() || !previewOk;
  byId('openLibrary').disabled = saving;
  byId('figureTitle').textContent = project?.title || '未打开图件';
  byId('revision').textContent = project ? `版本 ${project.render_version}${changed() ? ' · 草稿' : ''}` : '';
}

function renderExports() {
  const menu = byId('exportMenu');
  menu.replaceChildren();
  const project = state.project;
  if (!project) { menu.append(element('p', '请先打开图件。', 'muted')); return; }
  menu.append(element('p', `导出已保存版本 ${project.render_version}`, 'muted'));
  if (project.presentation_note) menu.append(element('p', project.presentation_note, 'muted'));
  for (const format of ['svg', 'png', 'pdf']) {
    const row = element('div');
    const capability = project.format_capabilities[format];
    const button = element('button', format.toUpperCase());
    button.disabled = !capability || capability.state === 'unavailable';
    if (capability?.state === 'generatable') button.textContent = `生成 ${format.toUpperCase()}`;
    button.addEventListener('click', async () => {
      button.disabled = true;
      const epoch = state.epoch;
      const revision = project.render_version;
      try {
        const url = revisionUrl(project, `/export/${format}`);
        const response = await request(url, capability.state === 'generatable' ? { method: 'POST' } : {});
        if (Number(response.headers.get('X-Figure-Revision')) !== revision || !response.headers.get('X-Figure-SHA256')) {
          throw new Error('导出返回了无法确认的图件版本。');
        }
        const blobUrl = URL.createObjectURL(await response.blob());
        const link = element('a');
        link.href = blobUrl;
        link.download = `${project.title}-v${revision}.${format}`;
        document.body.append(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
        if (state.epoch === epoch) {
          capability.state = 'ready';
          renderExports();
          status(`已下载 ${format.toUpperCase()} · 版本 ${revision}。`);
        }
      } catch (error) {
        if (state.epoch === epoch) status(error.message, true);
      } finally {
        button.disabled = false;
      }
    });
    row.append(button);
    if (button.disabled && capability?.reason) row.append(element('small', capability.reason));
    menu.append(row);
  }
  const originals = Object.values(project.original_exports || {});
  if (originals.length) {
    menu.append(element('p', '未修改的原始文件', 'muted'));
    for (const original of originals) {
      if (!/^\/api\/v2\/files\/[a-f0-9]{32}\/download$/.test(original.download_url)) continue;
      const row = element('div');
      const link = element('a', original.filename);
      link.href = original.download_url;
      link.download = original.filename;
      row.append(link);
      menu.append(row);
    }
  }
}

async function loadAsset(project) {
  let error;
  for (const format of ['svg', 'png']) {
    if (project.format_capabilities[format]?.state !== 'ready') continue;
    try { return await imageAsset(await request(revisionUrl(project, `/preview/${format}`)), format); }
    catch (failure) { error = failure; }
  }
  throw error || new Error(project.format_capabilities.svg?.reason || project.read_only_reason || '图件没有可读取的 SVG 或 PNG 预览。');
}

function clearPreviewTimer() {
  if (state.timer !== null) clearTimeout(state.timer);
  state.timer = null;
}

function activate(project, asset, { preserveFocus = false } = {}) {
  clearPreviewTimer();
  state.draft += 1;
  state.epoch += 1;
  state.project = project;
  state.previewOk = true;
  state.snapshots = [[]];
  state.cursor = 0;
  if (!preserveFocus) state.expanded = new Set(project.groups.map((group) => group.id));
  if (!preserveFocus || !project.groups.some((group) => group.id === state.selected)) {
    state.selected = project.groups[0]?.id || null;
  }
  canvas.show(asset, project, !preserveFocus);
  canvas.focus(state.selected);
  renderGroups();
  renderProperties();
  renderExports();
  renderToolbar();
  status(project.read_only_reason || project.presentation_note || '请选择对象组。');
}

function applyChanges(entries) {
  if (state.saving) return;
  const before = new Map(commands().map((entry) => [commandKey(entry), entry]));
  for (const entry of entries) {
    const key = commandKey(entry);
    const initial = state.project.groups.find((group) => group.id === entry.group_id)?.values[entry.property];
    if (JSON.stringify(entry.value) === JSON.stringify(initial)) before.delete(key);
    else before.set(key, entry);
  }
  state.snapshots = [...state.snapshots.slice(0, state.cursor + 1), [...before.values()]];
  state.cursor += 1;
  updateDraft();
}

function updateDraft() {
  state.previewOk = false;
  state.draft += 1;
  renderGroups();
  renderProperties();
  renderToolbar();
  clearPreviewTimer();
  status(changed() ? '正在重绘草稿…' : '正在恢复已保存图件…');
  state.timer = setTimeout(() => {
    state.timer = null;
    preview(state.epoch, state.draft);
  }, 220);
}

async function preview(epoch, draft) {
  const current = () => epoch === state.epoch && draft === state.draft;
  if (state.flight) await state.flight;
  if (!current() || state.saving) return;
  const project = state.project;
  const batch = commands();
  const flight = (async () => {
    try {
      let result = project;
      let asset;
      if (batch.length) {
        const response = await request(editUrl(project, 'preview'), post({ expected_revision: project.render_version,
          draft_revision: draft, commands: batch }));
        result = await response.json();
        if (result.figure_id !== project.figure_id || result.render_version !== project.render_version || result.draft_revision !== draft) {
          throw new Error('草稿预览返回了无法确认的版本。');
        }
        asset = await imageAsset(new Response(new Blob([result.svg], { type: 'image/svg+xml' })), 'svg');
      } else asset = await loadAsset(project);
      if (!current()) { URL.revokeObjectURL(asset.url); return; }
      state.previewOk = true;
      canvas.show(asset, result);
      canvas.focus(state.selected);
      status(batch.length ? '草稿预览已更新。' : '已恢复保存的图件。');
    } catch (error) {
      if (current()) status(`预览失败，草稿仍在本页。${error.message}`, true);
    } finally {
      if (current()) renderToolbar();
    }
  })();
  state.flight = flight;
  await flight;
  if (state.flight === flight) state.flight = null;
}

async function save() {
  if (state.saving || !state.previewOk || !changed()) return;
  const project = state.project;
  state.saving = true;
  renderProperties();
  renderToolbar();
  status('正在保存图件…');
  try {
    const response = await request(editUrl(project, 'save'), post({ expected_revision: project.render_version,
      commands: commands() }));
    const saved = await response.json();
    if (saved.figure_id !== project.figure_id || saved.render_version !== project.render_version + 1) {
      throw new Error('保存回执版本无法确认，请从图件库重新打开。');
    }
    try { activate(saved, await loadAsset(saved), { preserveFocus: true }); }
    catch (error) {
      // The commit succeeded. Treat the confirmed revision as current even
      // when its preview was concurrently superseded or could not be loaded.
      state.project = saved;
          state.snapshots = [[]];
      state.cursor = 0;
      state.previewOk = false;
      byId('frame').hidden = true;
      byId('canvasEmpty').hidden = false;
      byId('canvasEmpty').textContent = '请重新打开查看保存的图件。';
      renderGroups();
      renderExports();
      status(`已保存版本 ${saved.render_version}。${error.message}`, true);
      return;
    }
    status(`已保存版本 ${saved.render_version}。`);
    refreshAssets();
  } catch (error) {
    status(`保存未确认，草稿仍在本页。${error.message}`, true);
  } finally {
    state.saving = false;
    renderToolbar();
    renderProperties();
  }
}

const dialog = byId('library');

for (const [button, side] of [['toggleGroups', 'left'], ['toggleInspector', 'right']]) {
  byId(button).addEventListener('click', () => openDrawer(side, byId(button).getAttribute('aria-expanded') !== 'true'));
}
for (const [id, offset] of [['undo', -1], ['redo', 1]]) {
  byId(id).addEventListener('click', () => {
    if (byId(id).disabled) return;
    state.cursor += offset;
    updateDraft();
  });
}
byId('save').addEventListener('click', save);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !dialog.open) {
    openDrawer('left', false);
    openDrawer('right', false);
  }
  if (event.key.toLowerCase() === 'z' && (event.ctrlKey || event.metaKey) && !dialog.open &&
      !['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) {
    const button = byId(event.shiftKey ? 'redo' : 'undo');
    if (!button.disabled) { event.preventDefault(); button.click(); }
  }
});
window.addEventListener('beforeunload', (event) => {
  if (changed()) { event.preventDefault(); event.returnValue = ''; }
});
renderToolbar();
renderExports();
const requested = starting.get('figure_id') || starting.get('project');
const openLibrary = initializeOpenDialog({ request, api, revisionUrl, loadAsset, activate, changed, requested,
  busy: () => state.saving,
  onOpened: () => {
    if (matchMedia('(max-width: 920px)').matches) openDrawer('left', true);
    byId('groupTree').querySelector('[tabindex="0"]')?.focus();
  },
});
const refreshAssets = initializeAssets({ request, api, revisionUrl, choose: openLibrary });
if (requested) openLibrary();
