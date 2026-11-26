'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {browser, login} = require('./frontend-harness.cjs');

test('publication panel only posts the selected committed file', async () => {
  const b = browser("app.js");
  login(b);
  const anchor = b.document.createElement('div');
  const operation = 'a'.repeat(32);
  const url = '/api/v2/session/session/artifacts/' + 'b'.repeat(32);
  b.ctx.appendPublicationReconciliation(anchor, 'session', 'turn');
  const panel = anchor.querySelector('details');
  assert.equal(b.requests.length, 0);
  panel.open = true;
  const loading = panel.listeners.toggle[0]();
  assert.match(b.requests[0].url, /turn\/reconcile\/publications$/);
  b.reply(b.requests[0], {files: [
    {operation_id: operation, file_index: 1, filename: 'done.txt', stage: 'confirmed', url},
    {operation_id: operation, file_index: 2, filename: 'pending.txt', stage: 'committed', can_restore: true},
    {operation_id: operation, file_index: 3, filename: 'unverified.txt', stage: 'started', url: 'https://example.test/'},
  ]});
  await loading;
  assert.equal(panel.querySelectorAll('button').length, 1);
  assert.equal(panel.querySelectorAll('a').length, 1);
  const restore = panel.querySelector('button');
  const failing = restore.onclick();
  assert.equal(b.requests[1].opts.method, 'POST');
  assert.ok(b.requests[1].url.endsWith('/' + operation + '/2'));
  b.reply(b.requests[1], {}, 409);
  await failing;
  assert.equal(restore.disabled, false);
  const restoring = restore.onclick();
  b.reply(b.requests[2], {stage: 'confirmed', url});
  await restoring;
  assert.equal(panel.querySelectorAll('button').length, 0);
  assert.equal(panel.querySelectorAll('a').length, 2);
  assert.match(panel.textContent, /未重新执行计算/);
});
