'use strict';
// Focused offline regressions for the U3 library item actions and the bounded
// browser-side table previews. Loads the production script with a tiny DOM and
// no network, vendor code or credentials.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const flush = async () => { for (let i = 0; i < 120; i++) await Promise.resolve(); };
const VOID = new Set(['img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'area',
  'base', 'col', 'embed', 'param', 'track', 'wbr']);

function decodeEntities(value) {
  return String(value)
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

class Element {
  constructor(tag = 'div', doc = null) {
    this.tagName = String(tag).toUpperCase();
    this.doc = doc;
    this.children = [];
    this.parentNode = null;
    this.attrs = {};
    this.dataset = {};
    this.listeners = {};
    this.className = '';
    this._text = '';
    this._html = '';
    this._hidden = false;
    this.title = '';
    this.href = '';
    this.tabIndex = -1;
    this.style = {
      setProperty(k, v) { this[k] = v; },
      getPropertyValue(k) { return this[k] || ''; },
      removeProperty(k) { delete this[k]; }
    };
    this.classList = {
      contains: name => this.className.split(/\s+/).filter(Boolean).includes(name),
      add: (...names) => {
        this.className = [...new Set(this.className.split(/\s+/).filter(Boolean).concat(names))]
          .join(' ').trim();
      },
      remove: (...names) => {
        this.className = this.className.split(/\s+/).filter(n => n && !names.includes(n)).join(' ');
      },
      toggle: (name, on) => {
        const want = on === undefined ? !this.classList.contains(name) : !!on;
        this.classList[want ? 'add' : 'remove'](name);
        return want;
      }
    };
  }
  get parentElement() { return this.parentNode; }
  get hidden() { return this._hidden; }
  set hidden(value) {
    this._hidden = !!value;
    if (this._hidden) this.attrs.hidden = '';
    else delete this.attrs.hidden;
  }
  appendChild(node) {
    if (node.parentNode) node.remove();
    this.children.push(node);
    node.parentNode = this;
    return node;
  }
  append(node) { return this.appendChild(node); }
  before(node) { if (this.parentNode) this.parentNode.insertBefore(node, this); }
  insertBefore(node, before) {
    if (!before) return this.appendChild(node);
    node.remove();
    const index = this.children.indexOf(before);
    this.children.splice(index < 0 ? this.children.length : index, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    this.children = this.children.filter(n => n !== node);
    node.parentNode = null;
    return node;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...nodes) {
    this.children.forEach(n => { n.parentNode = null; });
    this.children = [];
    this._text = '';
    nodes.forEach(n => this.appendChild(n));
  }
  set textContent(text) {
    this.replaceChildren();
    this._text = String(text);
    this._html = '';
  }
  get textContent() {
    return this._text + this.children.map(n => n.textContent).join('');
  }
  set innerHTML(html) {
    this.replaceChildren();
    this._html = String(html);
    const tokens = String(html).match(/<[^>]*>|[^<]+/g) || [];
    const stack = [this];
    for (const token of tokens) {
      if (token.startsWith('</')) {
        if (stack.length > 1) stack.pop();
        continue;
      }
      if (!token.startsWith('<')) {
        stack[stack.length - 1]._text += decodeEntities(token);
        continue;
      }
      const tag = token.match(/^<([a-zA-Z][\w-]*)([\s\S]*?)\/?>$/);
      if (!tag) continue;
      const node = new Element(tag[1], this.doc);
      const attrText = tag[2] || '';
      const attrRe = /([\w:-]+)(?:\s*=\s*"([^"]*)")?/g;
      let attr;
      while ((attr = attrRe.exec(attrText))) {
        if (!attr[1] || attr[1] === '/') continue;
        node.setAttribute(attr[1], attr[2] === undefined ? '' : decodeEntities(attr[2]));
      }
      stack[stack.length - 1].appendChild(node);
      if (!VOID.has(tag[1].toLowerCase())) stack.push(node);
    }
  }
  get innerHTML() { return this._html; }
  get firstChild() { return this.children[0] || null; }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name === 'hidden') this._hidden = true;
    if (name === 'title') this.title = String(value);
    if (name.startsWith('data-')) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; if (name === 'hidden') this._hidden = false; }
  matches(selector) {
    if (selector.startsWith('.')) {
      return selector.slice(1).split('.').every(c => this.classList.contains(c));
    }
    if (selector.startsWith('[')) {
      const m = selector.match(/^\[([\w:-]+)(?:="([^"]*)")?\]$/);
      if (!m) return false;
      const value = this.getAttribute(m[1]);
      return m[2] === undefined ? value !== null : String(value) === m[2];
    }
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    return this.children.flatMap(n =>
      (n.matches(selector) ? [n] : []).concat(n.querySelectorAll(selector)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode ? this.parentNode.closest(selector) : null; }
  contains(node) { return node === this || this.children.some(n => n.contains(node)); }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  click() {
    const event = {target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {}};
    (this.listeners.click || []).forEach(fn => fn(event));
    return event;
  }
  focus() { if (this.doc) this.doc.activeElement = this; }
  blur() { if (this.doc && this.doc.activeElement === this) this.doc.activeElement = null; }
  scrollIntoView() {}
  getBoundingClientRect() { return {left: 0, top: 0, right: 120, bottom: 40, width: 120, height: 40}; }
}

function storage() {
  const map = new Map();
  return {
    getItem: key => map.get(key) === undefined ? null : map.get(key),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: key => map.delete(key)
  };
}

function makeBrowser() {
  const nodes = new Map();
  const document = {
    readyState: 'loading',
    activeElement: null,
    body: null,
    documentElement: null,
    getElementById(id) {
      if (!nodes.has(id)) {
        const node = new Element('div', document);
        node.attrs.id = id;
        document.body.appendChild(node);
        nodes.set(id, node);
      }
      return nodes.get(id);
    },
    createElement(tag) { return new Element(tag, document); },
    querySelector(selector) { return document.body.querySelector(selector); },
    querySelectorAll(selector) { return document.body.querySelectorAll(selector); },
    contains(node) { return node === document.body || document.body.contains(node); },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  };
  document.body = new Element('body', document);
  document.documentElement = new Element('html', document);
  document.body.appendChild(document.documentElement);

  const context = {
    console, document, TextDecoder, TextEncoder, AbortController, DOMException,
    navigator: {}, sessionStorage: storage(), localStorage: storage(),
    setTimeout: () => 0, clearTimeout() {}, requestAnimationFrame: () => 0,
    addEventListener() {}, confirm: () => true, alert() {},
    URL: class extends URL { static createObjectURL() { return 'blob:test'; } static revokeObjectURL() {} },
    URLSearchParams,
    uiIcon() { return '<svg aria-hidden="true"></svg>'; },
    showBanner(message) { context.banners.push(message); },
    banners: []
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(readFileSync(join(__dirname, '../assets/session_files.js'), 'utf8'),
    context, {filename: 'session_files.js'});

  const api = {
    context, document,
    get(id) { return document.getElementById(id); },
    fetchHandler: null,
    requests: []
  };
  context.fetch = (url, options) => {
    api.requests.push({url, options: options || {}});
    return Promise.resolve().then(() => api.fetchHandler(url, options));
  };
  return api;
}

function fileRecord(overrides) {
  const base = {
    file_id: 'a'.repeat(32),
    session_id: 'session-a',
    filename: 'raw data.csv',
    origin: 'uploaded',
    mime_type: 'text/csv',
    size_bytes: 2048,
    kind: 'table',
    visibility: 'public',
    created_at: 1700000000,
    last_accessed_at: 1700050000,
    turn_request_id: '',
    source_session_id: 'session-a',
    download_url: '/api/v2/files/' + 'a'.repeat(32) + '/download',
    preview_url: '/api/v2/files/' + 'a'.repeat(32) + '/preview'
  };
  return Object.assign(base, overrides || {});
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status < 400,
    status,
    headers: {get: name => name === 'content-length' ? JSON.stringify(payload).length : null},
    json: async () => payload,
    text: async () => JSON.stringify(payload)
  };
}

function bytesResponse(buffer, headers = {}) {
  return {
    ok: true,
    status: 200,
    headers: {get: name => headers[name] === undefined ? null : String(headers[name])},
    body: new ReadableStream({start(controller) {
      controller.enqueue(new Uint8Array(buffer));
      controller.close();
    }})
  };
}

function workbookBuffer() {
  const xlsx = require('../assets/xlsx.full.min.js');
  const book = xlsx.utils.book_new();
  xlsx.utils.book_append_sheet(book, xlsx.utils.aoa_to_sheet([['A', 'B'], [1, 2]]), 'S1');
  return xlsx.write(book, {type: 'array', bookType: 'xlsx'});
}

function bufferOf(text) {
  return new TextEncoder().encode(text).buffer;
}

async function openTablePreview(browser, file, buffer, headers) {
  browser.fetchHandler = (url) => {
    if (url === '/api/v2/files') {
      return jsonResponse({count: 1, uploaded: [file], generated: [], retention_idle_days: 30});
    }
    assert.equal(url, file.download_url);
    return bytesResponse(buffer, headers || {});
  };
  await browser.context.loadSessionFiles();
  browser.context.openAccountFilePreview(file.file_id);
  await flush();
  return browser.get('sessionFilePreviewStage').querySelector('.sf-preview-table');
}

test('library cards expose only preview, original, reference and conditional source', async () => {
  const b = makeBrowser();
  const uploaded = fileRecord({});
  const generated = fileRecord({
    file_id: 'b'.repeat(32), filename: 'report.pdf', origin: 'generated', kind: 'pdf',
    mime_type: 'application/pdf', download_url: '/api/v2/files/' + 'b'.repeat(32) + '/download',
    turn_request_id: 'crid-1', source_session_id: 'session-a'
  });
  const payload = {
    count: 2, session_id: '', scope: 'account', retention_idle_days: 14,
    uploaded: [uploaded], generated: [generated]
  };
  b.fetchHandler = () => jsonResponse(payload);

  const before = JSON.stringify(payload);
  const returned = await b.context.loadSessionFiles();
  assert.deepEqual(returned, payload, 'loadSessionFiles return shape is unchanged');
  assert.equal(JSON.stringify(returned), before, 'rendering must not mutate the server payload');

  const cards = b.get('sessionFilesUploaded').querySelectorAll('.session-file-item');
  assert.equal(cards.length, 2, 'one searchable catalog contains both kinds of files');
  const uploadedCard = cards.find(card => card.dataset.fileId === uploaded.file_id);
  const generatedCard = cards.find(card => card.dataset.fileId === generated.file_id);
  const actions = card => card.querySelectorAll('[data-file-act]').map(button => button.getAttribute('data-file-act'));
  assert.deepEqual(actions(uploadedCard), ['preview', 'download', 'reference']);
  assert.deepEqual(actions(generatedCard), ['preview', 'download', 'reference', 'jump']);
  assert.equal(uploadedCard.querySelector('[data-file-act="download"]').getAttribute('aria-label'), '下载原件 raw data.csv');
  assert.equal(uploadedCard.querySelector('[aria-haspopup="menu"]'), null);
  assert.match(uploadedCard.querySelector('.session-file-sub').title, /最近使用/);
  b.document.listeners.DOMContentLoaded[0]();
  const jumps = [];
  b.context.jumpToFileTurn = (...pair) => { jumps.push(pair); return true; };
  libraryDocumentAction(b, generatedCard.querySelector('[data-file-act="jump"]'));
  await flush();
  assert.deepEqual(jumps, [['session-a', 'crid-1']]);

  const retention = b.get('sessionFilesRetention').textContent;
  assert.match(retention, /连续 14 天/);
  assert.match(retention, /浏览资料库不会续期/);
  assert.match(retention, /只刷新所用文件的保留期限/);
  assert.doesNotMatch(retention, /保留 30 天/);

  assert.equal(typeof b.context.showSessionFileInfo, 'undefined');
  assert.equal(typeof b.context.openAccountFileExport, 'undefined');
});

test('table thumbnails are visible-only, account-scoped, bounded and rendered as text', async () => {
  const b = makeBrowser();
  let observer;
  class Intersections {
    constructor(callback, options) { this.callback = callback; this.options = options; this.tiles = []; observer = this; }
    observe(tile) { this.tiles.push(tile); }
    unobserve(tile) { this.tiles = this.tiles.filter(item => item !== tile); }
    disconnect() { this.tiles = []; }
    change(tile, visible) { this.callback([{target: tile, isIntersecting: visible}], this); }
  }
  b.context.IntersectionObserver = Intersections;
  const file = fileRecord({});
  const route = `/api/v2/files/${file.file_id}/table-sample`;
  const raw = '<img src=x onerror=alert(1)>';
  const rows = [[raw, 'value', '', 'hidden? no', 'not sampled'], ['2'], ['3'], ['4']];
  b.context.accountFetch = (url, options) => {
    if (url === '/api/v2/files') return Promise.resolve(jsonResponse({uploaded: [file]}));
    assert.equal(url, route);
    assert.equal(options.credentials, 'same-origin');
    assert.ok(options.signal);
    return b.context.fetch(url, options);
  };
  b.fetchHandler = () => jsonResponse({rows});
  await b.context.loadSessionFiles();
  const tile = observer.tiles[0];
  assert.equal(observer.options.root, b.get('sessionFilesBody'));
  assert.equal(tile.querySelector('table'), null);
  observer.change(tile, false);
  assert.equal(b.requests.length, 0);
  observer.change(tile, true);
  await flush();
  assert.deepEqual(b.requests.map(request => request.url), [route]);
  assert.equal(tile.querySelector('table').querySelectorAll('tr').length, 3);
  assert.equal(tile.querySelector('tr').querySelectorAll('td').length, 4);
  assert.equal(tile.querySelector('td').textContent, raw);
  assert.equal(tile.querySelector('img'), null);
  b.context.resetSessionFiles();
  assert.equal(b.get('sessionFilesUploaded').children.length, 0);
});

test('the reference pill queues the existing attachment reference without upload', async () => {
  const b = makeBrowser();
  const uploaded = fileRecord({});
  b.fetchHandler = () => jsonResponse({retention_idle_days: 30, uploaded: [uploaded], generated: []});
  await b.context.loadSessionFiles();
  b.context.queueAccountFileReference(uploaded.file_id);
  assert.equal(b.context._selectedAccountFileRefs.length, 1);
  assert.equal(b.context._selectedAccountFileRefs[0].file_id, uploaded.file_id);
  assert.deepEqual(b.context.banners, ['已添加到当前问题。']);
});

test('CSV parsing keeps quoted commas, doubled quotes and embedded newlines in one cell', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: 'c'.repeat(32), filename: 'quoted.csv',
    download_url: '/api/v2/files/' + 'c'.repeat(32) + '/download'});
  const csv = 'name,note\r\n"Zhang, San","line one\nline ""two"""\r\nplain,"a,b"';
  const node = await openTablePreview(b, file, bufferOf(csv));

  const headers = node.querySelectorAll('th').map(n => n.textContent);
  assert.deepEqual(headers, ['name', 'note']);
  const cells = node.querySelectorAll('td').map(n => n.textContent);
  assert.deepEqual(cells, ['Zhang, San', 'line one\nline "two"', 'plain', 'a,b']);
  assert.equal(node.querySelectorAll('tr').length, 3);
});

