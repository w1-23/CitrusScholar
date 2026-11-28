// SPDX-License-Identifier: AGPL-3.0-only
import {api, importPlan, selectedHash} from './api.mjs?v=2';
import {$, listCards, cardEditable, cardOrdinal} from './views.mjs?v=2';

const formats = {
  script:[['','全部脚本'], ['py','Python'], ['r','R']],
  static:[['','全部静态格式'], ['svg','SVG'], ['png','PNG'], ['jpg','JPG'], ['jpeg','JPEG'], ['pdf','PDF']],
};
const prepareErrors = {
  source_missing:'所选源文件或依赖已不可用。请重新选择。',
  library_unavailable:'暂时无法读取授权源文件。请稍后重试。',
  invalid_source_or_capture:'脚本、依赖或图件序号未能唯一验证。请检查源文件。',
  isolated_render_failed:'隔离渲染未完成。请检查脚本和依赖。',
  renderer_unavailable:'此绘图语言的隔离环境暂不可用。',
  interrupted:'渲染已中断。请重新准备预览。',
};
const running = status => ['pending','running','queued'].includes(status);

export function createResources({run, session, pending, formMessage, onImport}) {
  let selectedCard, kind, available = [], sourceLoad = 0, dependencyLoad = 0, cardLoad = 0;
  // A repeated submission of the same source+dependency snapshot reuses its key.
  const keys = new Map();
  let uploaded = new WeakMap();
  async function upload(file) {
    if (!uploaded.has(file)) uploaded.set(file,await api.upload(file));
    return uploaded.get(file);
  }
  const current = () => available.find(asset => asset.asset_ref === $('source-library').value);
  function updateSource() {
    const asset = current();
    $('file').required = !asset;
    $('file-label').hidden = !!asset;
    $('import-asset').hidden = !asset;
    $('import-asset').textContent = asset ? `已授权资料 · ${asset.name}` : '';
  }
  async function sourceOptions() {
    const id = ++sourceLoad;
    const currentKind = kind;
    $('source-status').textContent = '正在读取授权资料…';
    $('source-status').hidden = false;
    $('source-library').disabled = true;
    available = [];
    $('source-library').replaceChildren(new Option('使用本地文件',''));
    updateSource();
    try {
      const {assets} = await api.library($('filter').value, currentKind);
      if (id !== sourceLoad || !$('import-dialog').open) return;
      const refs = new Set((session()?.source_assets ?? []).map(ref => ref.startsWith('library:') ? ref : `library:${ref}`));
      available = assets.filter(asset => formats[currentKind].some(([ext]) => ext && ext === asset.format.toLowerCase()));
      available.sort((a,b) => Number(refs.has(b.asset_ref)) - Number(refs.has(a.asset_ref)));
      $('source-library').append(...available.map(asset => new Option(`${asset.name}${refs.has(asset.asset_ref) ? ' · 来自本次会话' : ''}`, asset.asset_ref)));
      $('source-status').hidden = !!available.length;
      $('source-status').textContent = '没有符合条件的授权资料，仍可选择本地文件。';
    } catch {
      if (id !== sourceLoad || !$('import-dialog').open) return;
      $('source-status').textContent = '无法读取授权资料，仍可选择本地文件。';
    } finally {
      if (id === sourceLoad) $('source-library').disabled = false;
    }
  }
  async function loadDependencies() {
    const id = ++dependencyLoad;
    const status = $('dependency-status');
    try {
      const {assets} = await api.library('', 'dependency');
      if (id !== dependencyLoad || !$('import-dialog').open) return;
      const options = assets.map(asset => {
        const option = new Option(asset.name, asset.asset_ref);
        option.dataset.version = asset.version;
        return option;
      });
      $('library-dependencies').replaceChildren(...options);
      $('library-dependencies-label').hidden = !options.length;
      status.hidden = true;
    } catch {
      if (id !== dependencyLoad || !$('import-dialog').open) return;
      status.textContent = '无法读取授权依赖，仍可选择本地数据文件。';
      status.hidden = false;
    }
  }
  function showForm(which) {
    if (!session() || pending()) return;
    kind = which;
    const script = kind === 'script';
    $('import-form').reset();
    formMessage($('import-form'),'');
    $('import-title').textContent = script ? '准备可编辑图件预览' : '打开静态预览';
    $('import-submit').textContent = script ? '隔离渲染预览' : '打开仅预览图件';
    $('library-kind').value = kind;
    $('file').accept = script ? '.py,.r' : '.svg,.png,.jpg,.jpeg,.pdf';
    $('filter').replaceChildren(...formats[kind].map(([value,text]) => new Option(text,value)));
    $('dependency-fields').hidden = !script;
    $('library-dependencies-label').hidden = true;
    $('library-dependencies').replaceChildren();
    $('dependency-status').hidden = true;
    $('import-note').textContent = script
      ? '只有点“隔离渲染预览”才运行脚本。渲染后从图件资源选择结果，再决定是否打开编辑。'
      : 'PNG、JPEG、SVG 和 PDF 首页仅作静态预览，不转换为可编辑对象。';
    ++dependencyLoad;
    $('import-dialog').showModal();
    void sourceOptions();
    if (script) void loadDependencies();
  }
  async function refresh() {
    if (session()?.worker_only) return;
    const id = ++cardLoad;
    $('assets').textContent = '正在读取已准备的图件…';
    try {
      const {cards} = await api.cards();
      if (id === cardLoad) listCards(cards, openCard);
    } catch (error) {
      if (id !== cardLoad) return;
      $('assets').textContent = '无法读取已准备的图件。';
      throw error;
    }
  }
  function openCard(card) {
    if (!session() || pending()) return;
    const url = api.thumbnail(card.thumbnail_url);
    if (!url) return;
    selectedCard = card;
    $('card-title').textContent = card.label;
    $('card-details').textContent = `图 ${cardOrdinal(card)} · 预览版本 ${card.preview_version} · ${cardEditable(card) ? '来源已验证' : '此图仅预览'}`;
    $('card-image').src = url;
    $('card-mode').value = cardEditable(card) ? 'editable' : 'preview_only';
    $('card-mode-label').hidden = !cardEditable(card);
    formMessage($('card-form'),'');
    $('card-dialog').showModal();
  }
  function clear() {
    ++cardLoad; ++sourceLoad; ++dependencyLoad;
    available = [];
    keys.clear();
    uploaded = new WeakMap();
    selectedCard = null;
    $('assets').textContent = '登录后显示已渲染的预览。';
  }
  async function sourceBundle() {
    const asset = current(), file = asset ? null : $('file').files[0];
    const format = asset?.format ?? file?.name.split('.').pop();
    const {source, mode} = importPlan(format,asset?.asset_ref,kind);
    const depFiles = mode === 'editable' ? Array.from($('dependencies').files) : [];
    const selected = mode === 'editable' ? Array.from($('library-dependencies').selectedOptions) : [];
    if (selected.length + depFiles.length > 32) throw new Error('最多可选择 32 个依赖文件。');
    if ((file?.size ?? 0) + depFiles.reduce((n, dep) => n + dep.size,0) > 12*1024*1024)
      throw new Error('本地脚本及依赖不能超过 12 MiB。');
    if (!asset && !file) throw new Error('请选择源文件。');
    if (mode === 'editable' && file?.size > 512*1024) throw new Error('脚本不能超过 512 KiB。');
    source.hashes = Object.create(null);
    const pin = (ref,version) => { source.hashes[ref] = selectedHash(version); return ref; };
    if (asset) pin(asset.asset_ref,asset.version);
    else {
      const entry = await upload(file);
      source.entrypoint_ref = pin(entry.asset_ref,entry.source_hash);
    }
    source.assets = selected.map(option => pin(option.value,option.dataset.version));
    for (const file of depFiles) {
      const dep = await upload(file);
      source.assets.push(pin(dep.asset_ref,dep.source_hash));
    }
    return {source,mode};
  }
  for (const id of ['import-open','empty-prepare']) $(id).addEventListener('click',() => showForm('script'));
  $('static-open').addEventListener('click',() => showForm('static'));
  $('source-library').addEventListener('change',updateSource);
  $('filter').addEventListener('change',sourceOptions);
  $('import-form').addEventListener('submit',event => {
    event.preventDefault();
    const form = event.currentTarget, script = kind === 'script';
    return run(async () => {
      const {source,mode} = await sourceBundle();
      if (script) {
        const snapshot = JSON.stringify(source);
        if (!keys.has(snapshot)) {
          if (keys.size >= 24) keys.delete(keys.keys().next().value);
          keys.set(snapshot,crypto.randomUUID());
        }
        const result = await api.prepare(source,keys.get(snapshot));
        const operation = running(result.status) ? await api.prepareStatus(result.operation_id) : result;
        if (!operation.cards?.length) {
          if (!running(operation.status)) keys.delete(snapshot);
          throw new Error(running(operation.status) ? '图件仍在渲染，可稍后重新提交同一选择检查结果。' : prepareErrors[operation.error] ?? '没有生成可辨识的预览图。请核对脚本、依赖和图件数量后再试。');
        }
        $('import-dialog').close();
        await refresh();
        $('assets').querySelector('button')?.focus();
      } else {
        const result = await api.importFigure(source,mode);
        $('import-dialog').close();
        await onImport(result.figure_id);
      }
    },{form,busy:script ? '正在隔离运行并生成预览…' : '正在读取静态图件…',done:script ? '已生成预览。请在“图件资源”中选择渲染结果。' : '已打开静态预览。'});
  });
  $('card-form').addEventListener('submit',event => {
    event.preventDefault();
    const form = event.currentTarget, card = selectedCard;
    if (!card) return;
    return run(async () => {
      const result = await api.openCard(card,cardEditable(card) ? $('card-mode').value : 'preview_only');
      $('card-dialog').close();
      await onImport(result.figure_id);
    },{form,busy:'正在打开固定版本的渲染图…',done:'已打开图件。'});
  });
  return {refresh,clear};
}
