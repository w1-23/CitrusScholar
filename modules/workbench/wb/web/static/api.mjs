// SPDX-License-Identifier: AGPL-3.0-only
const figure = id => `figures/${encodeURIComponent(id)}`;
const group = (id, gid) => `${figure(id)}/groups/${encodeURIComponent(gid)}`;
const rev = revision => revision ? `?revision=${encodeURIComponent(revision)}` : '';
const body = (method, data) => ({method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function createApi(moduleUrl = import.meta.url) {
  const base = new URL('../../api/v1/', moduleUrl);
  async function request(path, options, binary = false) {
    const response = await fetch(new URL(path, base), {credentials: 'same-origin', ...options});
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = payload?.detail;
      const validation = Array.isArray(detail) ? detail.map(item => `${item.loc?.slice(1).join('.') || '参数'}：${item.msg || '无效'}`).join('；') : '';
      throw new ApiError(response.status, (typeof detail === 'string' && detail) || validation || `请求未完成（HTTP ${response.status}）`);
    }
    return binary ? response : response.json();
  }

  return {
    session: () => request('session'),
    login: token => request('standalone', body('POST', {token})),
    list: () => request('figures'),
    cards: () => request('preview-cards'),
    prepare: (source, key) => {
      const options = body('POST', {source});
      options.headers['Idempotency-Key'] = key;
      return request('preview-runs', options);
    },
    prepareStatus: id => request(`preview-runs/${encodeURIComponent(id)}`),
    thumbnail: path => {
      if (typeof path !== 'string' || !/^preview-cards\/[A-Za-z0-9_-]+\/thumbnail\?version=[1-9][0-9]*$/.test(path)) return null;
      return new URL(path, base).href;
    },
    openCard: (card, parse_mode) => request('figures/import', body('POST', {
      preview_ref:card.preview_ref, preview_version:card.preview_version, options:{parse_mode},
    })),
    library: (format, type = '') => request(`library?${new URLSearchParams({format, type})}`),
    upload: file => request('assets', {method:'POST', headers:{'X-Filename':encodeURIComponent(file.name), 'Content-Type':'application/octet-stream'}, body:file}),
    importFrozen: (project_ref, render_version = 1) => request('figures/import-frozen', body('POST', {project_ref, render_version})),
    importFigure: (source, parse_mode) => request('figures/import', body('POST', {source, options:{parse_mode}})),
    doc: (id, revision) => request(figure(id) + rev(revision)),
    objects: (id, revision) => request(`${figure(id)}/objects${rev(revision)}`),
    preview: (id, revision) => new URL(`${figure(id)}/preview${rev(revision)}`, base).href,
    assetPreview: ref => new URL(`library/${encodeURIComponent(ref.slice('library:'.length))}/preview`, base).href,
    pick: (id, data) => request(`${figure(id)}/pick`, body('POST', data)),
    group: (id, gid, revision) => request(group(id, gid) + rev(revision)),
    resolve: (id, data) => request(`${figure(id)}/groups/resolve`, body('POST', data)),
    transform: (id, gid, kind, data) => request(`${group(id, gid)}/${kind}`, body('POST', data)),
    patch: (id, native, data) => request(`${figure(id)}/${native ? 'native-' : ''}overrides`, body('PATCH', data)),
    history: id => request(`${figure(id)}/history`),
    nameVersion: (id, revision, name) => request(`${figure(id)}/history/${encodeURIComponent(revision)}/name`,body('PUT',{name})),
    restore: (id, data) => request(`${figure(id)}/restore`, body('POST', data)),
    export: (id, data) => request(`${figure(id)}/export`, body('POST', data), true),
  };
}

export const api = createApi();

export function safeReturn(value, origin) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || /[\\\x00-\x1f]/.test(value)) return null;
  const url = new URL(value, origin);
  return url.origin === origin ? url.href : null;
}

export function importSource(format, ref) {
  const ext = (format || '').toLowerCase();
  const script = {py:'python', r:'r'};
  if (Object.hasOwn(script, ext)) return {kind:'script', language:script[ext], entrypoint_ref:ref};
  if (['png','jpg','jpeg','svg','pdf'].includes(ext)) return {kind:'rendered_asset', language:'unknown', entrypoint_ref:ref};
  throw new Error('此格式不支持导入。');
}

export function importPlan(format, ref, kind = 'script') {
  const source = importSource(format, ref);
  const wanted = kind === 'script' ? 'script' : 'rendered_asset';
  if (source.kind !== wanted) throw new Error(kind === 'script' ? '可编辑导入只接受 Python 或 R 源脚本。' : '静态预览只接受 PNG、JPEG、SVG 或 PDF。');
  return {source, mode:kind === 'script' ? 'editable' : 'preview_only'};
}

export function selectedHash(version) {
  if (typeof version !== 'string' || !/^[a-f0-9]{64}$/.test(version))
    throw new Error('无法确认所选文件的版本，请刷新资料库后重试。');
  return version;
}

export function previewPoint(rect, x, y) {
  if (!(rect.width > 0 && rect.height > 0) || !Number.isFinite(x) || !Number.isFinite(y)) return null;
  const unit = n => Math.max(0, Math.min(1, n));
  return {x:unit((x - rect.left) / rect.width), y:unit((y - rect.top) / rect.height)};
}

export function exportOptions(values) {
  return Object.fromEntries(['format', 'width_mm', 'height_mm', 'dpi'].flatMap(name => {
    const value = values.get(name);
    return value == null || value === '' ? [] : [[name, name === 'format' ? value : Number(value)]];
  }));
}