test('CSV preview renders client-side batches and never claims server paging', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: 'd'.repeat(32), filename: 'many.csv',
    download_url: '/api/v2/files/' + 'd'.repeat(32) + '/download'});
  const rows = ['value'];
  for (let i = 0; i < 250; i++) rows.push('v' + i);
  const node = await openTablePreview(b, file, bufferOf(rows.join('\n')));

  assert.equal(node.querySelectorAll('tr').length, 100);
  let status = node.querySelector('.sf-table-status').textContent;
  assert.match(status, /已渲染 100 \/ 251 行/);
  let more = node.querySelector('.sf-load-more');
  assert.ok(more);
  assert.equal(more.hidden, false);
  assert.equal(more.textContent, '再显示 100 行');

  more.click();
  await flush();
  assert.equal(node.querySelectorAll('tr').length, 200);
  assert.match(node.querySelector('.sf-table-status').textContent, /200 \/ 251/);

  more = node.querySelector('.sf-load-more');
  more.click();
  await flush();
  assert.equal(node.querySelectorAll('tr').length, 251);
  status = node.querySelector('.sf-table-status').textContent;
  assert.match(status, /已渲染全部 251 行/);
  assert.equal(node.querySelector('.sf-load-more').hidden, true);
  assert.doesNotMatch(node.textContent, /服务器分页/);
});

