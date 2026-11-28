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
function native(marker, text, seq, extra = {}) {
  return {job_id: 'job', note_id: 'todo:' + marker.repeat(64), origin: 'langgraph', source: 'write_todos',
    visibility: 'public', status: 'in_progress', text, seq, ...extra};
}
const waiting = native('a', '核对两种实验方法', 2);
const review = native('b', '整理交付证据', 5);
const legacy = {job_id: 'job', note_id: 'old', origin: 'host', visibility: 'public',
  category: 'agent_model_wait', text: '已向模型发送请求，正在等待首批响应。', seq: 1};

test('native note drives waiting, tool activity takes priority, and finish clears the active stage', t => {
  const {b, panel} = mount();
  t.after(panel.stopClock);
  const start = Math.floor(Date.now() / 1000) - 40;
  const state = {job_id: 'job', status: 'running', started_at: start};
  panel.syncTask(state, null, {});
  panel.receive('job', {job_id: 'job', seq: 1});
  panel.receive('assistant_progress', legacy);
  panel.receive('tool_progress', {origin: 'host', visibility: 'public', retrieval_stage: 'full_text_read'});
  assert.equal(panel.label.textContent, '');
  assert.equal(panel.body.textContent, '');
  panel.receive('assistant_progress', waiting);
  assert.equal(panel.label.textContent, waiting.text);
  assert.match(panel.elapsed.textContent, /^· 已运行 \d+ 秒$/);
  panel.receive('assistant_progress', {...waiting, origin: 'model', text: '历史模型工作说明'});
  panel.receive('assistant_progress', native('d', '外部任务信息', 10, {job_id: 'other'}));
  panel.receive('assistant_progress', native('e', '内部工作说明', 10, {private: true}));
  assert.equal(panel.label.textContent, waiting.text);
  panel.receive('tool_call_start', {tool_call_id: 'step-1', display_name: '读取网页内容', args: 'private fixture'});
  assert.equal(panel.label.textContent, '正在执行 · 读取网页内容');
  panel.receive('assistant_progress', review);
  assert.equal(panel.label.textContent, '正在执行 · 读取网页内容');
  assert.match(panel.body.textContent, /整理交付证据/);
  panel.receive('tool_result', {tool_call_id: 'step-1', display_name: '读取网页内容', status: 'complete'});
  assert.equal(panel.label.textContent, review.text);
  panel.receive('assistant_progress', {...waiting, seq: 89});
  assert.equal(panel.label.textContent, review.text);
  const {text, ...inactive} = native('c', '', 7, {status: 'inactive'});
  panel.receive('assistant_progress', inactive);
  assert.equal(panel.label.textContent, '');
  panel.receive('assistant_progress', native('f', '旧的重放说明', 4));
  assert.equal(panel.label.textContent, '', 'a late historical note may not reactivate the heading');
  assert.equal([...panel.element.querySelectorAll('.research-process-note')]
    .filter(note => note.textContent === waiting.text).length, 1);
  assert.doesNotMatch(panel.element.textContent, /private fixture|内部工作说明|外部任务信息|首批响应/);
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
  emit('assistant_progress', legacy);
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
    {kind: 'note', ...legacy, content: legacy.text},
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


test('recovery fences the previous stage, never synthesizes a note, then shows new native progress', t => {
  const {panel} = mount();
  t.after(panel.stopClock);
  const started = {job_id: 'job', status: 'running', started_at: Math.floor(Date.now() / 1000)};
  panel.syncTask(started, null, {});
  panel.receive('job', started);
  panel.receive('assistant_progress', waiting);
  assert.equal(panel.label.textContent, waiting.text);
  panel.receive('agent_recovery', {job_id: 'job', status: 'admitted', seq: 10});
  panel.receive('assistant_progress', {...waiting, seq: 11});
  assert.equal(panel.label.textContent, '正在恢复任务');
  panel.receive('assistant_progress', {...legacy, seq: 12});
  assert.equal(panel.label.textContent, '正在恢复任务');
  const resumed = native('c', '核对附件范围', 13);
  panel.receive('assistant_progress', resumed);
  assert.equal(panel.label.textContent, resumed.text);
  panel.receive('answer_start', {seq: 14});
  assert.equal(panel.label.textContent, '');
  panel.terminal('completed');
  panel.receive('assistant_progress', resumed);
  assert.equal(panel.label.textContent, '已完成');
});

test('flat history fallback ignores old narrated rows', () => {
  const {ctx} = browser();
  const rows = [{role: 'user', content: 'Question', request_id: 'turn', message_id: 1},
    {role: 'assistant', content: legacy.text, origin: 'host', progress: true, request_id: 'turn', message_id: 2},
    {role: 'assistant', content: 'Saved answer', request_id: 'turn', message_id: 3}];
  const [turn] = ctx.groupHistoryTurns(rows);
  assert.equal(turn.answer.content, 'Saved answer');
  assert.doesNotMatch(JSON.stringify(turn), /首批响应/);
  const [unfinished] = ctx.groupHistoryTurns(rows.slice(0, -1));
  assert.equal(unfinished.answer, null);
  assert.doesNotMatch(JSON.stringify(unfinished), /首批响应/);
});


test('public figure milestones are deduplicated in live and restored timelines', () => {
  const {b, panel} = mount();
  const table = {tool_call_id: 'read-table', tool_name: 'inspect_uploaded_table', status: 'complete'};
  const figure = {tool_call_id: 'figure', tool_name: 'run_research_figure_script', status: 'complete', reason_code: 'figure_published'};
  const frames = [{event: 'tool_result', data: table, seq: 4}, {event: 'tool_result', data: figure, seq: 6}];
  for (const ev of frames) panel.receive(ev.event, ev.data);
  panel.receive('tool_result', {...figure, tool_call_id: 'repeat'});
  panel.receive('tool_result', {...figure, tool_call_id: 'draft', draft: true, summary: 'untrusted-draft'});
  panel.receive('tool_result', {...table, private: true, summary: 'private-raw'});
  const old = mount().panel;
  old.terminal('completed');
  b.ctx.projectHistoryTrace(old, frames);
  for (const view of [panel, old]) {
    assert.deepEqual([...view.body.querySelectorAll('.research-process-note')].map(x => x.textContent),
      ['已读取附件中的表格信息', '图件已渲染并通过交付校验']);
    assert.doesNotMatch(view.body.textContent, /untrusted-draft|private-raw/);
  }
});


test('narrated timeline shows ordered public labels, never raw tool metadata', t => {
  const {b, panel} = mount();
  const replay = mount().panel;
  t.after(panel.stopClock);
  t.after(replay.stopClock);
  const mark = (stage, text, seq) => ({type:'execution_narration', event_id:stage,
    visibility:'public', job_id:'job', text, seq});
  const privateBits = {tool_name:'secret_internal_name', display_name:'raw name',
    reason_code:'context_budget_exhausted', summary:'private summary',
    action_hint:'raw hint', attempt:2, elapsed_ms:9183};
  const frames = [
    ['job', {job_id:'job'}],
    ['tool_call_start', {tool_call_id:'t', status:'proposed', ...privateBits}],
    ['execution_narration', mark('read','我先查看表格。',3)],
    ['tool_executing', {tool_call_id:'t', status:'running', ...privateBits}],
    ['tool_result', {tool_call_id:'t', status:'failed', ...privateBits}],
    ['execution_narration', mark('check','接下来检查数据质量。',6)],
    ['execution_narration', mark('read','我先查看表格。',3)]
  ];
  for (const [type,data] of frames.slice(0,3)) panel.receive(type,data);
  const pending = panel.body.querySelector('.research-process-timeline');
  assert.equal(pending.children.length, 1);
  assert.equal(pending.children[0].textContent, frames[2][1].text);
  assert.equal(pending.querySelector('.research-action-group'), null);
  for (const [type,data] of frames.slice(3)) panel.receive(type,data);
  const entries = frames.slice(0,6).map(([event,data],i)=>({seq:i+1,event,data}));
  b.ctx.projectHistoryTrace(replay,entries.reverse());
  for (const view of [panel, replay]) {
    const timeline = view.body.querySelector('.research-process-timeline');
    assert.deepEqual(Array.from(timeline.children, row => row.textContent),
      ['我先查看表格。', '处理任务 · 未完成', '接下来检查数据质量。']);
    assert.doesNotMatch(view.body.textContent,
      /secret_internal_name|raw name|private summary|raw hint|第 2 次|上下文|9\.2 秒/);
    assert.equal(timeline.querySelector('.research-action-group'), null);
  }
});


test('historical terminal diagnostics do not appear beside the user answer', () => {
  const b = browser();
  const rows = b.ctx.normalizeHistoryTurns([{turn_request_id:'fixture',
    user:{content:'问题', request_id:'fixture'}, answer:{content:'可确认的结论。'},
    terminal_snapshot:{display_status:'completed',turn_timings:{provider_first_packet_ms:350,
      queue_wait_ms:100, context_maintenance_ms:990, visible_answer_first_ms:2000}}}]);
  b.ctx.renderHistoryMessages([], rows);
  const chat = b.get('chat-container');
  assert.match(chat.querySelector('.turn-answer').innerHTML, /可确认的结论/);
  assert.equal(chat.querySelector('.turn-timings'), null);
  assert.doesNotMatch(chat.textContent, /服务商首包|上下文维护用时|队列等待/);
});
