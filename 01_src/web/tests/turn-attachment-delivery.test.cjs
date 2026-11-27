"use strict";
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser, flush} = require('./frontend-harness.cjs');

const input = {file_id:'input-id', filename:'question.pdf'};
const second = {file_id:'next-input-id', filename:'old-question.pdf'};
const terminal = {display_status:'completed'};
const row = (rid, attachments) => ({
  turn_request_id:rid, job_id:'job-'+rid, terminal_snapshot:terminal,
  user:{content:'Prompt '+rid, request_id:rid, attachments}, answer:{content:'Answer '+rid}
});
const continuationRequests = b => b.requests.filter(req => req.url.includes('/continuation/'));
const assistant = b => b.get('chat-container').querySelectorAll('.bot');
const continuation = node => node.querySelector('.turn-explicit-continuation');

function history(b, rows) {
  b.ctx.renderHistoryMessages([], b.ctx.normalizeHistoryTurns(rows));
}

test('only a server-confirmed source exposes an inheritance button and only explicit choice sends it', async () => {
  const b = browser();
  const previews = [];
  b.ctx.openAccountFilePreview = file => previews.push(file.file_id);
  history(b, [row('none', []), row('gone', [second]), row('eligible', [input])]);
  const bots = assistant(b);
  assert.equal(bots.some(continuation), false);
  const requests = continuationRequests(b);
  assert.deepEqual(requests.map(req => req.url), [
    '/api/v2/session/session/continuation/gone', '/api/v2/session/session/continuation/eligible'
  ]);
  b.reply(requests[0], {attachments:[]});
  b.reply(requests[1], {attachments:[input]});
  await flush();
  assert.equal(continuation(bots[0]), null);
  assert.equal(continuation(bots[1]), null);
  assert.ok(continuation(bots[2]));
  b.ctx.confirmTurnAttachmentInheritance(bots[2].querySelector('.content'));
  assert.equal(continuationRequests(b).length, 2, 'the same mounted turn is not checked twice');

  b.get('user-input').value = '独立问题';
  await b.ctx.sendMsg();
  await flush();
  const first = b.requests.find(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue'));
  assert.equal(Object.hasOwn(JSON.parse(first.opts.body), 'continue_from'), false);
  b.reply(first, {client_request_id:JSON.parse(first.opts.body).client_request_id, state:'queued'}, 202);
  await flush();
  continuation(bots[2]).fire('click');
  b.get('user-input').value = '使用已确认的文件';
  await b.ctx.sendMsg();
  await flush();
  const last = b.requests.filter(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue')).at(-1);
  assert.equal(JSON.parse(last.opts.body).continue_from, 'eligible');
  assert.deepEqual(JSON.parse(last.opts.body).file_refs, [], 'inherited files are resolved by the server');
  const users = b.get('chat-container').querySelectorAll('.user');
  const chip = users.at(-1).querySelector('.sent-attachment-reference');
  assert.equal(chip.querySelector('.sent-attachment-name').textContent, input.filename);
  assert.equal(users.at(-1).querySelector('.sent-attachment-count'), null);
  chip.onclick();
  assert.deepEqual(previews, [input.file_id]);
});

test('eligibility response from an earlier session generation cannot reveal a control', async () => {
  const b = browser();
  history(b, [row('source', [input])]);
  const [old] = assistant(b);
  const [probe] = continuationRequests(b);
  b.ctx.sessionNavigation++;
  history(b, [row('outside', [])]);
  b.reply(probe, {attachments:[input]});
  await flush();
  assert.equal(continuation(old), null);
  assert.equal(continuation(assistant(b)[0]), null);
});

test('an authorized same-turn card replaces only its duplicate answer link', () => {
  const b = browser();
  const content = b.ctx.addMessage('bot','');
  content.dataset.sessionId = 'session';
  content.dataset.requestId = 'card-turn';
  const answer = b.document.createElement('div');
  answer.className = 'turn-answer';
  content.appendChild(answer);
  const url = '/api/v2/files/out-id/download';
  for (const [href, text] of [[url,'分析结果'],['https://doi.org/10.0/example','参考文献'],['/api/v2/files/other/download','其他来源']]) {
    const anchor = b.document.createElement('a');
    anchor.setAttribute('href',href);
    anchor.textContent = text;
    answer.appendChild(anchor);
  }
  const file = {file_id:'out-id', artifact_id:'art-id', filename:'report.pdf', source_session_id:'session', turn_request_id:'card-turn'};
  b.ctx.renderArtifactDelivery(content,{schema_version:'artifact-delivery/1',status:'verified',artifacts:[{asset_ref:'art-id',url}]});
  b.ctx.appendTurnGeneratedFiles(content,[{...file,turn_request_id:'other-turn'}]);
  assert.equal(answer.querySelectorAll('a').length,3);
  b.ctx.appendTurnGeneratedFiles(content,[file]);
  b.ctx.appendTurnGeneratedFiles(content,[file]);
  const bot = assistant(b)[0];
  assert.equal(bot.querySelectorAll('.turn-file-card').length,1);
  assert.equal(bot.querySelector('.turn-files-heading').textContent,'本轮产出');
  assert.equal(bot.querySelector('.turn-file-card').querySelector('a').href, url);
  assert.match(answer.textContent,/分析结果/);
  assert.deepEqual(Array.from(answer.querySelectorAll('a'), a => a.getAttribute('href')),
    ['https://doi.org/10.0/example','/api/v2/files/other/download']);
});


test('only a source still settling gets a bounded eligibility retry', async () => {
  const b = browser();
  history(b, [row('settling', [input]), row('unavailable', [second])]);
  const [pending, unavailable] = continuationRequests(b);
  const timersBefore = new Set(b.timers.keys());
  b.reply(pending, {detail:{code:'turn_baseline_not_ready'}}, 409);
  b.reply(unavailable, {detail:{code:'turn_baseline_unavailable'}}, 409);
  await flush();
  const retries = [...b.timers].filter(([id]) => !timersBefore.has(id));
  assert.equal(retries.length, 1);
  retries[0][1]();
  const retry = continuationRequests(b).at(-1);
  assert.equal(retry.url, pending.url);
  b.reply(retry, {attachments:[input]});
  await flush();
  assert.ok(continuation(assistant(b)[0]));
  assert.equal(continuation(assistant(b)[1]), null);
});
