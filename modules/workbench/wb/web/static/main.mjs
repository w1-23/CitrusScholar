// SPDX-License-Identifier: AGPL-3.0-only
import {api, safeReturn, exportOptions} from './api.mjs?v=2';
import {createAssistant} from './assistant-ui.mjs';
import {createCanvas} from './canvas-ui.mjs?v=2';
import {createResources} from './resources.mjs?v=3';
import {push} from './tavotto-primitives.mjs';
import {$, inspector, listFigures, listHistory, listObjects, make} from './views.mjs?v=2';

const state = {session:null, figures:[], versions:[], doc:null, head:null, selected:null, group:null, locks:new Set(), point:null, candidates:[], zoom:1, pending:false, restore:null, nameRevision:null, past:[], future:[]};
const known = (kind, id) => {
  const objects = kind === 'group' ? state.doc?.native_manifest?.groups : kind === 'native' ? state.doc?.native_manifest?.elements : state.doc?.objects;
  return objects?.some(item => (item.gid ?? item.id) === id);
};
const latest = () => state.head === state.doc?.revision;
const frozenOffer = () => /^[a-f0-9]{32}$/.test(state.session?.frozen_project ?? '') ? state.session.frozen_project : '';
const message = (text, error = false) => { $('status').textContent = text; $('status').dataset.error = error; };
const switchPage = id => {
  for (const button of document.querySelectorAll('[data-page]')) {
    const active = button.dataset.page === id;
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
    $(button.dataset.page).hidden = !active;
  }
};
function buttons() {
  const doc = state.doc;
  const disabled = state.pending || !state.session;
  const workerOnly = state.session?.worker_only === true;
  for (const id of ['import-open','empty-prepare','static-open']) {
    $(id).hidden = workerOnly;
    $(id).disabled = disabled || workerOnly;
  }
  $('library').hidden = workerOnly;
  $('worker-source-note').hidden = !workerOnly || !!frozenOffer();
  $('empty-source-note').textContent = workerOnly
    ? '打开左侧已导入图件，或返回聊天，从图件卡接手 Worker 图件。'
    : '从左侧打开图件或选择已渲染的预览。也可准备 Python 或 R，或打开静态图。';
  if (workerOnly) for (const id of ['import-dialog','card-dialog']) if ($(id).open) $(id).close();
  $('undo').disabled = disabled || !latest() || !state.past.length || doc?.editability === 'preview_only';
  $('redo').disabled = disabled || !latest() || !state.future.length || doc?.editability === 'preview_only';
  canvas.buttons(disabled);
  $('object-search').disabled = !doc;
  for (const id of ['zoom-in','zoom-out','zoom-reset']) $(id).disabled = disabled || !doc;
  $('export-open').disabled = disabled || !doc || !latest();
  $('cycle').disabled = disabled || !state.point || state.candidates.length < 2;
  $('refresh').disabled = disabled;
  $('frozen-import').hidden = !frozenOffer();
  $('frozen-import').disabled = disabled || !frozenOffer();
  $('signin').hidden = !!state.session;
  $('signin').disabled = state.pending;
  $('studio').setAttribute('aria-busy', String(state.pending));
}

