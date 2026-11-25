import { byId, element } from './dom.js';

import { legacyEditorUrl } from './navigation.js?v=1';

const DRAG_TYPE = 'application/x-citrus-workbench-figure';
const validId = (id) => typeof id === 'string' && /^[a-f0-9]{32}$/.test(id);
const validVersion = (value) => Number.isSafeInteger(value) && value >= 0;

function thumbnail(project, revisionUrl) {
  const format = project.exports?.svg ? 'svg' : project.exports?.png ? 'png' : '';
  if (!format) return null;
  const image = element('img', undefined, 'thumbnail');
  image.src = revisionUrl(project, `/preview/${format}`);
  image.alt = '';
  image.draggable = false;
  image.loading = 'lazy';
  image.addEventListener('error', () => image.remove());
  return image;
}

export function initializeLibrary({ request, api, revisionUrl, loadAsset, activate, changed, busy, switchToken, setStatus, onOpened }) {
  const list = byId('assetList'), message = byId('assetStatus'), more = byId('assetMore'), retry = byId('assetRetry');
  const dialog = byId('library'), shelf = byId('assetLibrary'), search = byId('assetQuery'), canvas = byId('canvas');
  const notice = byId('libraryNotice');
  let sequence = 0, offset = 0, debounce, dragged, opening = false;

  async function load(append = false) {
    const current = ++sequence;
    more.hidden = retry.hidden = true;
    message.textContent = '正在读取图件…';
    if (!append) { list.replaceChildren(); offset = 0; }
    const params = new URLSearchParams({ limit: 12, offset, query: search.value });
    try {
      const { projects, has_more: hasMore } = await (await request(`${api}/projects?${params}`)).json();
      if (current !== sequence) return;
      for (const project of projects) {
        if (!validId(project.figure_id) || !validVersion(project.render_version)) continue;
        const row = element('li');
        const button = element('button', undefined, 'asset-button');
        button.type = 'button';
        button.draggable = true;
        const preview = thumbnail(project, revisionUrl);
        if (preview) button.append(preview);
        const caption = element('span', undefined, 'asset-caption');
        caption.append(element('strong', project.title || '未命名图件'), element('small', `版本 ${project.render_version}`));
        button.append(caption);
        button.setAttribute('aria-label', project.title || '未命名图件');
        button.addEventListener('click', () => open(project));
        button.addEventListener('dragstart', (event) => {
          if (!event.dataTransfer || busy() || opening || dialog.open) { event.preventDefault(); return; }
          dragged = project;
          event.dataTransfer.effectAllowed = 'copy';
          event.dataTransfer.setData(DRAG_TYPE, project.figure_id);
        });
        button.addEventListener('dragend', () => { dragged = null; canvas.classList.remove('drop-ready'); });
        row.append(button);
        const legacy = legacyEditorUrl(project);
        if (legacy) {
          const editor = element('a', '旧版 SVG 编辑器', 'asset-editor');
          editor.href = legacy;
          editor.setAttribute('aria-label', `在旧版 SVG 编辑器打开 ${project.title || '未命名图件'}`);
          row.append(editor);
        }
        list.append(row);
      }
      offset += projects.length;
      more.hidden = !hasMore;
      message.textContent = offset ? '' : '没有符合条件的图件。';
    } catch (error) {
      if (current !== sequence) return;
      message.textContent = error.message;
      retry.hidden = false;
    }
  }

  function report(message, error = false) {
    setStatus(message, error);
    if (dialog.open) {
      notice.textContent = message;
      notice.classList.toggle('warning', error);
    }
  }

  async function open(candidate) {
    if (busy() || opening) return;
    const id = candidate.figure_id, version = candidate.render_version;
    if (!validId(id) || (version !== undefined && !validVersion(version))) {
      report('图件链接无效。', true);
      return;
    }
    if (changed() && !window.confirm('打开成功后会放弃当前未保存的草稿。继续打开？')) return;
    opening = true;
    const fromDialog = dialog.open, token = switchToken();
    list.inert = true;
    report('正在打开图件…');
    try {
      const url = version === undefined ? `${api}/projects/${encodeURIComponent(id)}` : revisionUrl(candidate);
      const project = await (await request(url)).json();
      if (project.figure_id !== id || !validVersion(project.render_version) ||
          (version !== undefined && project.render_version !== version)) {
        throw new Error('图件版本无法确认，请重新选择。');
      }
      const asset = await loadAsset(project);
      if ((fromDialog && !dialog.open) || busy() || token !== switchToken()) {
        URL.revokeObjectURL(asset.url);
        if (!fromDialog || dialog.open) report('当前草稿已变化，未切换图件。');
        return;
      }
      activate(project, asset);
      if (dialog.open) dialog.close();
      onOpened();
    } catch (error) {
      report(error.status === 409 ? '图件版本已更新，请重新选择图件。' : error.message, true);
      if (error.status === 409) load();
    } finally {
      opening = false;
      list.inert = false;
    }
  }

  const canDrop = (event) => dragged && event.dataTransfer?.types.includes(DRAG_TYPE);
  canvas.addEventListener('dragover', (event) => {
    if (!event.dataTransfer) return;
    event.preventDefault();
    const supported = canDrop(event);
    event.dataTransfer.dropEffect = supported ? 'copy' : 'none';
    if (!supported) return;
    canvas.classList.add('drop-ready');
  });
  canvas.addEventListener('dragleave', (event) => {
    if (!event.relatedTarget || !canvas.contains(event.relatedTarget)) canvas.classList.remove('drop-ready');
  });
  canvas.addEventListener('drop', (event) => {
    canvas.classList.remove('drop-ready');
    event.preventDefault();
    if (!canDrop(event)) return;
    const project = dragged;
    dragged = null;
    if (event.dataTransfer.getData(DRAG_TYPE) === project.figure_id) open(project);
  });
  byId('openLibrary').addEventListener('click', () => {
    if (busy() || dialog.open) return;
    byId('assetDialogShelf').append(shelf);
    notice.textContent = '选择图件即可打开。';
    notice.classList.remove('warning');
    dialog.showModal();
    search.focus();
  });
  byId('cancelOpen').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => byId('assetShelf').append(shelf));
  search.addEventListener('input', () => {
    clearTimeout(debounce);
    sequence += 1;
    list.replaceChildren();
    debounce = setTimeout(load, 180);
  });
  more.addEventListener('click', () => load(true));
  retry.addEventListener('click', () => load());
  load();
  return { open, refresh: () => { clearTimeout(debounce); load(); } };
}
