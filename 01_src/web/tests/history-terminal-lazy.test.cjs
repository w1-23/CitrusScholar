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
