// SPDX-License-Identifier: AGPL-3.0-only
import {api, safeReturn, importSource, previewPoint, exportOptions} from './api.mjs';
import {$, inspector, listAssets, listFigures, listHistory, listObjects, make} from './views.mjs';

const state = {session:null, figures:[], versions:[], doc:null, head:null, selected:null, group:null, locks:new Set(), point:null, candidates:[], zoom:1, pending:false, asset:null, restore:null};
const known = (kind, id) => {
  const objects = kind === 'group' ? state.doc?.native_manifest?.groups : kind === 'native' ? state.doc?.native_manifest?.elements : state.doc?.objects;
  return objects?.some(item => (item.gid ?? item.id) === id);
};
const latest = () => state.head === state.doc?.revision;
const message = (text, error = false) => { $('status').textContent = text; $('status').dataset.error = error; };
const switchPage = id => {
  for (const button of document.querySelectorAll('[data-page]')) {
    const active = button.dataset.page === id;
    button.setAttribute('aria-pressed', active);
    $(button.dataset.page).hidden = !active;
  }
};
function buttons() {
  const doc = state.doc;
  const disabled = state.pending || !state.session;
  for (const id of ['import-open','filter']) $(id).disabled = disabled;
  for (const id of ['zoom-in','zoom-out','zoom-reset']) $(id).disabled = disabled || !doc;
  $('export-open').disabled = disabled || !doc || !latest();
  $('cycle').disabled = disabled || !state.point || state.candidates.length < 2;
  $('refresh').disabled = disabled || !doc;
  $('signin').hidden = !!state.session;
  $('signin').disabled = state.pending;
  $('studio').setAttribute('aria-busy', String(state.pending));
}

function clearSession() {
  Object.assign(state, {session:null, figures:[], versions:[], doc:null, head:null, selected:null, group:null});
  state.locks.clear();
  clearPick();
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  document.querySelector('#login-form [name=token]').value = '';
  $('return').hidden = true;
  $('return').removeAttribute('href');
  listAssets([], new Set(), openImport);
  if (!state.session) $('assets').textContent = '登录后显示授权资料。';
  $('figures').textContent = '登录后显示你的图件。';
  refreshViews();
}

function formMessage(form, text) {
  if (!form) return;
  let el = form.querySelector('.form-error');
  if (!el) { el = make('p', '', 'form-error'); el.setAttribute('role','alert'); form.append(el); }
  el.textContent = text;
  el.hidden = !text;
}

async function run(callback, {busy = '正在读取工作台…', form, done = ''} = {}) {
  if (state.pending) return;
  state.pending = true;
  buttons();
  const submit = form?.querySelector('[type=submit]');
  if (submit) submit.disabled = true;
  if (form) formMessage(form, '');
  message(busy);
  try { await callback(); message((typeof done === 'function' ? done() : done) || (state.session ? '工作台已就绪。' : '请通过会话打开工作台，或使用独立模式令牌。')); }
  catch (error) {
    let text = error.message || '操作未完成。';
    if (error.status === 401) {
      if (form?.id !== 'login-form') clearSession();
      text = form?.id === 'login-form' ? '访问令牌无效，或独立模式未开启。' : '工作台会话未建立或已失效。请从会话重新打开，或使用独立模式令牌。';
    }
    if (error.status === 409) text += ' 可用画布上方的“刷新”读取最新版本，再重新应用所需编辑。';
    formMessage(form, text);
    message(text, true);
  } finally {
    state.pending = false;
    if (submit) submit.disabled = false;
    buttons();
  }
}

function clearPick() { state.point = null; state.candidates = []; }
function paintSelection() {
  const box = $('outline');
  const elements = state.doc?.native_manifest?.elements;
  const select = state.selected;
  const group = select?.kind === 'group' && state.doc?.native_manifest?.groups.find(item => item.gid === select.id);
  const ids = new Set(group ? group.members : select?.kind === 'native' && select.id !== 'figure' ? [select.id] : []);
  const bounds = (elements ?? []).filter(el => ids.has(el.gid)).map(el => el.bbox).filter(b => b?.length === 4 && b.every(Number.isFinite));
  box.hidden = !bounds.length;
  if (!bounds.length) return;
  const clamp = n => Math.max(0, Math.min(1, n));
  const x = clamp(Math.min(...bounds.map(b => b[0]))), y = clamp(Math.min(...bounds.map(b => b[1])));
  const w = clamp(Math.max(...bounds.map(b => b[0] + b[2]))) - x, h = clamp(Math.max(...bounds.map(b => b[1] + b[3]))) - y;
  for (const [key, value] of Object.entries({left:x, top:y, width:w, height:h})) box.style.setProperty(key, `${value * 100}%`);
}