function clearSession() {
  Object.assign(state, {session:null, figures:[], versions:[], doc:null, head:null, selected:null, group:null, past:[], future:[]});
  state.locks.clear();
  canvas.closeInline();
  clearPick();
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  document.querySelector('#login-form [name=token]').value = '';
  $('return').hidden = true;
  $('return').removeAttribute('href');
  resources.clear();
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
const renderInspector = () => inspector(state.doc, state.selected, state.group, !latest(), state.locks, {
  select, lock: (id, locked) => {
    if (state.pending) return;
    if (locked) state.locks.add(id); else state.locks.delete(id);
    clearPick();
    renderSelection();
    buttons();
  },
  nativeSave: (patches, form) => {
    if (!patches.length) { formMessage(form, '没有可保存的修改。'); return; }
    return saveNative(patches,form);
  },
  textSave: (overrides, form) => mutate(expected_revision => api.patch(state.doc.figure_id, false, {expected_revision, overrides}), form),
  transform: (gid, kind, values, form) => mutate(expected_revision => api.transform(state.doc.figure_id, gid, kind, {expected_revision, locked_gids:[...state.locks], ...values}), form),
});

function renderSelection() {
  listObjects(state.doc, state.selected, state.locks, select);
  renderInspector();
  assistant.render();
  canvas.selection();
  buttons();
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
  $('canvas-note').textContent = !doc ? '打开图件后可缩放。' : doc.native_manifest ? '单击拾取 · 双击编辑可写文字 · 拖动具有位置能力的选中对象 · Alt + 单击轮选' : '此图件不支持画布精确拾取。可通过对象列表查看公开属性。';
  if (doc) $('canvas-note').textContent += ' 画布上按 ＋ / − 缩放，按 0 重置。';
  $('zoom-reset').textContent = state.zoom === 1 ? '适合' : `${Math.round(state.zoom * 100)}%`;
  canvas.size();
  canvas.properties();
  if (state.session) listFigures(state.figures, doc?.figure_id, id => openFigure(id));
  listHistory(state.versions, doc, state.head, {view:rev => openFigure(doc.figure_id, rev), restore:openRestore, name:openRename});
  renderSelection();
  buttons();
}

async function refreshList() { state.figures = (await api.list()).figures; }
async function initialize() {
  state.session = await api.session();
  const url = safeReturn(state.session.return_to, location.origin);
  $('return').hidden = !url;
  if (url) $('return').href = url;
  await refreshList();
  refreshViews();
  await resources.refresh();
}

async function readGroup(id) {
  const {figure_id, revision} = state.doc;
  const [{group}, resolution] = await Promise.all([
    api.group(figure_id, id, revision), api.resolve(figure_id, {revision, gids:[id], locked_gids:[...state.locks]}),
  ]);
  return {group, resolution};
}

async function readFigure(id, revision, historyMode = null) {
  const doc = await api.doc(id, revision);
  const [{versions}, objects] = await Promise.all([api.history(id), api.objects(id, doc.revision)]);
  if (state.doc?.figure_id !== id) {
    state.selected = null;
    state.locks.clear();
    $('object-search').value = '';
    state.zoom = 1;
  }
  const head = versions[0]?.revision ?? doc.revision;
  if (state.doc?.figure_id !== id || (state.head !== head && !historyMode)) {
    state.past = versions.filter(v => v.revision !== head).map(v => v.revision).reverse().slice(-200);
    state.future = [];
  }
  if (state.doc?.revision !== doc.revision || state.doc?.figure_id !== id) canvas.closeInline();
  Object.assign(state, {doc:{...doc, ...objects}, versions, head, group:null});
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
    canvas.closeInline();
    const group = kind === 'group' ? await readGroup(id) : null;
    Object.assign(state, {selected:{kind,id}, group});
    clearPick();
    switchPage('inspector-properties');
    renderSelection();
  });
}

function pick(point, cycle = false) {
  if (!point || !state.doc?.native_manifest) return;
  return run(async () => {
    const result = await api.pick(state.doc.figure_id, {revision:state.doc.revision, ...point, locked_gids:[...state.locks], ...(cycle && state.selected?.kind === 'native' ? {current_gid:state.selected.id} : {})});
    Object.assign(state, {point, candidates:result.candidate_gids, group:null, selected:result.picked_gid && known('native',result.picked_gid) ? {kind:'native',id:result.picked_gid} : null});
    switchPage('inspector-properties');
    renderSelection();
  }, {done:() => state.selected ? `已选取 ${state.selected.id}，此处有 ${state.candidates.length} 个候选对象。` : '此处没有可拾取对象。'});
}

function mutate(action, form, allowHistorical = false, done = '修改已保存为新版本。', role = 'change') {
  if (!state.doc || (!allowHistorical && !latest())) return;
  return run(async () => {
    const previous = state.head;
    const result = await action(previous);
    if (role === 'undo') { state.future.push(previous); state.past.pop(); }
    else if (role === 'redo') { state.past.push(previous); state.future.pop(); }
    else Object.assign(state,push(state.past,previous));
    if (form?.closest('dialog')) form.closest('dialog').close();
    // The mutation has already committed. Refresh only by reading its returned revision.
    try { await refreshList(); await readFigure(result.figure_id, result.revision,role); }
    catch (error) {
      if (error.status === 401) throw error;
      throw new Error(`服务器已写入版本 ${result.revision}，但刷新失败。请用“刷新”重新打开。`);
    }
    if (form?.id === 'inline-form') $('stage').focus({preventScroll:true});
    else if (form) $('inspector-title').focus({preventScroll:true});
  }, {form, busy:'正在保存并重新渲染图件…', done});
}

function stepHistory(role) {
  const stateStack = role === 'undo' ? state.past : state.future;
  const revision = stateStack.at(-1);
  if (!revision || !latest() || $(role).disabled) return;
  return mutate(expected_revision => api.restore(state.doc.figure_id,{expected_revision,revision}),undefined,false,
    role === 'undo' ? '已撤销。结果保存为新版本。' : '已重做。结果保存为新版本。',role);
}

