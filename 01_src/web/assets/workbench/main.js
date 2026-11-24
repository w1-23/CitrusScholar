import { byId, element, renderInspector, renderTree } from './controls.js';
import { CanvasView, imageAsset } from './canvas.js';
import { initializeAssets, thumbnail } from './library.js';

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
  status(project.read_only_reason || '请选择对象组。');
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

const library = { epoch: 0, offset: 0, candidate: null, pending: false };
const dialog = byId('library');
const libraryNotice = byId('libraryNotice');
const confirmOpen = byId('confirmOpen');

function libraryStatus(message, error = false) {
  libraryNotice.textContent = message;
  libraryNotice.classList.toggle('warning', error);
}

function chooseAsset(project) {
  library.candidate = project;
  confirmOpen.disabled = library.pending;
  libraryStatus(changed() ? '打开成功后将放弃当前未保存的草稿。' : '确认打开所选图件。');
}

async function refreshLibrary(more = false) {
  const epoch = library.epoch;
  byId('moreFigures').hidden = true;
  byId('retryLibrary').hidden = true;
  libraryStatus('正在读取图件库…');
  if (!more) {
    library.offset = 0;
    library.candidate = null;
    confirmOpen.disabled = true;
    byId('libraryList').replaceChildren();
  }
  try {
    const response = await request(`${api}/projects?limit=24&offset=${library.offset}`);
    const { projects, has_more: hasMore } = await response.json();
    if (epoch !== library.epoch || !dialog.open) return;
    for (const project of projects) {
      const row = element('label');
      const radio = element('input');
      radio.type = 'radio';
      radio.name = 'figure';
      radio.value = project.figure_id;
      radio.addEventListener('change', () => chooseAsset(project));
      row.append(radio);
      const preview = thumbnail(project, revisionUrl);
      if (preview) row.append(preview);
      row.append(element('span', project.title || '未命名图件'), element('small', `版本 ${project.render_version}`));
      byId('libraryList').append(row);
      const requested = starting.get('figure_id') || starting.get('project');
      if (!library.candidate && requested === project.figure_id) {
        radio.checked = true;
        chooseAsset(project);
      }
    }
    library.offset += projects.length;
    byId('moreFigures').hidden = !hasMore;
    if (!library.candidate) libraryStatus(library.offset ? '选择一张图件。' : '图件库中没有可打开的图件。');
  } catch (error) {
    if (epoch !== library.epoch || !dialog.open) return;
    libraryStatus(error.message, true);
    byId('retryLibrary').hidden = false;
  }
}

async function openCandidate() {
  if (!library.candidate || library.pending) return;
  const { candidate, epoch } = library;
  library.pending = true;
  confirmOpen.disabled = true;
  byId('libraryItems').disabled = true;
  libraryStatus('正在打开所选图件…');
  try {
    const project = await (await request(revisionUrl(candidate))).json();
    if (project.figure_id !== candidate.figure_id || project.render_version !== candidate.render_version) {
      throw new Error('图件版本已更新，请重试加载图件库。');
    }
    const asset = await loadAsset(project);
    if (epoch !== library.epoch || !dialog.open) { URL.revokeObjectURL(asset.url); return; }
    activate(project, asset);
    dialog.close();
    if (matchMedia('(max-width: 920px)').matches) openDrawer('left', true);
    byId('groupTree').querySelector('[tabindex="0"]')?.focus();
  } catch (error) {
    if (epoch !== library.epoch || !dialog.open) return;
    libraryStatus(error.message, true);
    byId('retryLibrary').hidden = error.status !== 409;
  } finally {
    if (epoch === library.epoch) {
      library.pending = false;
      confirmOpen.disabled = false;
      byId('libraryItems').disabled = false;
    }
  }
}

function openLibrary(preset = null) {
  if (state.saving || dialog.open) return;
  library.epoch += 1;
  library.pending = false;
  byId('libraryItems').disabled = false;
  const single = byId('selectedAsset');
  single.hidden = !preset;
  single.replaceChildren();
  byId('libraryItems').hidden = !!preset;
  byId('moreFigures').hidden = true;
  byId('retryLibrary').hidden = true;
  dialog.showModal();
  if (!preset) { refreshLibrary(); return; }
  const preview = thumbnail(preset, revisionUrl);
  if (preview) single.append(preview);
  single.append(element('strong', preset.title), element('span', `版本 ${preset.render_version}`, 'muted'));
  chooseAsset(preset);
  confirmOpen.focus();
}

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
byId('openLibrary').addEventListener('click', () => openLibrary());
byId('save').addEventListener('click', save);
byId('moreFigures').addEventListener('click', () => refreshLibrary(true));
byId('retryLibrary').addEventListener('click', () => {
  byId('libraryItems').hidden = false;
  byId('selectedAsset').hidden = true;
  refreshLibrary();
});
confirmOpen.addEventListener('click', openCandidate);
byId('cancelOpen').addEventListener('click', () => dialog.close());
dialog.addEventListener('close', () => { library.epoch += 1; });
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
const refreshAssets = initializeAssets({ request, api, revisionUrl, choose: openLibrary });
if (starting.has('figure_id') || starting.has('project')) openLibrary();