const renderInspector = () => inspector(state.doc, state.selected, state.group, !latest(), state.locks, {
  select, lock: (id, locked) => {
    if (state.pending) return;
    if (locked) state.locks.add(id); else state.locks.delete(id);
    clearPick();
    listObjects(state.doc, state.selected, state.locks, select);
    buttons();
  },
  nativeSave: (patches, form) => {
    if (!patches.length) { formMessage(form, '没有可保存的修改。'); return; }
    return mutate(expected_revision => api.patch(state.doc.figure_id, true, {expected_revision, patches}), form);
  },
  textSave: (overrides, form) => mutate(expected_revision => api.patch(state.doc.figure_id, false, {expected_revision, overrides}), form),
  transform: (gid, kind, values, form) => mutate(expected_revision => api.transform(state.doc.figure_id, gid, kind, {expected_revision, locked_gids:[...state.locks], ...values}), form),
});

function renderSelection(focus = false) {
  listObjects(state.doc, state.selected, state.locks, select);
  renderInspector();
  paintSelection();
  if (focus) $('inspector-title').focus();
}

function refreshViews() {
  const doc = state.doc;
  $('revision').hidden = !doc;
  $('revision').textContent = doc ? `${latest() ? '' : '历史 · '}版本 ${doc.revision}` : '';
  $('canvas-title').textContent = doc?.name ?? '画布预览';
  $('empty').hidden = !!doc;
  $('paper').hidden = !doc;
  const image = $('preview');
  if (doc) {
    image.alt = `${doc.name}，版本 ${doc.revision}`;
    const preview = api.preview(doc.figure_id, doc.revision);
    if (image.src !== preview) { $('paper').style.removeProperty('width'); image.src = preview; }
  } else image.removeAttribute('src');
  image.dataset.pick = !!doc?.native_manifest;
  $('canvas-note').textContent = !doc ? '打开图件后可缩放。' : doc.native_manifest ? '单击拾取，Alt + 单击或按 R 轮选重叠对象。键盘也可从对象列表选取。' : '此图件不支持画布精确拾取。可通过对象列表查看公开属性。';
  if (doc) $('canvas-note').textContent += ' 画布上按 ＋ / − 缩放，按 0 重置。';
  $('zoom-reset').textContent = state.zoom === 1 ? '适合' : `${Math.round(state.zoom * 100)}%`;
  sizePaper();
  if (state.session) listFigures(state.figures, doc?.figure_id, id => openFigure(id));
  listHistory(state.versions, doc, state.head, {view:rev => openFigure(doc.figure_id, rev), restore:openRestore});
  renderSelection();
  buttons();
}

async function refreshList() { state.figures = (await api.list()).figures; }
async function refreshLibrary() {
  const authorized = new Set((state.session.source_assets ?? []).map(ref => ref.startsWith('library:') ? ref : `library:${ref}`));
  $('assets').textContent = '正在读取授权资料…';
  try { listAssets((await api.library($('filter').value)).assets, authorized, openImport); }
  catch (error) { $('assets').textContent = '无法读取授权资料。'; throw error; }
}

async function initialize() {
  state.session = await api.session();
  const url = safeReturn(state.session.return_to, location.origin);
  $('return').hidden = !url;
  if (url) $('return').href = url;
  await refreshList();
  refreshViews();
  await refreshLibrary();
}

async function readGroup(id) {
  const {figure_id, revision} = state.doc;
  const [{group}, resolution] = await Promise.all([
    api.group(figure_id, id, revision), api.resolve(figure_id, {revision, gids:[id], locked_gids:[...state.locks]}),
  ]);
  return {group, resolution};
}