test('declared over-cap CSV is not parsed at all and offers the original download', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: 'e'.repeat(32), filename: 'huge.csv',
    download_url: '/api/v2/files/' + 'e'.repeat(32) + '/download'});
  b.fetchHandler = (url) => {
    if (url === '/api/v2/files') return jsonResponse({retention_idle_days: 30, uploaded: [file], generated: []});
    return bytesResponse(bufferOf('a,b\n1,2'), {'content-length': 9 * 1024 * 1024});
  };
  await b.context.loadSessionFiles();
  b.context.openAccountFilePreview(file.file_id);
  await flush();
  const node = b.get('sessionFilePreviewStage').querySelector('.sf-preview-table');
  assert.match(node.textContent, /解析上限/);
  assert.match(node.textContent, /未在页面内解析/);
  assert.match(node.innerHTML, /下载原件/);
  assert.equal(node.querySelector('table'), null, 'no partial table is shown');
});

test('streamed CSV is cancelled as soon as the byte cap is crossed', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: 'f'.repeat(32), filename: 'stream.csv',
    download_url: '/api/v2/files/' + 'f'.repeat(32) + '/download'});
  let cancelled = false;
  let reads = 0;
  const chunk = new Uint8Array(5 * 1024 * 1024);
  const body = {
    getReader() {
      return {
        read() {
          reads++;
          return Promise.resolve({done: false, value: chunk});
        },
        async cancel() { cancelled = true; }
      };
    }
  };
  b.fetchHandler = (url) => {
    if (url === '/api/v2/files') return jsonResponse({retention_idle_days: 30, uploaded: [file], generated: []});
    return {ok: true, status: 200, headers: {get: () => null}, body,
      arrayBuffer: async () => { throw new Error('arrayBuffer fallback must not run'); }};
  };
  await b.context.loadSessionFiles();
  b.context.openAccountFilePreview(file.file_id);
  await flush();
  const node = b.get('sessionFilePreviewStage').querySelector('.sf-preview-table');
  assert.equal(cancelled, true);
  assert.equal(reads, 2);
  assert.match(node.textContent, /解析上限/);
  assert.equal(node.querySelector('table'), null);
});

