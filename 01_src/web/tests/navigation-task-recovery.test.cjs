'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser, flush, reply, framesPage} = require("./frontend-harness.cjs");
function render(b, turns) { b.context.renderHistoryMessages([], b.context.normalizeHistoryTurns(turns)); }
const active = (id = 'job', status = 'detached') => ({turn_request_id: id + '-request', job_id: id, job_status: status});
const events = (b, job) => b.requests.findLast(r => r.url.includes('/jobs/' + job + '/events?'));
const chats = b => b.requests.filter(r => r.url === '/api/v2/chat');

test('an unsaved detached task restores one task card and stops only its exact executing job', async () => {
  const b = browser(); render(b, [active()]);
  assert.equal(b.get('chat-container').querySelectorAll('.bot').length, 1);
  assert.equal(b.get('chat-container').querySelectorAll('.user').length, 0);
  assert.equal(b.get('chat-container').querySelector('.research-task-line').dataset.state, 'waiting');
  assert.equal(b.context.activeTurnComposer.store.state.status, 'detached');
  assert.equal(b.context.currentJobId, 'job');
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '停止当前任务');
  b.get('user-input').value = '未发送的独立草稿';
  b.context.activateComposer();
  await flush();
  assert.deepEqual(b.requests.filter(r => r.opts?.method === 'POST').map(r => r.url), ['/api/v2/chat/jobs/job/cancel']);
  assert.equal(chats(b).length, 0);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
  assert.equal(b.get('user-input').value, '未发送的独立草稿');
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '正在停止当前任务');
});

test('a newer terminal turn cannot steal controls from an earlier active task', () => {
  const b = browser(); render(b, [active('running'), {...active('completed', 'completed'), terminal_snapshot: {display_status: 'completed'}, answer: {content: '已完成'}}]);
  assert.equal(b.context.currentJobId, 'running');
  assert.equal(b.context.activeTurnComposer.store.state.job_id, 'running');
  b.context.stopCurrentTask();
  assert.ok(b.requests.some(r => r.url === '/api/v2/chat/jobs/running/cancel'));
});

test('a restored queued job leaves Send available and is never the target of Stop', async () => {
  const b = browser();
  const turn = {session_id: 'session', client_request_id: 'queued-id', job_id: 'queued-job',
    query: '原有排队消息', queue_seq: 1, queue_position: 2, state: 'queued'};
  b.context.renderHistoryMessages([], b.context.mergeQueuedHistory([], [turn]));
  const chat = b.get('chat-container');
  assert.equal(chat.querySelector('.user').querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.ok(chat.querySelector('.bot').classList.contains('turn-awaiting-dispatch'));
  assert.equal(chat.querySelector('.turn-queue-cancel'), null);
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '发送消息');
  assert.equal(events(b, 'queued-job'), undefined);
  b.context.stopCurrentTask();
  b.get('user-input').value = '再提交一条';
  await b.context.activateComposer(); await flush();
  const posts = b.requests.filter(r => r.opts?.method === 'POST');
  assert.deepEqual(posts.map(r => r.url), ['/api/v2/session/session/turn-queue']);
  assert.equal(JSON.parse(posts[0].opts.body).query, '再提交一条');
  assert.equal(chats(b).length, 0);
});

test('rerender retires old observers and late replay cannot update replacement DOM', async () => {
  const b = browser(); render(b, [active('old')]);
  const oldBinding = b.context.activeTurnComposer, oldReplay = events(b, 'old');
  render(b, [active('new')]);
  assert.equal(oldBinding.owns(), false);
  assert.equal(b.context.historyTaskDisposers.length, 1);
  reply(oldReplay, framesPage('old', [[1, 'done', {answer: '迟到的旧答案', turn_envelope: {display_status: 'completed'}}]])); await flush();
  assert.doesNotMatch(b.get('chat-container').textContent, /迟到的旧答案/);
  assert.equal(b.context.currentJobId, 'new');
  assert.equal(b.requests.filter(r => r.url === '/jobs/old').length, 0);
});

test('canonical terminal releases the restored shared Stop control without a cancellation request', async () => {
  const b = browser(); render(b, [active()]);
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '停止当前任务');
  reply(events(b, 'job'), framesPage('job', [[1, 'done', {answer: '真实终答', turn_envelope: {display_status: 'completed'}}]]));
  await flush();
  const answer = b.get('chat-container').querySelector('.turn-answer');
  assert.match(answer.innerHTML, /真实终答/);
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '发送消息');
  b.context.stopCurrentTask();
  assert.equal(b.requests.filter(r => r.opts?.method === 'POST').length, 0);
});

test('session switch retires restored controls and late approval cannot appear in next session', async () => {
  const b = browser(); render(b, [active()]); const replay = events(b, 'job');
  b.context.switchSession('next');
  assert.equal(b.context.currentJobId, '');
  assert.equal(b.context.activeTurnComposer, null);
  assert.equal(b.context.historyTaskDisposers.length, 0);
  reply(replay, framesPage('job', [[1, 'permission_request', {approval_id: 'old-approval', tool_name: 'code_execute'}]])); await flush();
  assert.equal(b.document.body.querySelectorAll('.perm-card').length, 0);
  assert.equal(b.requests.filter(r => r.url.includes('/cancel')).length, 0);
});

test('late cancel acknowledgement cannot show a banner in a different session', async () => {
  const b = browser(); render(b, [active()]);
  const banners = []; b.context.showBanner = text => banners.push(text);
  b.context.stopCurrentTask();
  const cancel = b.requests.find(r => r.url === '/api/v2/chat/jobs/job/cancel');
  b.context.switchSession('next');
  reply(cancel, {accepted: false, note: 'not_in_this_process'}); await flush();
  assert.equal(banners.length, 0);
});

test('pending approvals and operation controls return through the original task snapshot', async () => {
  const b = browser(); render(b, [active()]);
  reply(events(b, 'job'), framesPage('job', [])); await flush();
  reply(b.requests.findLast(r => r.url === '/jobs/job'), {job_id: 'job', status: 'detached', approvals: [{approval_id: 'approval', tool_name: 'code_execute', status: 'pending'}]}); await flush();
  assert.equal(b.context._pendingPermission.job_id, 'job');
  assert.equal(b.context._pendingPermission.approval_id, 'approval');
  assert.equal(b.document.body.querySelectorAll('.perm-card').length, 1);
  assert.equal(chats(b).length, 0);
});


test('old file-turn action cannot jump in a resumed session after A → B → A', async () => {
  const b=browser();
  let resolveRestore, jumps=0;
  b.context.restoreSessionHistory=()=>new Promise(resolve=>{resolveRestore=resolve;});
  b.context.highlightTurnNode=()=>{jumps++; return true;};
  const paused=b.context.jumpToFileTurn('session','request');
  const finish=resolveRestore;
  b.context.switchSession('other',true);
  b.context.switchSession('session',true);
  finish(true);
  assert.equal(await paused,false);
  assert.equal(jumps,0);
});
