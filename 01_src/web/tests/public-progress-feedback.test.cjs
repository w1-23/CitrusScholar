'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const {browser} = require('./frontend-harness.cjs');

function mount() {
  const b = browser();
  const panel = b.ctx.createResearchProcessPanel('');
  b.get('chat-container').appendChild(panel.element);
  return {b, panel};
}
const waiting = {job_id: 'job', step_id: 'attempt-1', note_id: 'attempt-1:wait',
  origin: 'host', visibility: 'public', category: 'agent_model_wait',
  text: '已向模型发送请求，正在等待首批响应。', seq: 2};
const review = {job_id: 'job', note_id: 'review', origin: 'host', visibility: 'public',
  category: 'agent_review_evidence', text: '已收到工具结果，正在结合已有资料继续处理。', seq: 5};

test('live status prefers verified milestones or current tool family, and never estimates completion', t => {
  const {b, panel} = mount();
  t.after(panel.stopClock);
  const start = Math.floor(Date.now() / 1000) - 40;
  const state = {job_id: 'job', status: 'running', started_at: start};
  panel.syncTask(state, null, {});
  panel.receive('job', {job_id: 'job', seq: 1});
  panel.receive('assistant_progress', waiting);
  assert.equal(panel.label.textContent, waiting.text);
  assert.match(panel.elapsed.textContent, /^· 已运行 \d+ 秒$/);
  panel.receive('assistant_progress', {...waiting, origin: 'model', note_id: 'legacy', text: '历史模型工作说明。'});
  assert.equal(panel.label.textContent, waiting.text);
  panel.receive('model_attempt', {attempt_id: 'unrelated', purpose: 'main_loop', status: 'complete'});
  assert.equal(panel.label.textContent, waiting.text);
  panel.receive('model_attempt', {attempt_id: 'attempt-1', purpose: 'main_loop', status: 'complete'});
  assert.equal(panel.label.textContent, '正在处理');
  panel.receive('tool_call_start', {tool_call_id: 'step-1', display_name: '读取网页内容', args: 'private fixture'});
  assert.equal(panel.label.textContent, '正在执行 · 读取网页内容');
  panel.receive('tool_result', {tool_call_id: 'step-1', display_name: '读取网页内容', status: 'complete'});
  panel.receive('assistant_progress', review);
  assert.equal(panel.label.textContent, review.text);
  panel.receive('assistant_progress', {...waiting, seq: 89});  // Replay must not reset the current phase.
  assert.equal(panel.label.textContent, review.text);
  panel.receive('assistant_progress', {...waiting, note_id: 'attempt-2:wait', step_id: 'attempt-2', seq: 90});
  assert.equal(panel.label.textContent, waiting.text, 'a new verified attempt updates the live heading');
  assert.equal([...panel.element.querySelectorAll('.research-process-note')]
    .filter(note => note.textContent === waiting.text).length, 1, 'repeated fixed host label stays one milestone');
  assert.equal(panel.element.textContent.includes('private fixture'), false);
  b.ctx.renderTurnTimings(panel.element, {turn_timings: {visible_answer_first_ms: 2500, provider_first_packet_ms: 350}});
  assert.equal(panel.element.querySelector('.turn-timings').textContent,
    '提交到首段可见答复 2.5 秒 · 模型调用到服务商首包 0.3 秒');
  b.ctx.renderTurnTimings(panel.element, {terminal_snapshot: {turn_timings: {queue_wait_ms: 100}}});
  assert.equal(panel.element.querySelectorAll('.turn-timings').length, 1);
  panel.terminal('completed');
  panel.syncTask({...state, display_status: 'completed', finished_at: start + 45}, null, {});
  assert.equal(panel.label.textContent, '已完成');
  assert.match(panel.elapsed.textContent, /运行用时 45 秒/);
});

test('one collapsed disclosure preserves 30 attempts and stable milestone IDs live and hydrated', () => {
  const {b, panel} = mount();
  const history = [];
  function emit(type, data) {
    panel.receive(type, data);
    history.push({event: type, data, seq: history.length + 1});
  }
  const action = (call, key, attempt, status) => ({tool_call_id: call, logical_key: key, attempt, status,
    display_name: '读取网页内容'});
  emit('assistant_progress', waiting);
  emit('tool_result', action('first', 'verified-retry', 1, 'failed'));
  emit('assistant_progress', review);
  emit('tool_result', action('second', 'verified-retry', 2, 'complete'));
  for (let i = 3; i <= 30; i++) {
    emit('tool_result', action('call-' + i, 'independent-' + i, 1, i < 28 ? 'complete' : 'not_run'));
  }
  panel.terminal('completed');
  const reloaded = mount().panel;
  reloaded.terminal('completed');
  b.ctx.projectHistoryTrace(reloaded, history.slice().reverse());
  // A history preview with no operation ID must merge with the actual retry
  // events using the persisted attempt and per-call identity.
  const again = mount().panel;
  again.terminal('completed');
  b.ctx.projectHistoryTrace(again, [
    {...action('first', 'verified-retry', 1, 'failed'), kind: 'call'},
    {...action('second', 'verified-retry', 2, 'complete'), kind: 'call'},
  ]);
  b.ctx.projectHistoryTrace(again, history);
  for (const view of [panel, reloaded, again]) {
    view.receive('assistant_progress', review);
    const groups = view.element.querySelectorAll('.research-action-group');
    assert.equal(groups.length, 1);
    assert.notEqual(groups[0].open, true);
    assert.match(groups[0].querySelector('summary').textContent, /29 项操作 · 共 30 条尝试记录 · 1 次失败/);
    assert.equal(groups[0].querySelectorAll('.term-line').length, 30);
    assert.match(groups[0].querySelectorAll('.term-line')[1].textContent, /第 2 次尝试/);
    const notes = [...view.element.querySelectorAll('.research-process-note')];
    assert.deepEqual(notes.map(note => note.textContent), [waiting.text, review.text]);
  }
});


test('preparation is shown only in the status and replay cannot replace the next milestone', t => {
  const {panel} = mount();
  t.after(panel.stopClock);
  const started = {job_id: 'job', status: 'running', started_at: Math.floor(Date.now() / 1000)};
  panel.syncTask(started, null, {});
  panel.receive('job', started);
  const prepare = {job_id: 'job', note_id: 'job:prepare', origin: 'host', visibility: 'public',
    category: 'agent_prepare', text: '正在准备本轮任务'};
  panel.receive('assistant_progress', prepare);
  assert.equal(panel.label.textContent, prepare.text);
  assert.doesNotMatch(panel.body.textContent, /正在准备本轮任务/);
  panel.receive('assistant_progress', waiting);
  panel.receive('assistant_progress', prepare);
  assert.equal(panel.label.textContent, waiting.text);
  panel.receive('agent_recovery', {job_id: 'job', status: 'admitted', seq: 10});
  const resumed = {...prepare, note_id: 'job:resume-prepare', category: 'agent_resume_prepare', text: '正在准备恢复本轮任务'};
  panel.receive('assistant_progress', resumed);
  assert.equal(panel.label.textContent, resumed.text);
  assert.doesNotMatch(panel.body.textContent, /正在准备恢复本轮任务/);
  panel.terminal('completed');
  panel.receive('assistant_progress', resumed);
  assert.equal(panel.label.textContent, '已完成');
});