test('XLSX parsing is bounded with sheetRows and its DOM is paged client-side', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: '1'.repeat(32), filename: 'sheet.xlsx',
    download_url: '/api/v2/files/' + '1'.repeat(32) + '/download'});
  let readOptions = null;
  b.context.XLSX = {
    read(buffer, options) {
      readOptions = options;
      return {SheetNames: ['S1'], Sheets: {S1: {'!ref': 'A1:B250'}}};
    },
    utils: {
      decode_range(ref) {
        const end = Number(String(ref).split(':')[1].replace(/[A-Z]+/g, ''));
        return {s: {r: 0, c: 0}, e: {r: end - 1, c: 1}};
      },
      encode_cell(cell) { return String.fromCharCode(65 + cell.c) + (cell.r + 1); }
    }
  };
  const node = await openTablePreview(b, file, workbookBuffer());
  assert.equal(readOptions.sheetRows, 2000);

  let body = node.querySelector('.sf-sheet-body');
  assert.equal(body.querySelectorAll('tr').length, 100);
  assert.match(body.querySelector('.sf-table-status').textContent, /已渲染 100 \/ 250 行/);
  body.querySelector('.sf-load-more').click();
  await flush();
  body = node.querySelector('.sf-sheet-body');
  assert.equal(body.querySelectorAll('tr').length, 200);
  body.querySelector('.sf-load-more').click();
  await flush();
  body = node.querySelector('.sf-sheet-body');
  assert.equal(body.querySelectorAll('tr').length, 250);
  assert.match(body.querySelector('.sf-table-status').textContent, /已渲染全部 250 行/);
  assert.equal(body.querySelector('.sf-load-more'), null);
});

