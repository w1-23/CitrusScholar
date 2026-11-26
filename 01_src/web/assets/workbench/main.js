import { byId, element } from './dom.js';
import { renderInspector, renderTree } from './controls.js?v=4';
import { CanvasView, imageAsset } from './canvas.js?v=6';
import { hexDigest } from './sha256.js?v=1';
import { initializeLibrary } from './library.js?v=4';
import { legacyEditorUrl, sourceReturnUrl } from './navigation.js?v=1';
import { categoryValues, displayGroups } from './categories.js?v=1';

const api = '/api/v2/figure-workbench';
const state = { project: null, groups: [], selected: null, expanded: new Set(), snapshots: [[]], cursor: 0,
  epoch: 0, draft: 0, timer: null, flight: null, saving: false, previewOk: true, conflict: false };
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
const canvas = new CanvasView((id, reveal = true) => { showSideTab('objects'); selectGroup(id, false, reveal); },
  (id, value) => {
    const group = state.groups.find((entry) => entry.id === id);
    if (!group?.properties.includes('offset_px')) return false;
    return applyChanges([{ group_id: id, property: 'offset_px', value }]);
  });

async function request(url, init = {}) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init });
  if (response.ok) return response;
  const payload = await response.json().catch(() => ({}));
  const detail = payload.detail;
  const error = new Error(typeof detail === 'string' ? detail : detail?.message || `请求失败（${response.status}）`);
  error.status = response.status;
  error.code = payload.code;
  throw error;
}

function post(data) {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
}

function editUrl(project, operation) { return `${path(project.figure_id)}/${operation}`; }

