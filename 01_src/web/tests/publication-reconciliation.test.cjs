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


test('partial figure delivery names only verified and missing deliverables', () => {
  const b = browser('app.js');
  const container = b.document.createElement('div');
  const delivery = {schema_version:'artifact-delivery/1', status:'partial',
    verified_file_ids:['png-file','pdf-file'], missing_formats:['svg','private_format'],
    limitations:['unsupported_raster_svg','completion_proof_invalid'],
    reason_code:'script_outputs_incomplete'};
  const node = b.ctx.renderArtifactDelivery(container, delivery);
  assert.equal(node.hidden, false);
  assert.equal(node.textContent, '部分文件已交付 · 缺少 SVG · 已核实 2 个文件');
  assert.equal(node.dataset.verifiedCount, '2');
  const pending = b.ctx.renderArtifactDelivery(container, {...delivery,status:'unknown',verified_file_ids:[]});
  assert.equal(pending.textContent, '正在确认交付');
  assert.equal(container.querySelectorAll('.turn-delivery').length, 1);
});