test('XLSX at the sheetRows cap is described as bounded, never as complete', async () => {
  const b = makeBrowser();
  const file = fileRecord({file_id: '2'.repeat(32), filename: 'long.xlsx',
    download_url: '/api/v2/files/' + '2'.repeat(32) + '/download'});
  b.context.XLSX = {
    read() { return {SheetNames: ['S1'], Sheets: {S1: {'!ref': 'A1:A2000'}}}; },
    utils: {
      decode_range() { return {s: {r: 0, c: 0}, e: {r: 1999, c: 0}}; },
      encode_cell(cell) { return 'A' + (cell.r + 1); }
    }
  };
  const node = await openTablePreview(b, file, workbookBuffer());
  const body = node.querySelector('.sf-sheet-body');
  const status = body.querySelector('.sf-table-status').textContent;
  assert.match(status, /已达解析上限 2000 行/);
  assert.match(status, /超出部分请下载原件/);
  assert.match(status, /已渲染 100 \/ 2000 行/);
  assert.doesNotMatch(status, /已渲染全部/);
});

test('audit workbook preview jumps to an internal source cell without following external links', async () => {
  const browser = makeBrowser();
  const xlsx = require('../assets/xlsx.full.min.js');
  browser.context.XLSX = {...xlsx,
    read(buffer, options) {
      return xlsx.read(Buffer.from(new Uint8Array(buffer)), {...options, type: 'buffer'});
    }
  };
  const book = xlsx.utils.book_new();
  const source = { '!ref': 'H23:I145', H23: { t: 's', v: 'header' },
    I145: { t: 'n', v: 42, c: [{ a: 'Audit', t: '重复待核' }] } };
  xlsx.utils.book_append_sheet(book, source, "Samples' data");
  const summary = xlsx.utils.aoa_to_sheet([
    ['位置', '类型', '结论'], ["Samples' data!I145", '重复', '核对原始记录'],
    ['外部地址', '测试', '仅显示文字'], ['不存在的地址', '测试', '仅显示文字']
  ]);
  summary.A2.l = { Target: "#'Samples'' data'!$I$145" };
  summary.A3.l = { Target: 'https://example.org/' };
  summary.A4.l = { Target: "#'Samples'' data'!Z145" };
  xlsx.utils.book_append_sheet(book, summary, '审查结果');
  const file = fileRecord({file_id: 'd'.repeat(32), filename: 'source_审查标注.xlsx',
    download_url: '/api/v2/files/' + 'd'.repeat(32) + '/download'});
  const node = await openTablePreview(browser, file, xlsx.write(book, {type: 'array', bookType: 'xlsx'}));
  node.querySelector('[data-sheet="审查结果"]').click();
  const buttons = node.querySelectorAll('.sf-audit-jump');
  assert.equal(buttons.length, 1);
  buttons[0].click();
  assert.ok(node.querySelector('[data-sheet="Samples\' data"]').classList.contains('active'));
  const target = node.querySelector('[data-ref="I145"]');
  assert.ok(target.classList.contains('sf-cell-focus'));
  assert.ok(target.classList.contains('sf-cell-noted'));
  assert.equal(target.getAttribute('title'), '重复待核');
});


