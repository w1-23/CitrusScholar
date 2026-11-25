'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '..', 'assets', 'admin.js'), 'utf8');
const start = source.indexOf('// ---------------------------------------------------------------- 文献候审库');
const end = source.indexOf('// ---------------------------------------------------------------- 注册申请', start);
assert.ok(start >= 0 && end > start);

function harness(fetch) {
  const elements = new Map();
  const el = (id) => {
    if (!elements.has(id)) elements.set(id, {
      value: '', innerHTML: '', textContent: '', open: false, hidden: true,
      removeAttribute(name) { delete this[name]; },
      showModal() { this.open = true; },
    });
    return elements.get(id);
  };
  const scope = {el, fetch, AbortController, esc: String,
    jsonRequest() { throw new Error('unexpected JSON or mutation request'); },
    notify(message) { throw new Error(message); }};
  vm.runInNewContext(source.slice(start, end), scope);
  const button = {dataset: {id: '1', corpus: 'open'},
    closest: () => ({querySelector: () => ({textContent: 'fixture title'})})};
  const action = () => scope.handleCorpusAction({target: {closest: () => button}});
  return {el, scope, action, button};
}

function candidates() {
  return {
    candidates: [
      {id: 1, title:'Archived fixture', status:'pending', raw_status:'available', archive_state:'archived'},
      {id: 2, title:'Metadata fixture', status:'pending', raw_status:'fulltext_missing',
        archive_state:'fulltext_missing', url:'https://example.test/article'},
      {id: 3, title:'Unavailable fixture', status:'approved', raw_status:'unavailable', archive_state:'archived'},
    ],
  };
}

test('review list reports real availability and does not offer write actions without legacy opt in', async () => {
  const h = harness(() => { throw new Error('unexpected request'); });
  h.scope.renderCorpusCandidates(candidates());
  const readOnly = h.el('corpusList').innerHTML;
  assert.match(readOnly, /查看原件/);
  assert.equal((readOnly.match(/data-corpus="open"/g) || []).length, 1);
  assert.match(readOnly, /已归档 · 原件可用/);
  assert.match(readOnly, /未取得全文 · 仅有元数据/);
  assert.match(readOnly, /原件不可用/);
  assert.match(readOnly, /https:\/\/example\.test\/article/);
  assert.doesNotMatch(readOnly, /data-corpus="(?:fulltext|approve|reject|reopen)"/);
  h.button.dataset.corpus = 'reject';
  await h.action();
  h.scope.renderCorpusCandidates({...candidates(), legacy_mutation_enabled:true});
  assert.match(h.el('corpusList').innerHTML, /补录全文/);
  assert.match(h.el('corpusList').innerHTML, /data-corpus="approve"/);
});

test('open original uses authenticated non-JSON bytes and exact download link', async () => {
  const text = '# Exact original\r\n<script>fixture</script>';
  const h = harness(async (url, options) => {
    assert.equal(url, '/api/v2/admin/corpus/candidates/1/raw');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.method, undefined);
    return {ok:true, status:200, headers:new Map([['content-type', 'text/plain; charset=utf-8']]),
      text: async () => text, json() { throw new Error('do not parse raw as JSON'); }};
  });
  await h.action();
  assert.equal(h.el('corpusOriginalText').textContent, text);
  assert.equal(h.el('corpusOriginalText').hidden, false);
  assert.equal(h.el('corpusOriginalDownload').href, '/api/v2/admin/corpus/candidates/1/raw?download=true');
  h.scope.resetCorpusOriginal();
  assert.equal(h.el('corpusOriginalText').textContent, '');
  assert.equal(h.el('corpusOriginalDownload').href, undefined);
});

test('gone and JSON responses do not display content or offer download', async () => {
  for (const [ok, status] of [[false, 410], [true, 200]]) {
    const h = harness(async () => ({ok, status, headers:new Map([['content-type', 'application/json']]),
      text() { throw new Error('unexpected JSON body read'); }}));
    await h.action();
    assert.match(h.el('corpusOriginalStatus').textContent, ok ? /未收到原件/ : /原件已不可用/);
    assert.equal(h.el('corpusOriginalText').hidden, true);
    assert.equal(h.el('corpusOriginalDownload').hidden, true);
  }
});

test('PDF offers open and download links without decoding binary as text', async () => {
  const h = harness(async () => ({ok:true, status:200, headers:new Map([['content-type', 'application/pdf']]),
    text() { throw new Error('unexpected text decode'); }}));
  await h.action();
  assert.equal(h.el('corpusOriginalPdf').href, '/api/v2/admin/corpus/candidates/1/raw');
  assert.equal(h.el('corpusOriginalPdf').hidden, false);
  assert.equal(h.el('corpusOriginalDownload').hidden, false);
});