function effective(group) {
  if (group.kind === 'category') return categoryValues(group, effective);
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

const sideTabs = [byId('libraryTab'), byId('objectsTab')];
function showSideTab(tab, focus = false) {
  for (const button of sideTabs) {
    const active = button.id === `${tab}Tab`;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    byId(button.getAttribute('aria-controls')).hidden = !active;
    if (active && focus) button.focus();
  }
}
sideTabs.forEach((button, index) => {
  button.addEventListener('click', () => showSideTab(index === 0 ? 'library' : 'objects'));
  button.addEventListener('keydown', (event) => {
    let next;
    if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = 1;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') next = 1 - index;
    else return;
    event.preventDefault();
    showSideTab(next === 0 ? 'library' : 'objects', true);
  });
});

function selectGroup(id, focus, reveal) {
  const group = state.groups.find((entry) => entry.id === id);
  if (!group) return;
  const changedGroup = state.selected !== id;
  state.selected = id;
  let ancestor = group.parent;
  while (ancestor) {
    state.expanded.add(ancestor);
    ancestor = state.groups.find((entry) => entry.id === ancestor)?.parent;
  }
  const showMobileProperties = reveal && matchMedia('(max-width: 920px)').matches;
  if (showMobileProperties) openDrawer('right', true);
  renderGroups(focus && !showMobileProperties);
  renderProperties();
  if (changedGroup || showMobileProperties) byId('inspector').scrollTop = 0;
  if (showMobileProperties) byId('inspectorTitle').focus({ preventScroll: true });
  canvas.focus(id);
}

function renderGroups(focus = false) {
  const tree = byId('groupTree');
  const groups = state.groups;
  tree.hidden = !groups.length;
  byId('groupsEmpty').hidden = !!groups.length;
  const current = renderTree(tree, groups, state.selected, state.expanded, selectGroup, effective);
  if (focus && current) current.focus();
}

function renderProperties() {
  const group = state.groups.find((entry) => entry.id === state.selected);
  renderInspector(byId('propertyPanel'), state.project, group, group ? effective(group) : {}, applyChanges);
  const form = byId('propertyPanel').querySelector('form');
  if (form && state.saving) for (const field of form.elements) field.disabled = true;
}

function renderToolbar() {
  const { project, saving, previewOk } = state;
  byId('undo').disabled = saving || state.cursor === 0;
  byId('redo').disabled = saving || state.cursor + 1 >= state.snapshots.length;
  byId('save').disabled = saving || !changed() || !previewOk || state.conflict;
  byId('recoverDraft').hidden = !state.conflict || !changed();
  byId('recoverDraft').disabled = saving;
  byId('openLibrary').disabled = saving;
  byId('figureTitle').textContent = project?.title || '未打开图件';
  byId('revision').textContent = project ? `版本 ${project.render_version}${changed() ? ' · 草稿' : ''}` : '';
  const editor = byId('legacyEditor');
  const legacy = legacyEditorUrl(project);
  editor.hidden = !legacy;
  if (legacy) editor.href = legacy;
  else editor.removeAttribute('href');
  const back = byId('returnSession');
  const source = sourceReturnUrl(project);
  byId('returnFiles').hidden = !source;
  back.href = source || '/?files=1';
  back.textContent = source ? '← 返回来源会话' : '← 返回资料库';
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
        const digest = response.headers.get('X-Figure-SHA256');
        if (Number(response.headers.get('X-Figure-Revision')) !== revision || !/^[a-f0-9]{64}$/.test(digest || '')) {
          throw new Error('导出返回了无法确认的图件版本。');
        }
        const blob = await response.blob();
        if (await hexDigest(await blob.arrayBuffer()) !== digest) throw new Error('导出内容与保存版本的摘要不一致。');
        const blobUrl = URL.createObjectURL(blob);
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
    try {
      const response = await request(revisionUrl(project, `/preview/${format}`));
      const mime = format === 'svg' ? 'image/svg+xml' : 'image/png';
      if (response.headers.get('X-Figure-Revision') !== String(project.render_version) ||
          !/^[a-f0-9]{64}$/.test(response.headers.get('X-Figure-SHA256') || '') ||
          response.headers.get('Content-Type')?.split(';')[0].trim() !== mime) {
        throw new Error('无法确认预览所对应的图件版本。');
      }
      const digest = response.headers.get('X-Figure-SHA256');
      if (format === 'svg' && project.object_contract?.renderKey && project.object_contract.renderKey !== digest) {
        throw new Error('预览与对象清单的渲染摘要不一致。');
      }
      const asset = await imageAsset(response, format, digest);
      if (error) asset.warning = `${error.message} 已显示此版本的 ${format.toUpperCase()}。`;
      return asset;
    } catch (failure) { error = failure; }
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
  state.groups = displayGroups(project.groups);
  syncLocation(project);
  state.previewOk = true;
  state.conflict = false;
  state.snapshots = [[]];
  state.cursor = 0;
  if (!preserveFocus) state.expanded = new Set();
  if (!preserveFocus || !state.groups.some((group) => group.id === state.selected)) {
    state.selected = null;
  }
  canvas.show(asset, project, !preserveFocus);
  canvas.focus(state.selected);
  renderGroups();
  renderProperties();
  renderExports();
  renderToolbar();
  status(asset.warning || project.read_only_reason || project.presentation_note || '请选择对象或对象组。', !!asset.warning);
}

function applyChanges(entries) {
  if (state.saving) return false;
  const before = new Map(commands().map((entry) => [commandKey(entry), entry]));
  for (const entry of entries) {
    const key = commandKey(entry);
    const initial = state.project.groups.find((group) => group.id === entry.group_id)?.values[entry.property];
    if (JSON.stringify(entry.value) === JSON.stringify(initial ?? (entry.property === 'offset_px' ? [0, 0] : undefined))) before.delete(key);
    else before.set(key, entry);
  }
  if (before.size > 200) { status('最多可以提交 200 项修改。', true); return false; }
  const next = [...before.values()];
  if (JSON.stringify(next) === JSON.stringify(commands())) return false;
  state.snapshots = [...state.snapshots.slice(0, state.cursor + 1), next];
  state.cursor += 1;
  updateDraft();
  return true;
}

function updateDraft() {
  canvas.finishDrag(null, true);
  state.previewOk = false;
  state.draft += 1;
  canvas.setOffsets(commands(), state.project);
  renderGroups();
  renderProperties();
  renderToolbar();
  clearPreviewTimer();
  if (state.conflict) {
    status('图件已有更新。载入最新版以恢复本页草稿。', true);
    return;
  }
  status(changed() ? '正在重绘草稿…' : '正在恢复已保存图件…');
  state.timer = setTimeout(() => {
    state.timer = null;
    preview(state.epoch, state.draft);
  }, 150);
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
          source_sha256: project.source_sha256, draft_revision: draft, commands: batch }));
        result = await response.json();
        if (result.figure_id !== project.figure_id || result.render_version !== project.render_version ||
            result.source_sha256 !== project.source_sha256 || result.draft_revision !== draft ||
            result.object_contract?.draftRevision !== draft || result.object_contract.revision !== project.render_version ||
            !/^[a-f0-9]{64}$/.test(result.object_contract.renderKey || '')) {
          throw new Error('草稿预览返回了无法确认的版本。');
        }
        asset = await imageAsset(new Response(new Blob([result.svg], { type: 'image/svg+xml' })), 'svg', result.object_contract.renderKey);
      } else asset = await loadAsset(project);
      await canvas.idle;
      if (!current()) { URL.revokeObjectURL(asset.url); return; }
      state.previewOk = true;
      canvas.show(asset, result);
      canvas.focus(state.selected);
      status(batch.length ? '草稿预览已更新。' : '已恢复保存的图件。');
    } catch (error) {
      if (current()) {
        state.conflict = error.code === 'revision_conflict' || error.code === 'source_conflict';
        status(`预览失败，草稿仍在本页。${error.message}`, true);
      }
    } finally {
      if (current()) renderToolbar();
    }
  })();
  state.flight = flight;
  await flight;
  if (state.flight === flight) state.flight = null;
}