test('account library accepts an actual SVG selection and uses image-only previews', async () => {
  const b = makeBrowser();
  const file = fileRecord({filename: 'private diagram.SVG', mime_type: 'image/svg+xml', kind: 'svg'});
  b.context.currentSessionId = () => 'session-fixture';
  b.fetchHandler = (url, opt) => {
    if (opt.method === 'POST') {
      assert.equal(url, '/api/v2/session/session-fixture/files/upload?filename=private%20diagram.SVG');
      assert.equal(opt.body, selected);
      return jsonResponse({file_id: file.file_id}, 201);
    }
    assert.equal(url, '/api/v2/files');
    return jsonResponse({uploaded: [file], generated: [], retention_idle_days: 30});
  };
  const selected = {name: file.filename, size: 38, type: ''};
  b.document.listeners.DOMContentLoaded[0]();
  const input = b.get('libraryUploadInput');
  b.get('libraryUploadButton').click();
  input.files = [selected];
  input.listeners.change[0]();
  await flush();
  assert.match(b.get('libraryUploadStatus').textContent, /已上传 1 份/);
  const thumb = b.get('sessionFilesUploaded').querySelector('img');
  assert.equal(thumb.getAttribute('src'), file.preview_url + '?library_tile=1');
  b.context.openAccountFilePreview(file.file_id);
  const stage = b.get('sessionFilePreviewStage');
  assert.equal(stage.querySelector('img').getAttribute('src'), file.preview_url);
  assert.equal(stage.querySelector('iframe'), null);
  assert.equal(stage.querySelector('svg'), null);
  assert.equal(stage.querySelector('object'), null);
  assert.equal(b.get('sessionFilePreviewDownload').href, file.download_url);
  const image = stage.querySelector('img');
  image.naturalWidth = 400;
  image.naturalHeight = 200;
  image.parentNode.clientWidth = 240;
  image.parentNode.clientHeight = 280;
  image.listeners.load.forEach(handler => handler());
  stage.querySelector('[aria-label="原始大小 100%"]').click();
  assert.equal(image.style.width, '400px');
  assert.equal(image.style.height, '200px');
  stage.querySelector('[aria-label="放大图片"]').click();
  assert.equal(image.style.width, '500px');
  assert.equal(image.style.height, '250px');
  stage.querySelector('[aria-label="缩小图片"]').click();
  assert.equal(image.style.width, '400px');
  stage.querySelector('[aria-label="适应窗口"]').click();
  assert.equal(image.style.width, '');
  assert.equal(image.style.height, '');
  assert.equal(image.getAttribute('src'), file.preview_url);

});

test('real range links target one merged anchor even when the audit sheet name collides', async () => {
  const b = makeBrowser();
  const xlsx = require('../assets/xlsx.full.min.js');
  b.context.XLSX = {...xlsx, read: (buf, opts) => xlsx.read(Buffer.from(new Uint8Array(buf)), {...opts, type: 'buffer'})};
  const book = xlsx.utils.book_new();
  const originalName = "Plant's Data";
  const sheet = xlsx.utils.aoa_to_sheet([['A', 'B', 'C', 'D'], ['merged', '', 'x', 'y'], ['', '', 'z', 'w']]);
  sheet['!merges'] = [xlsx.utils.decode_range('A2:B3')];
  xlsx.utils.book_append_sheet(book, sheet, originalName);
  xlsx.utils.book_append_sheet(book, xlsx.utils.aoa_to_sheet([['User original named like summary']]), '审查结果');
  const summary = xlsx.utils.aoa_to_sheet([['位置','类型','结论'], ['actual range','duplicate','review'],
    ['valid-looking but absent link A2','not a target','plain'], ['external','web','plain'], ['beyond sheet','none','plain']]);
  summary.A2.l = {Target: "#'Plant''s Data'!$B$3:$D$3"};
  summary.A4.l = {Target: 'https://example.invalid/another-workbook'};
  summary.A5.l = {Target: "#'Plant''s Data'!A2:Z3"};
  xlsx.utils.book_append_sheet(book, summary, '审查结果_2');
  const issues = xlsx.utils.aoa_to_sheet([['序号', '工作表', '区域', '单元格', '行', '列'],
    [1, originalName, 'selected', 'B3', 3, 'B'], [2, originalName, 'selected', '第 3 行（整行）', 3, '']]);
  xlsx.utils.book_append_sheet(book, issues, '问题清单_2');
  const file = fileRecord({filename: 'samples_审查标注.xlsx'});
  const node = await openTablePreview(b, file, xlsx.write(book, {type: 'array', bookType: 'xlsx'}));
  node.querySelector('[data-sheet="审查结果_2"]').click();
  const buttons = node.querySelectorAll('.sf-audit-jump');
  assert.equal(buttons.length, 1);
  buttons[0].click();
  const target = node.querySelector('[data-ref="A2"]');
  assert.equal(target.getAttribute('rowspan'), '2');
  assert.equal(target.getAttribute('colspan'), '2');
  assert.ok(target.classList.contains('sf-cell-focus'));
  assert.equal(node.querySelectorAll('.sf-cell-focus').length, 1);
  assert.match(node.querySelector('[role="status"]').textContent, /B3:D3.*仅定位到范围起点.*合并单元格 A2/);
  node.querySelector('[data-sheet="问题清单_2"]').click();
  assert.equal(node.querySelectorAll('.sf-audit-jump').length, 1);
  node.querySelector('.sf-audit-jump').click();
  assert.match(node.querySelector('[role="status"]').textContent, /B3.*合并单元格 A2/);
});

