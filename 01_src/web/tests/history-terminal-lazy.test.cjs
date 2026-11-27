'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser,flush} = require('./frontend-harness.cjs');

function fixture() {
  const b = browser();
  const content = b.ctx.document.createElement('div');
  const answer = b.ctx.document.createElement('div');
  const element = b.ctx.document.createElement('div');
  content.appendChild(answer); content.appendChild(element);
  b.get('chat-container').appendChild(content);
  const events = [];
  const process = {element, receive(type,data) {events.push([type,data.seq]);}, resetDetails() {}, terminal() {}};
  b.ctx.bindSettledHistoryDetails({jobId:'settled-fixture',displayStatus:'completed'},content,answer,process);
  return {b,button:element.firstChild,answer,events};
}

test('settled history only requests real event pages on expansion', async () => {
  const {b,button,answer,events} = fixture();
  assert.equal(b.requests.length,0);
  const opened = button.onclick();
  assert.equal(b.requests[0].url,'/jobs/settled-fixture/events?after=0&limit=200');
  b.reply(b.requests[0], {job_id:'settled-fixture',protocol:'citrus-task/1',next_cursor:1,has_more:true,
    events:[{seq:1,event:'operation',data:{}}]});
  await flush();
  assert.equal(b.requests[1].url,'/jobs/settled-fixture/events?after=1&limit=200');
  b.reply(b.requests[1], {job_id:'settled-fixture',protocol:'citrus-task/1',next_cursor:2,has_more:false,
    events:[{seq:2,event:'done',data:{answer:'Recovered accepted answer'}}]});
  await opened;
  assert.deepEqual(events,[['operation',1],['done',2]]);
  assert.match(answer.innerHTML,/Recovered accepted answer/);
  assert.equal(button.disabled,true);
  await button.onclick();
  assert.equal(b.requests.length,2);
});

test('settled history distinguishes unavailable, retryable detail from a verified empty page', async () => {
  const {b,button,events} = fixture();
  const first = button.onclick();
  b.reply(b.requests[0], {detail:'unavailable'}, 503);
  await first;
  assert.match(button.textContent,/暂不可用/);
  assert.equal(button.disabled,false);
  const second = button.onclick();
  b.reply(b.requests[1], {job_id:'settled-fixture',protocol:'citrus-task/1',next_cursor:0,has_more:false,events:[]});
  await second;
  assert.match(button.textContent,/没有更多/);
  assert.deepEqual(events,[]);
});


test('history replaces its summary with ordered, generation-scoped action cards', async () => {
  const b = browser();
  const document = b.ctx.document;
  const content = document.createElement('div');
  const answer = document.createElement('div');
  const process = b.ctx.createResearchProcessPanel('Synthetic turn');
  content.appendChild(answer);
  content.appendChild(process.element);
  b.get('chat-container').appendChild(content);
  const jobId = 'history-current';
  const target = {tool_call_id: 'same-call', tool_name: 'read_tool_result'};
  process.receive('tool_result', Object.assign({status: 'failed'}, target));
  process.terminal('completed');
  b.ctx.bindSettledHistoryDetails({jobId, displayStatus: 'completed'}, content, answer, process);
  const button = process.element.children[process.element.children.length - 1];
  const result = button.onclick();
  b.reply(b.requests[0], {job_id: jobId, protocol: 'citrus-task/1', recovery_cursor: 3, next_cursor: 5,
    has_more: false, events: [
      {seq: 1, event: 'tool_result', data: Object.assign({status: 'failed'}, target)},
      {seq: 2, event: 'done', data: {answer: 'superseded', turn_envelope: {display_status: 'failed'}}},
      {seq: 3, event: 'agent_recovery', data: {status: 'admitted'}},
      {seq: 4, event: 'tool_result', data: Object.assign({status: 'complete'}, target)},
      {seq: 5, event: 'done', data: {answer: 'accepted result', turn_envelope: {display_status: 'completed'}}}
    ]});
  await result;
  assert.equal(process.projection.entries.filter(entry => entry.kind === 'action').length, 1);
  assert.equal(process.projection.entries[0].data.status, 'complete');
  assert.match(answer.innerHTML, /accepted result/);
  assert.doesNotMatch(answer.innerHTML, /superseded/);
});


const publicFailure = '主模型暂时无法连接，请稍后重试。';
const failedSnapshot = {display_status:'failed', notices:[{private:true, message:'private reason'}, publicFailure]};
function assertNotice(bot) {
  assert.equal(bot.querySelector('.turn-terminal-notice')?.textContent, publicFailure);
  assert.equal(bot.querySelectorAll('.turn-terminal-notice').length, 1);
  assert.equal(bot.querySelector('.turn-answer').textContent, '');
  assert.equal(bot.querySelector('.assistant-actions'), null);
  assert.doesNotMatch(bot.textContent, /private reason|private traceback/);
}

