'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {File} = require('node:buffer');
const {browser, flush} = require('./frontend-harness.cjs');

const original = new File(['original test PDF bytes'], 'paper.pdf', {type: 'application/pdf'});
const encoded = 'data:application/pdf;base64,b3JpZ2luYWwgdGVzdCBQREYgYnl0ZXM=';
const skill = {id: 'literature-verification', version: 'a'.repeat(40), sha256: 'b'.repeat(64)};

async function review() {
  const b = browser('app.js');
  b.ctx.currentAccount = {user_id: 'A', username: 'A'};
  b.ctx.sessionId = 'A-session';
  const host = b.document.createElement('div');
  b.document.body.appendChild(host);
  b.ctx.appendSentAttachmentPreviews(host, [original]);
  host.querySelector('.paper-review-btn').onclick();
  assert.equal(b.readers[0].file, original);
  b.readers[0].finish(encoded);
  await flush();
  const req = b.requests.find(r => r.url.endsWith('/paper-review'));
  assert.equal(JSON.parse(req.opts.body).data, encoded);
  b.reply(req, {verification_mode: 'external', status: 'presentation_only',
    reason: 'Only citation markers highlighted.', highlighted_pdf_url: '/api/v2/session/A-session/artifact/example'});
  await flush();
  const result = host.querySelector('.paper-review-result');
  const verify = result.querySelector('.paper-review-btn');
  assert.ok(verify, result.textContent);
  assert.equal(verify.textContent, '核验文献');
  assert.equal(result.querySelector('a').href, '/api/v2/session/A-session/paper-preview/example');
  return {b, verify, result};
}

test('PDF verification uses the original PDF and approved skill via normal queue without consuming the draft', async () => {
  const {b, verify, result} = await review();
  const keep = new File(['unrelated draft'], 'other.pdf', {type: 'application/pdf'});
  const keepRef = {file_id: 'keep', filename: 'library.csv'};
  let pickerChanges = 0;
  b.ctx.skillPicker = {capture() { pickerChanges++; return {items: []}; }, acknowledge() { pickerChanges++; }};
  b.ctx._selectedAttachments = [keep];
  b.ctx._selectedAccountFileRefs = [keepRef];
  b.get('user-input').value = 'Keep my unrelated draft';
  const click = verify.onclick();
  const catalog = b.requests.find(r => r.url === '/api/v2/skills');
  b.reply(catalog, {skills: [{...skill, id: 'reference-verification'}, {...skill, description: 'approved'}]});
  await click;
  assert.equal(b.readers[1].file, original);
  b.readers[1].finish(encoded);
  await flush();
  const req = b.requests.find(r => r.url.endsWith('/turn-queue') && r.opts.method === 'POST');
  assert.ok(req);
  const payload = JSON.parse(req.opts.body);
  assert.match(payload.query, /核验.*原始 PDF/);
  assert.deepEqual(payload.selected_skills, [skill]);
  assert.deepEqual(payload.attachments, [{name: original.name, type: original.type, data: encoded}]);
  assert.deepEqual(payload.file_refs, []);
  b.reply(req, {client_request_id: payload.client_request_id, state: 'queued', queue_position: 1});
  await flush();
  assert.equal(b.get('user-input').value, 'Keep my unrelated draft');
  assert.equal(b.ctx._selectedAttachments[0], keep);
  assert.equal(b.ctx._selectedAccountFileRefs[0], keepRef);
  assert.equal(pickerChanges, 0);
  assert.equal(result.querySelector('.paper-audit-controls'), null);
  assert.equal(b.requests.some(r => r.url.includes('/reference-audits')), false);
});

test('missing exact skill does not fall back and stale session cannot submit', async () => {
  const {b, verify, result} = await review();
  let click = verify.onclick();
  b.reply(b.requests.at(-1), {skills: [{...skill, id: 'reference-verification'}]});
  await click;
  assert.match(result.querySelector('[role="status"]').textContent, /没有可用/);
  click = verify.onclick();
  const catalog = b.requests.at(-1);
  b.ctx.sessionId = 'B-session';
  b.reply(catalog, {skills: [skill]});
  await click;
  assert.equal(b.requests.some(r => r.url.endsWith('/turn-queue')), false);
});