async function readFigure(id, revision) {
  const doc = await api.doc(id, revision);
  const [{versions}, objects] = await Promise.all([api.history(id), api.objects(id, doc.revision)]);
  if (state.doc?.figure_id !== id) {
    state.selected = null;
    state.locks.clear();
    state.zoom = 1;
  }
  Object.assign(state, {doc:{...doc, ...objects}, versions, head:versions[0]?.revision ?? doc.revision, group:null});
  if (state.selected && !known(state.selected.kind, state.selected.id)) state.selected = null;
  clearPick();
  if (state.selected?.kind === 'group') {
    try { state.group = await readGroup(state.selected.id); }
    catch (error) { state.selected = null; refreshViews(); throw error; }
  }
  refreshViews();
}

function openFigure(id, revision) {
  return run(async () => {
    await readFigure(id, revision);
    $('canvas-title').focus();
  });
}

function select(kind, id) {
  return run(async () => {
    if (!known(kind,id)) return;
    const group = kind === 'group' ? await readGroup(id) : null;
    Object.assign(state, {selected:{kind,id}, group});
    clearPick();
    renderSelection(true);
  });
}

function pick(point, cycle = false) {
  if (!point || !state.doc?.native_manifest) return;
  return run(async () => {
    const result = await api.pick(state.doc.figure_id, {revision:state.doc.revision, ...point, locked_gids:[...state.locks], ...(cycle && state.selected?.kind === 'native' ? {current_gid:state.selected.id} : {})});
    Object.assign(state, {point, candidates:result.candidate_gids, group:null, selected:result.picked_gid && known('native',result.picked_gid) ? {kind:'native',id:result.picked_gid} : null});
    switchPage('objects');
    renderSelection();
  }, {done:() => state.selected ? `已选取 ${state.selected.id}，此处有 ${state.candidates.length} 个候选对象。` : '此处没有可拾取对象。'});
}

function mutate(action, form, allowHistorical = false, done = '修改已保存为新版本。') {
  if (!state.doc || (!allowHistorical && !latest())) return;
  return run(async () => {
    const result = await action(state.head);
    if (form?.closest('dialog')) form.closest('dialog').close();
    // The mutation has already committed. Refresh only by reading its returned revision.
    try { await refreshList(); await readFigure(result.figure_id, result.revision); }
    catch (error) {
      if (error.status === 401) throw error;
      throw new Error(`服务器已写入版本 ${result.revision}，但刷新失败。请用“刷新”重新打开。`);
    }
    $('inspector-title').focus({preventScroll:true});
  }, {form, busy:'正在保存并重新渲染图件…', done});
}

function openImport(asset = null) {
  if (!state.session || state.pending) return;
  const form = $('import-form');
  form.reset();
  formMessage(form, '');
  state.asset = asset;
  $('file').required = !asset;
  $('file-label').hidden = !!asset;
  $('import-asset').hidden = !asset;
  $('import-asset').replaceChildren();
  if (asset) {
    $('import-asset').textContent = `已授权资料 · ${asset.name}`;
    if (/^(png|svg|jpe?g|pdf)$/i.test(asset.format)) {
      const preview = make('a', ' 在新标签中预览');
      Object.assign(preview, {href:api.assetPreview(asset.asset_ref), target:'_blank', rel:'noopener noreferrer'});
      $('import-asset').append(preview);
    }
  }
  $('import-dialog').showModal();
}

function openRestore(revision) {
  if (!state.doc || state.pending) return;
  state.restore = revision;
  $('restore-description').textContent = `恢复版本 ${revision} 会创建新版本，不会删除现有历史。`;
  formMessage($('restore-form'), '');
  $('restore-dialog').showModal();
}

function sizePaper() {
  const image = $('preview');
  const stage = $('stage');
  if (!state.doc || !image.naturalWidth || !image.naturalHeight) return;
  const availableWidth = Math.max(1, stage.clientWidth - 32);
  const availableHeight = Math.max(1, stage.clientHeight - 32);
  const fitWidth = Math.min(availableWidth, availableHeight * image.naturalWidth / image.naturalHeight);
  $('paper').style.width = `${Math.floor(fitWidth * state.zoom)}px`;
}