for (const source of ['error frame', 'terminal snapshot']) {
  test(`an accepted turn renders the same public notice from ${source} and reloaded history`, async () => {
    const b = browser();
    b.get('user-input').value = '测试公开失败说明';
    assert.equal(await b.ctx.sendMsg(), true);
    await flush();
    const admission = b.requests.find(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue'));
    const rid = JSON.parse(admission.opts.body).client_request_id;
    const job = 'failed-job';
    b.reply(admission, {client_request_id:rid, job_id:job, state:'running', dispatch_at:'started'}, 202);
    await flush();
    const replay = b.requests.find(r => r.url.startsWith(`/jobs/${job}/events?`));
    const events = source === 'error frame' ? [
      {seq:1, event:'error', data:{notice:publicFailure, detail:'private traceback', turn_envelope:{display_status:'failed'}}}
    ] : [];
    b.reply(replay, {job_id:job, protocol:'citrus-task/1', events, next_cursor:events.length, has_more:false,
      ...(events.length ? {} : {status:'failed', terminal_snapshot:failedSnapshot})});
    await flush();
    assertNotice(b.get('chat-container').querySelector('.bot'));

    const restored = browser();
    restored.ctx.renderHistoryMessages([], restored.ctx.normalizeHistoryTurns([
      {turn_request_id:rid, job_id:job, user:{content:'测试公开失败说明'}, terminal_snapshot:failedSnapshot}
    ]));
    assertNotice(restored.get('chat-container').querySelector('.bot'));
    assert.equal(restored.requests.length, 0, 'settled history needs no replay to show its public explanation');
  });
}

test('saved answers stay answers and a neighboring failed turn keeps its own notice', () => {
  const b = browser();
  b.ctx.renderHistoryMessages([], b.ctx.normalizeHistoryTurns([
    {turn_request_id:'failed', job_id:'failed', user:{content:'First'}, terminal_snapshot:failedSnapshot},
    {turn_request_id:'partial', job_id:'partial', user:{content:'Second'},
      answer:{content:'Accepted partial answer'}, terminal_snapshot:{...failedSnapshot, display_status:'partial'}}
  ]));
  const [failed, partial] = b.get('chat-container').querySelectorAll('.bot');
  assertNotice(failed);
  assert.match(partial.querySelector('.turn-answer').innerHTML, /Accepted partial answer/);
  assert.equal(partial.querySelector('.turn-terminal-notice'), null);
});


test('the exact stored stop marker is a notice, not copied or rated as an answer', () => {
  const b = browser();
  const marker = '已按你的停止请求中止本轮：上面已完成的步骤与其结果保留；被停止的步骤没有采用结果，也没有登记任何成果。';
  b.ctx.renderHistoryMessages([], b.ctx.normalizeHistoryTurns([
    {turn_request_id:'stopped', job_id:'stopped', user:{content:'Stop example'},
      answer:{content:marker}, terminal_snapshot:{display_status:'cancelled', notices:['本轮已停止']}},
    {turn_request_id:'ordinary', job_id:'ordinary', user:{content:'Quote example'},
      answer:{content:marker}, terminal_snapshot:{display_status:'completed'}}
  ]));
  const [stopped, ordinary] = b.get('chat-container').querySelectorAll('.bot');
  assert.equal(stopped.querySelector('.turn-terminal-notice').textContent, '本轮已停止');
  assert.equal(stopped.querySelector('.turn-answer').textContent, '');
  assert.equal(stopped.querySelector('.assistant-actions'), null);
  assert.equal(ordinary.querySelector('.turn-terminal-notice'), null);
  assert.match(ordinary.querySelector('.turn-answer').innerHTML, /已按你的停止请求中止本轮/);
});

test('an answer from the original segment cannot hide the latest continuation failure', () => {
  const b = browser();
  const turns = b.ctx.normalizeHistoryTurns([
    {turn_request_id:'root', job_id:'root-job', user:{content:'Root'}, answer:{content:'Prior segment answer'}},
    {turn_request_id:'child', job_id:'child-job', parent_job_id:'root-job', terminal_snapshot:failedSnapshot}
  ]);
  const merged = b.ctx.mergeQueuedHistory(turns, [{client_request_id:'root', job_id:'root-job',
    active_child_job_id:'child-job', state:'terminal', dispatch_at:'started'}]);
  b.ctx.renderHistoryMessages([], merged);
  const bots = b.get('chat-container').querySelectorAll('.bot');
  assert.equal(bots.length, 1);
  assertNotice(bots[0]);
  assert.doesNotMatch(bots[0].textContent, /Prior segment answer/);
});