test('library selection obeys separate reference bytes and joint count from server policy', async () => {
  const b = makeBrowser();
  const mebibyte = 1024 * 1024;
  const a = fileRecord({file_id: 'a'.repeat(32), size_bytes: 8 * mebibyte});
  const c = fileRecord({file_id: 'c'.repeat(32), size_bytes: 12 * mebibyte});
  const d = fileRecord({file_id: 'd'.repeat(32), size_bytes: 1});
  b.fetchHandler = () => jsonResponse({uploaded: [a,c,d], reference_limits: {max_turn_files: 3, max_reference_bytes: 20 * mebibyte}});
  await b.context.loadSessionFiles();
  b.context._selectedAttachments = [{size: 19 * mebibyte}];
  b.context.queueAccountFileReference(a.file_id);
  b.context.queueAccountFileReference(c.file_id);
  assert.equal(b.context._selectedAccountFileRefs.length, 2, 'reference budget is separate from new uploads');
  b.context.queueAccountFileReference(d.file_id);
  assert.match(b.context.banners.at(-1), /最多上传或引用 3/);
  b.context._selectedAttachments = [];
  b.context.queueAccountFileReference(d.file_id);
  assert.equal(b.context._selectedAccountFileRefs.length, 2);
  assert.match(b.context.banners.at(-1), /引用资料总大小不能超过 20 MB/);
});

test('pinned deletes preserve the catalog and older GET cannot resurrect a deleted file', async () => {
  const b = makeBrowser();
  const file = fileRecord();
  const filePayload = {uploaded: [file]};
  b.fetchHandler = () => jsonResponse(filePayload);
  await b.context.loadSessionFiles();
  b.context.queueAccountFileReference(file.file_id);
  b.fetchHandler = () => jsonResponse({detail: {code: 'file_pinned', message: '该文件正被任务引用。'}}, 409);
  assert.equal(await b.context.deleteSessionFile(file.file_id), false);
  assert.equal(b.get('sessionFilesUploaded').querySelectorAll('.session-file-item').length, 1);
  assert.match(b.context.banners.at(-1), /正被任务引用/);
  let release;
  b.fetchHandler = () => new Promise(resolve => { release = resolve; });
  const old = b.context.loadSessionFiles();
  await flush();
  b.fetchHandler = (_, options) => jsonResponse(options.method === 'DELETE' ? {status: 'ok'} : {uploaded: []});
  assert.equal(await b.context.deleteSessionFile(file.file_id), true);
  release(jsonResponse(filePayload));
  await old;
  assert.equal(b.get('sessionFilesUploaded').querySelectorAll('.session-file-item').length, 0);
  assert.equal(b.context._selectedAccountFileRefs.length, 0);
});


const importedFigure = 'b'.repeat(32);
const producerSvg = fileRecord({filename: 'output.svg', mime_type: 'image/svg+xml', kind: 'svg',
  origin: 'generated', producer_tool: 'code_deliver', sha256: 'c'.repeat(64),
  source_session_id: 'producer-session', turn_request_id: 'producer-turn'});

function sourceHandoff(file = producerSvg) {
  return {project: importedFigure, file: file.file_id, sha: file.sha256, account: '17',
    session: file.source_session_id, turn: file.turn_request_id};
}

function libraryDocumentAction(browser, button) {
  assert.ok(button, 'action displayed');
  browser.document.listeners.click[0]({target: button, preventDefault() {}});
}