function saveNative(patches, form) {
  return mutate(expected_revision => api.patch(state.doc.figure_id,true,{expected_revision,patches}),form);
}

const assistant = createAssistant({state,latest,save:saveNative,message});
const canvas = createCanvas({state,latest,pick,save:saveNative,error:(text,isError = true) => message(text,isError)});
const resources = createResources({run,session:() => state.session,pending:() => state.pending,formMessage,onImport:async id => {
  await refreshList();
  await readFigure(id);
  $('canvas-title').focus({preventScroll:true});
}});

function openRename(version) {
  if (!state.doc || state.pending) return;
  state.nameRevision = version.revision;
  $('rename-title').textContent = `命名版本 ${version.revision}`;
  $('version-name').value = version.name ?? '';
  formMessage($('rename-form'),'');
  $('rename-dialog').showModal();
  $('version-name').focus();
}

function openRestore(revision) {
  if (!state.doc || state.pending) return;
  state.restore = revision;
  $('restore-description').textContent = `恢复版本 ${revision} 会创建新版本，不会删除现有历史。`;
  formMessage($('restore-form'), '');
  $('restore-dialog').showModal();
}

function setZoom(factor) {
  if (!state.doc || state.pending) return;
  state.zoom = factor ? Math.max(.5, Math.min(3, state.zoom * factor)) : 1;
  canvas.size();
  $('zoom-reset').textContent = state.zoom === 1 ? '适合' : `${Math.round(state.zoom * 100)}%`;
}

for (const button of document.querySelectorAll('[data-page]')) button.addEventListener('click', () => switchPage(button.dataset.page));
for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => { if (!state.pending) button.closest('dialog').close(); });
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('cancel', event => { if (state.pending) event.preventDefault(); });
$('signin').addEventListener('click', () => $('login-dialog').showModal());
$('login-dialog').addEventListener('close', () => { $('login-form').reset(); formMessage($('login-form'), ''); });
$('frozen-import').addEventListener('click', () => run(async () => {
  const ref = frozenOffer();
  if (!ref) return;
  const imported = await api.importFrozen(ref, state.session.frozen_render_version ?? 1);
  await refreshList();
  await readFigure(imported.figure_id);
  state.session = {...state.session, frozen_project:''};
}, {busy:'正在用原脚本和数据渲染…', done:'图件已导入，可以开始编辑。'}));
$('refresh').addEventListener('click',() => run(async () => {
  await refreshList();
  if (state.doc) await readFigure(state.doc.figure_id); else refreshViews();
  await resources.refresh();
}));
$('zoom-in').addEventListener('click', () => setZoom(1.25));
$('zoom-out').addEventListener('click', () => setZoom(.8));
$('zoom-reset').addEventListener('click', () => setZoom(0));
$('object-search').addEventListener('input',() => listObjects(state.doc,state.selected,state.locks,select));
$('cycle').addEventListener('click', () => pick(state.point, true));
for (const role of ['undo','redo']) $(role).addEventListener('click',() => stepHistory(role));
$('inspector-tabs').addEventListener('keydown',event => {
  const tabs = [...document.querySelectorAll('[data-page]')];
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  const i = {ArrowLeft:(index+tabs.length-1)%tabs.length,ArrowRight:(index+1)%tabs.length,Home:0,End:tabs.length-1}[event.key];
  if (i === undefined) return;
  event.preventDefault();
  switchPage(tabs[i].dataset.page);
  tabs[i].focus();
});
$('preview').addEventListener('error', () => { if (state.doc) message('无法载入该版本的图件预览，请刷新。', true); });
$('stage').addEventListener('keydown', event => {
  if (event.target !== $('stage')) return;
  if ((event.ctrlKey || event.metaKey) && ['z','y'].includes(event.key.toLowerCase())) {
    event.preventDefault();
    stepHistory(event.key.toLowerCase() === 'y' || event.shiftKey ? 'redo' : 'undo');
    return;
  }
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
$('rename-form').addEventListener('submit',event => {
  event.preventDefault();
  if (!state.doc || !state.nameRevision) return;
  const form = event.currentTarget;
  const id = state.doc.figure_id, revision = state.nameRevision, name = $('version-name').value.trim();
  run(async () => {
    const named = await api.nameVersion(id,revision,name);
    state.versions = state.versions.map(entry => entry.revision === revision ? named : entry);
    listHistory(state.versions,state.doc,state.head,{view:rev => openFigure(id,rev),restore:openRestore,name:openRename});
    $('rename-dialog').close();
    $('tab-history').focus({preventScroll:true});
  },{form,busy:'正在保存版本名称…',done:'版本名称已保存。'});
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