async function save() {
  if (state.saving || state.conflict || !state.previewOk || !changed()) return;
  const project = state.project;
  state.saving = true;
  renderProperties();
  renderToolbar();
  status('正在保存图件…');
  try {
    const response = await request(editUrl(project, 'save'), post({ expected_revision: project.render_version,
      source_sha256: project.source_sha256, draft_revision: state.draft, commands: commands() }));
    const saved = await response.json();
    if (saved.figure_id !== project.figure_id || saved.source_sha256 !== project.source_sha256 ||
        saved.render_version !== project.render_version + 1) {
      throw new Error('保存回执版本无法确认，请从图件库重新打开。');
    }
    try { activate(saved, await loadAsset(saved), { preserveFocus: true }); }
    catch (error) {
      // The commit succeeded. Treat the confirmed revision as current even
      // when its preview was concurrently superseded or could not be loaded.
      state.project = saved;
      state.groups = displayGroups(saved.groups);
      syncLocation(saved);
      state.snapshots = [[]];
      state.cursor = 0;
      state.previewOk = false;
      byId('canvasSheet').hidden = true;
      byId('canvasEmpty').hidden = false;
      byId('canvasEmpty').querySelector('span').textContent = '请重新打开查看保存的图件。';
      renderGroups();
      renderExports();
      status(`已保存版本 ${saved.render_version}。${error.message}`, true);
      library.refresh();
      return;
    }
    status(`已保存版本 ${saved.render_version}。`);
    library.refresh();
  } catch (error) {
    state.conflict = error.code === 'revision_conflict' || error.code === 'source_conflict';
    status(`保存未确认，草稿仍在本页。${error.message}`, true);
  } finally {
    state.saving = false;
    renderToolbar();
    renderProperties();
  }
}

async function recoverDraft() {
  if (!state.conflict || !changed() || state.saving) return;
  const previous = state.project;
  const draft = commands();
  state.saving = true;
  renderToolbar();
  renderProperties();
  try {
    const current = await (await request(path(previous.figure_id))).json();
    const groups = new Map(current.groups.map((group) => [group.id, group]));
    if (current.figure_id !== previous.figure_id || current.render_version <= previous.render_version ||
        current.source_sha256 !== previous.source_sha256 ||
        draft.some(({ group_id: id, property }) => !groups.get(id)?.properties.includes(property))) {
      throw new Error('最新版的来源或对象不再匹配，草稿保留在本页。');
    }
    const asset = await loadAsset(current);
    activate(current, asset, { preserveFocus: true });
  } catch (error) {
    status(error.message, true);
    return;
  } finally {
    state.saving = false;
    renderToolbar();
    renderProperties();
  }
  if (applyChanges(draft)) status('已恢复草稿，请核对在最新版上的修改。');
  else status('最新版已包含草稿中的修改。');
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
byId('recoverDraft').addEventListener('click', recoverDraft);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !event.defaultPrevented && !dialog.open) {
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
function syncLocation(project) {
  const url = new URL(location.href);
  url.searchParams.delete('figure_id');
  url.searchParams.set('project', project.figure_id);
  url.searchParams.set('revision', project.render_version);
  history.replaceState(history.state, '', url);
}

const library = initializeLibrary({ request, api, revisionUrl, loadAsset, activate, changed,
  busy: () => state.saving,
  switchToken: () => `${state.epoch}:${state.draft}`,
  setStatus: status,
  onOpened: () => {
    showSideTab('objects');
    if (matchMedia('(max-width: 920px)').matches) openDrawer('left', false);
    byId('canvas').focus({ preventScroll: true });
  },
});
const requested = starting.get('figure_id') ?? starting.get('project');
if (requested !== null) {
  const revision = starting.get('revision');
  library.open({ figure_id: requested, render_version: revision === null ? undefined : /^\d+$/.test(revision) ? Number(revision) : NaN });
}