test('SVG library preview retains direct viewing without copy or edit actions', async () => {
  const b = makeBrowser();
  b.fetchHandler = url => {
    assert.equal(url, '/api/v2/files');
    return jsonResponse({generated: [producerSvg]});
  };
  await b.context.loadSessionFiles();
  const item = b.get('sessionFilesUploaded').querySelector('.session-file-item');
  assert.equal(item.children[0].classList.contains('session-file-thumb'), true);
  assert.equal(item.children[2].classList.contains('session-file-actions'), true);
  assert.deepEqual(item.querySelectorAll('[data-file-act]').map(btn => btn.getAttribute('data-file-act')),
    ['preview', 'download', 'reference', 'jump']);
  b.context.openAccountFilePreview(producerSvg.file_id);
  const stage = b.get('sessionFilePreviewStage');
  assert.ok(stage.querySelector('.sf-preview-image'));
  assert.ok(stage.querySelector('.sf-image-toolbar'));
  assert.equal(stage.querySelector('.sf-create-copy'), null);
  assert.equal(typeof b.context.copySvgSource, 'undefined');
});

test('an uncached generated-file citation cannot land in a different session', async () => {
  const b = makeBrowser();
  let sid = 'first-session';
  b.context.currentSessionId = () => sid;
  b.document.listeners.DOMContentLoaded[0]();
  const action = b.document.createElement('button');
  action.setAttribute('data-file-act', 'reference');
  action.setAttribute('data-file-id', producerSvg.file_id);
  b.document.body.appendChild(action);
  let release;
  b.fetchHandler = () => new Promise(resolve => { release = resolve; });
  libraryDocumentAction(b, action);
  await flush();
  sid = 'other-session';
  release(jsonResponse({generated: [producerSvg]}));
  await flush();
  assert.equal(b.context._selectedAccountFileRefs, undefined);
  libraryDocumentAction(b, action);
  assert.equal(b.context._selectedAccountFileRefs[0].file_id, producerSvg.file_id);
});

test('return jumps only to the exact pair revalidated in the current account catalog', async () => {
  for (const scenario of ['match', 'different account', 'different digest', 'different turn', 'forged query']) {
    const b = makeBrowser();
    const jumps = [];
    const replaces = [];
    const source = scenario === 'forged query' ? 'e'.repeat(32) : producerSvg.file_id;
    b.context.location = new URL('https://fixture.invalid/?files=1&source=' + source);
    b.context.history = {replaceState: (_, __, url) => replaces.push(url.search)};
    b.context.currentAccount = {user_id: scenario === 'different account' ? 29 : 17};
    b.get('authGate').hidden = true;
    b.context.sessionStorage.setItem('citrus:figure-source', JSON.stringify(sourceHandoff()));
    const current = {...producerSvg};
    if (scenario === 'different digest') current.sha256 = 'e'.repeat(64);
    if (scenario === 'different turn') current.turn_request_id = 'other-turn';
    b.fetchHandler = url => {
      assert.equal(url, '/api/v2/files');
      return jsonResponse({generated: [current]});
    };
    b.context.jumpToFileTurn = (...pair) => { jumps.push(pair); return true; };
    b.document.listeners.DOMContentLoaded[0]();
    await flush();
    assert.deepEqual(replaces, [''], scenario);
    assert.deepEqual(jumps, scenario === 'match' ? [['producer-session', 'producer-turn']] : [], scenario);
    assert.equal(b.get('sessionFilesPanel').hidden, scenario === 'match', scenario);
  }
});


test('library can upload Python and R source without offering inline preview', async () => {
  const b = makeBrowser();
  const rows = ['python_plot.py', 'r_plot.R'].map((filename, index) => fileRecord({
    filename, mime_type: 'text/plain; charset=utf-8', kind: 'download', file_id: String(index + 1).repeat(32)
  }));
  const uploaded = [];
  b.context.currentSessionId = () => 'session-fixture';
  b.fetchHandler = (url, opt) => {
    if (opt.method === 'POST') {
      assert.equal(url, '/api/v2/session/session-fixture/files/upload?filename=' + opt.body.name);
      uploaded.push(opt.body.name);
      return jsonResponse({file_id: rows[uploaded.length - 1].file_id}, 201);
    }
    assert.equal(url, '/api/v2/files');
    return jsonResponse({uploaded: rows, generated: []});
  };
  b.document.listeners.DOMContentLoaded[0]();
  const input = b.get('libraryUploadInput');
  input.files = rows.map(file => ({name: file.filename, size: 22, type: ''}));
  input.listeners.change[0]();
  await flush();
  assert.deepEqual(uploaded, rows.map(row => row.filename));
  assert.match(b.get('libraryUploadStatus').textContent, /已上传 2 份/);
  const list = b.get('sessionFilesUploaded');
  assert.equal(list.querySelectorAll('[data-kind="script"]').length, 2);
  assert.equal(list.querySelectorAll('[data-file-act="download"]').length, 2);
  assert.equal(list.querySelector('[data-file-act="preview"]'), null);
  assert.equal(list.querySelector('[data-file-act="reference"]'), null);
});
