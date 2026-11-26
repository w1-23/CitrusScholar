'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {browser} = require('./frontend-harness.cjs');

test('one approval card drains a native batch without losing its first decision', async () => {
  const b = browser('app.js');
  const first = {approval_id: 'a', status: 'pending', tool_name: 'commit_document_index'};
  const second = {...first, approval_id: 'b'};
  b.ctx.syncPermissionCards('job', [first, second]);
  b.ctx.syncPermissionCards('job', [first, second]);
  assert.equal(b.ctx._pendingPermission.approval_id, 'a');
  assert.equal(b.ctx._permissionQueue.length, 1);
  const saving = b.ctx.decidePermission(first.tool_name, 'granted', 'once');
  assert.ok(b.requests[0].url.endsWith('/job/approvals/a'));
  assert.deepEqual(JSON.parse(b.requests[0].opts.body), {decision: 'granted'});
  b.reply(b.requests[0], {status: 'granted'});
  await saving;
  assert.equal(b.ctx._pendingPermission.approval_id, 'b');
  b.ctx.syncPermissionCards('job', [first, second]);
  assert.equal(b.ctx._permissionQueue.length, 0);
  b.ctx.settlePermissionCard('b');
  assert.equal(b.ctx._pendingPermission, null);
  b.ctx.resetPermissionCards();
  assert.equal(b.ctx._decidedPermissionIds.size, 0);
});


test('a decision event expires a queued item without disturbing the visible card', () => {
  const b = browser('app.js');
  const one = {approval_id: 'one', status: 'pending', job_id: 'untrusted-job'};
  const two = {...one, approval_id: 'two'};
  b.ctx.syncPermissionCards('owned-job', [one, two]);
  b.ctx.settlePermissionCard('two');
  assert.equal(b.ctx._pendingPermission.job_id, 'owned-job');
  assert.equal(b.ctx._pendingPermission.approval_id, 'one');
  b.ctx.syncPermissionCards('owned-job', [one, two]);
  assert.equal(b.ctx._permissionQueue.length, 0);
  b.ctx.syncPermissionCards('owned-job', []);
  assert.equal(b.ctx._pendingPermission, null);
});
