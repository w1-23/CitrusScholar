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
    const url = new URLSearchParams({ limit: 12, offset, query: byId('assetQuery').value,
      kind: byId('assetKind').value, access: byId('assetAccess').value });
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