function setZoom(factor) {
  if (!state.doc || state.pending) return;
  state.zoom = factor ? Math.max(.5, Math.min(3, state.zoom * factor)) : 1;
  sizePaper();
  $('zoom-reset').textContent = state.zoom === 1 ? '适合' : `${Math.round(state.zoom * 100)}%`;
}

for (const button of document.querySelectorAll('[data-page]')) button.addEventListener('click', () => switchPage(button.dataset.page));
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => { if (!state.pending) button.closest('dialog').close(); });
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel', event => { if (state.pending) event.preventDefault(); });
$('import-open').addEventListener('click', () => openImport());
$('signin').addEventListener('click', () => $('login-dialog').showModal());
$('login-dialog').addEventListener('close', () => { $('login-form').reset(); formMessage($('login-form'), ''); });
$('filter').addEventListener('change', () => run(refreshLibrary));
$('refresh').addEventListener('click', () => { if (state.doc) run(async () => { await refreshList(); await readFigure(state.doc.figure_id); }); });
$('zoom-in').addEventListener('click', () => setZoom(1.25));
$('zoom-out').addEventListener('click', () => setZoom(.8));
$('zoom-reset').addEventListener('click', () => setZoom(0));
$('cycle').addEventListener('click', () => pick(state.point, true));
$('preview').addEventListener('click', event => {
  const image = event.currentTarget;
  if (image.complete && image.naturalWidth) pick(previewPoint(image.getBoundingClientRect(), event.clientX, event.clientY), event.altKey);
});
new ResizeObserver(sizePaper).observe($('stage'));
$('preview').addEventListener('load', sizePaper);
$('preview').addEventListener('error', () => { if (state.doc) message('无法载入该版本的图件预览，请刷新。', true); });
$('stage').addEventListener('keydown', event => {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const action = {'+':() => setZoom(1.25),'=':() => setZoom(1.25),'-':() => setZoom(.8),'0':() => setZoom(0),'r':() => { if (state.candidates.length > 1) pick(state.point,true); }}[event.key.toLowerCase()];
  if (action) { event.preventDefault(); action(); }
});
$('export-open').addEventListener('click', () => { formMessage($('export-form'), ''); $('export-dialog').showModal(); });
$('login-form').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.currentTarget;
  run(async () => {
    try { await api.login(new FormData(form).get('token')); }
    finally { form.reset(); }
    $('login-dialog').close();
    await initialize();
  }, {form});
});
$('import-form').addEventListener('submit', event => {
  event.preventDefault();
  run(async () => {
    const file = $('file').files[0];
    const format = state.asset?.format ?? file?.name.split('.').pop();
    const source = importSource(format, state.asset?.asset_ref);
    if (!state.asset) {
      if (!file || file.size > 12 * 1024 * 1024) throw new Error('请选择不超过 12 MiB 的单个文件。');
      if (source.kind === 'script' && file.size > 512 * 1024) throw new Error('脚本不能超过 512 KiB。');
      source.entrypoint_ref = (await api.upload(file)).asset_ref;
    }
    const result = await api.importFigure(source, $('parse').value);
    $('import-dialog').close();
    await refreshList();
    await readFigure(result.figure_id);
    $('canvas-title').focus();
  }, {form:event.currentTarget, busy:'正在隔离渲染并导入…', done:'图件已导入。'});
});
$('restore-form').addEventListener('submit', event => {
  event.preventDefault();
  mutate(expected_revision => api.restore(state.doc.figure_id, {expected_revision, revision:state.restore}), event.currentTarget, true, '已恢复为新版本。');
});
$('export-form').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = exportOptions(new FormData(form));
  run(async () => {
    const response = await api.export(state.doc.figure_id, data);
    const url = URL.createObjectURL(await response.blob());
    const a = make('a');
    a.href = url;
    a.download = `${state.doc.name.replace(/\.[^.]+$/, '') || 'figure'}.${data.format}`;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
    $('export-dialog').close();
    const revision = response.headers.get('X-Figure-Revision');
    $('canvas-note').textContent = `下载版本 ${revision} · ${response.headers.get('X-Export-Id')} · SHA-256 ${response.headers.get('X-Content-SHA256')}`;
  }, {form, busy:'正在生成下载文件…', done:'图件已生成并下载。'});
});
refreshViews();
run(initialize);
