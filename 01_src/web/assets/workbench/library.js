import { byId, element } from './controls.js';

export function thumbnail(project, revisionUrl) {
  const format = project.exports?.svg ? 'svg' : project.exports?.png ? 'png' : '';
  if (!format) return null;
  const image = element('img', undefined, 'thumbnail');
  image.src = revisionUrl(project, `/preview/${format}`);
  image.alt = '';
  image.loading = 'lazy';
  image.addEventListener('error', () => image.remove());
  return image;
}

export function initializeAssets({ request, api, revisionUrl, choose }) {
  const list = byId('assetList'), message = byId('assetStatus'), more = byId('assetMore'), retry = byId('assetRetry');
  let sequence = 0, offset = 0, debounce;

  async function load(append = false) {
    const current = ++sequence;
    more.hidden = retry.hidden = true;
    message.textContent = '正在读取图件…';
    if (!append) { list.replaceChildren(); offset = 0; }
    const type = byId('assetKind').value;
    const url = new URLSearchParams({ limit: 12, offset, query: byId('assetQuery').value,
      access: byId('assetAccess').value });
    if (type.startsWith('chart:')) url.set('chart', type.slice(6));
    else if (type) url.set('kind', type);
    try {
      const { projects, has_more: hasMore } = await (await request(`${api}/projects?${url}`)).json();
      if (current !== sequence) return;
      for (const project of projects) {
        const row = element('li');
        const button = element('button');
        const preview = thumbnail(project, revisionUrl);
        if (preview) button.append(preview);
        button.append(element('span', project.title));
        button.addEventListener('click', () => choose(project));
        row.append(button);
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

  byId('assetQuery').addEventListener('input', () => {
    clearTimeout(debounce);
    sequence += 1;
    debounce = setTimeout(load, 180);
  });
  for (const id of ['assetKind', 'assetAccess']) byId(id).addEventListener('change', () => {
    clearTimeout(debounce);
    load();
  });
  more.addEventListener('click', () => load(true));
  retry.addEventListener('click', () => load());
  load();
  return () => load();
}

export function initializeOpenDialog({ request, api, revisionUrl, loadAsset, activate, changed, busy, onOpened, requested }) {
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
      onOpened();
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
    if (busy() || dialog.open) return;
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

  byId('openLibrary').addEventListener('click', () => openLibrary());
  byId('moreFigures').addEventListener('click', () => refreshLibrary(true));
  byId('retryLibrary').addEventListener('click', () => {
    byId('libraryItems').hidden = false;
    byId('selectedAsset').hidden = true;
    refreshLibrary();
  });
  confirmOpen.addEventListener('click', openCandidate);
  byId('cancelOpen').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => { library.epoch += 1; });
  return openLibrary;
}
