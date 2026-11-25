'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser, flush, Node} = require('./frontend-harness.cjs');

function fixture() {
  const b = browser('corpus_reader.js', {localMarkdown: true});
  const mask = new Node(), body = b.get('crBody');
  mask.setAttribute('hidden', '');
  const originalGet = b.document.getElementById;
  b.document.getElementById = id => {
    if (id === 'corpusReaderMask') return mask.parentNode ? mask : null;
    return originalGet(id);
  };
  const originalCreate = b.document.createElement;
  b.document.createElement = tag => !mask.parentNode && tag === 'div' ? mask : originalCreate(tag);
  mask.querySelector = sel => b.get(sel.slice(1));
  let scrolled = 0;
  body.querySelector = sel => sel === '.cr-citation-focus' && body.innerHTML.includes('cr-citation-focus')
    ? {scrollIntoView() { scrolled++; }} : null;
  const copied = [];
  b.ctx.navigator.clipboard = {writeText: async text => copied.push(text)};
  return {b, body, copied, scrollCount: () => scrolled};
}

const key = 'cite:' + '1'.repeat(64);
const repeated = '🍊 **中文** & text';
const first = '# Fixture\n\n' + repeated + '\n\nAnother paragraph.\n\n';
const text = first + repeated + '\n\nLast section.';
const count = value => Array.from(value).length;

function response(overrides = {}) {
  return {ok: true, kind: 'stored_markdown', title: 'Fixture title', start: 0, end: count(text),
    total_chars: count(text), hit_start: count(first), hit_end: count(first + repeated), chars: count(text),
    md_sha256: 'f'.repeat(64), offset_unit: 'unicode_code_point', index_generation: 'fixture-generation',
    version_id: 'fixture-version', markdown: text, ...overrides};
}

function open(b) {
  b.ctx.CitrusCorpusReader.openCitation({kind: 'local', reader_id: key}, {sessionId: 'my-session', requestId: 'my-turn'});
}

test('only the cited bubble opts into full mode and highlights the exact Unicode occurrence', async () => {
  const {b, body, copied, scrollCount} = fixture();
  open(b);
  const full = new URL(b.requests[0].url, 'https://fixture.example');
  assert.equal(full.pathname, '/api/v2/session/my-session/citation-source');
  assert.equal(full.searchParams.get('ref_id'), key);
  assert.equal(full.searchParams.get('request_id'), 'my-turn');
  assert.equal(full.searchParams.get('mode'), 'full');
  assert.equal(full.searchParams.has('offset'), false);
  b.reply(b.requests[0], response());
  await flush();
  assert.match(body.innerHTML, /Fixture[\s\S]*🍊 <strong>中文<\/strong>[\s\S]*cr-hit-block[\s\S]*🍊 <strong>中文<\/strong>[\s\S]*Last section/);
  assert.equal(scrollCount(), 1);
  b.get('crViewBtn').fire('click');
  assert.match(body.innerHTML, /🍊 \*\*中文\*\* &amp; text[\s\S]*<mark class="cr-hit cr-citation-focus">🍊 \*\*中文\*\* &amp; text<\/mark>/);
  b.get('crCopyBtn').fire('click');
  await flush();
  assert.deepEqual(copied, [text]);
  b.ctx.CitrusCorpusReader.open(key);
  assert.match(b.requests[1].url, /^\/api\/v2\/corpus\/source\?record=/);
  assert.doesNotMatch(b.requests[1].url, /mode=full/);
});

test('duplicate text outside the hit page is not marked, and changed pages fail closed', async () => {
  const {b, body} = fixture();
  open(b);
  b.reply(b.requests[0], response({markdown: repeated, end: count(repeated), chars: count(repeated), next_start: count(repeated)}));
  await flush();
  assert.doesNotMatch(body.innerHTML, /cr-hit-block/);
  assert.equal(b.get('crCopyBtn').textContent, '复制本页');
  b.get('crNextBtn').fire('click');
  assert.equal(new URL(b.requests[1].url, 'https://fixture.example').searchParams.get('offset'), String(count(repeated)));
  b.reply(b.requests[1], response({start: count(repeated), markdown: Array.from(text).slice(count(repeated)).join(''),
    md_sha256: 'e'.repeat(64)}));
  await flush();
  assert.match(body.innerHTML, /不可核验/);
  assert.doesNotMatch(body.innerHTML, /Last section/);
});

test('fallback is explicitly labeled and rendering never loads a source image', async () => {
  const {b, body} = fixture();
  const fragment = '![remote](https://external.example/pixel)\n\n<img src="outside">';
  open(b);
  b.reply(b.requests[0], response({kind: 'stored_evidence', coverage: 'chunk', markdown: fragment,
    end: count(fragment), total_chars: count(fragment), chars: count(fragment), hit_start: 0, hit_end: count(fragment)}));
  await flush();
  assert.match(body.innerHTML, /remote/);
  assert.doesNotMatch(body.innerHTML, /<img\b/);
  assert.match(b.get('crFootNote').textContent, /全文不可用/);
});
