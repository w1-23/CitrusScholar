'use strict';
// F-14 wiring: the native stream surface inside the production task panel.
// The real app.js is loaded in a browser-like realm; only the DOM and network
// APIs are doubles, so these assertions exercise the shipped entry point
// (panel.receive) rather than a copy of the reducer.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {join} = require('node:path');
const {browser} = require('./frontend-harness.cjs');

const ROOT = join(__dirname, '..');
const JOB = 'job-app-stream';
const NOTE = 'stream:' + 'a'.repeat(64);
const SUB = 'sa:' + 'b'.repeat(16);
const CALL = 'mdl:' + 'c'.repeat(16);
const TODO_NOTE = 'todo:' + 'f'.repeat(64);

function frame(kind, extra, seq) {
  const status = {model_preview: 'streaming', tool_proposed: 'proposed',
    subagent_started: 'started', subagent_completed: 'completed',
    subagent_failed: 'failed', interrupt: 'requested', resume: 'resumed'}[kind];
  return Object.assign({origin: 'langgraph', source: 'native_stream', visibility: 'public',
    schema_version: 1, job_id: JOB, run_id: JOB, status, kind, at_ms: 1700000000000,
    note_id: NOTE, seq}, extra || {});
}

function nativeEvents() {
  return [
    frame('subagent_started', {subagent_id: SUB, subagent_depth: 1}, 11),
    frame('tool_proposed', {tool_call_id: 'call_1', tool_name: 'read_tool_result'}, 12),
    frame('interrupt', {interrupt_id: 'approval_1'}, 13)
  ];
}

function panel() {
  const b = browser();
  const process = b.ctx.createResearchProcessPanel('核对实验结果');
  // ownsDetails() requires the panel to live inside the chat container.
  b.ctx.chatContainer.appendChild(process.element);
  return {b, process, body: process.body};
}

function host(body) { return body.querySelector('.task-native-stream'); }
function rows(body) { const node = host(body); return node ? node.querySelectorAll('.task-native-row') : []; }
function rowKinds(body) { return rows(body).map(row => row.dataset.kind); }
function actions(body) { return body.querySelector('.research-action-list')?.querySelectorAll('.term-line') || []; }

test('legacy events keep their own surface and never create native rows', () => {
  const {process, body} = panel();
  process.receive('tool_call_start', {tool_call_id: 'call_1', tool_name: 'read_tool_result',
    logical_key: 'logic-1', started_at_ms: 1700000000000});
  process.receive('assistant_progress', {origin: 'langgraph', source: 'write_todos',
    visibility: 'public', job_id: JOB, note_id: TODO_NOTE, status: 'in_progress', text: '核对实验方法'});
  process.receive('status', {stage: 'working'});
  process.receive('context_usage', {logical_call_id: 'call-x', attempt_id: 'a', scope: 'single_call'});
  assert.equal(rows(body).length, 0, 'legacy events must not populate the native surface');
  // The pre-existing timeline still renders the public note it always rendered.
  const timeline = body.querySelector('.research-process-timeline');
  assert.ok(timeline, 'the existing process timeline is untouched');
  assert.match(timeline.textContent, /核对实验方法/);
});

test('an unknown kind or version is ignored without throwing', () => {
  const {process, body} = panel();
  assert.doesNotThrow(() => {
    process.receive('assistant_progress', Object.assign(frame('resume', {reason_code: 'answer'}, 21),
      {kind: 'future_kind'}));
    process.receive('assistant_progress', Object.assign(frame('resume', {reason_code: 'answer'}, 22),
      {schema_version: 2}));
    process.receive('assistant_progress', Object.assign(frame('resume', {reason_code: 'answer'}, 23),
      {checkpoint_ns: 'supervisor:private'}));
    process.receive('unheard_event', {job_id: JOB});
    process.receive('assistant_progress', null);
  });
  assert.equal(rows(body).length, 0);
});

test('provisional model text stays hidden by default', () => {
  const {process, body} = panel();
  process.receive('assistant_progress', frame('model_preview',
    {call_id: CALL, delta_index: 0, text: '第一段结论'}, 31));
  assert.equal(body.querySelectorAll('.task-native-preview').length, 0);
  assert.doesNotMatch(body.textContent, /第一段结论/);
  assert.equal(rows(body).length, 0);
});

test('lifecycle rows are visible in the default surface', () => {
  const {process, body} = panel();
  nativeEvents().forEach(event => process.receive('assistant_progress', event));
  assert.deepEqual(rowKinds(body), ['subagent', 'interrupt']);
  assert.equal(actions(body).length, 1);
  assert.match(actions(body)[0].textContent, /已提交/);
  const node = host(body);
  assert.equal(node.hidden, false);
  assert.equal(node.querySelectorAll('.task-native-row.is-active').length, 1);
  assert.equal(node.querySelectorAll('.task-native-row.is-proposed').length, 0);
  assert.equal(node.getAttribute('data-unrecorded'), '0');
  assert.equal(node.getAttribute('style'), null, 'rows are styled by app.css, never inline');
});

test('a replayed page appends nothing twice', () => {
  const {process, body} = panel();
  const events = nativeEvents();
  events.forEach(event => process.receive('assistant_progress', event));
  const first = rows(body).length;
  assert.equal(first, 2);
  events.forEach(event => process.receive('assistant_progress', event));
  events.slice().reverse().forEach(event => process.receive('assistant_progress', event));
  assert.equal(rows(body).length, first);
  assert.deepEqual(rowKinds(body), ['subagent', 'interrupt']);
  assert.equal(actions(body).length, 1);
});

test('a terminal frame freezes the surface and marks unrecorded endings', () => {
  const {process, body} = panel();
  nativeEvents().forEach(event => process.receive('assistant_progress', event));
  process.receive('done', {job_id: JOB, answer: '已完成', seq: 14,
    turn_envelope: {display_status: 'completed'}});
  const node = host(body);
  assert.equal(node.getAttribute('data-unrecorded'), '1');
  assert.equal(node.querySelectorAll('.task-native-row.is-unrecorded').length, 1);
  // A late native frame can no longer add a row.
  process.receive('assistant_progress', frame('tool_proposed',
    {tool_call_id: 'call_9', tool_name: 'execute'}, 15));
  assert.equal(rows(body).length, 2);
  assert.deepEqual(rowKinds(body), ['subagent', 'interrupt']);
  assert.equal(actions(body).length, 1);
});

test('legacy preview switches cannot expose provisional text', () => {
  const {b, process, body} = panel();
  b.ctx.NATIVE_STREAM_PREVIEW = true;
  process.receive('assistant_progress', frame('model_preview',
    {call_id: CALL, delta_index: 0, text: '第一段结论'}, 41));
  const preview = body.querySelector('.task-native-preview');
  assert.equal(preview, null);
  assert.doesNotMatch(body.textContent, /第一段结论/);
  process.receive('text', {content: '最终答复', delivery: 'committed', seq: 42});
  assert.equal(body.querySelectorAll('.task-native-preview').length, 0);
  process.receive('assistant_progress', frame('model_preview',
    {call_id: CALL, delta_index: 1, text: '迟到片段'}, 43));
  assert.doesNotMatch(body.textContent, /迟到片段/);
});

test('recovery reopens the native surface for the same turn', () => {
  const {process, body} = panel();
  process.receive('assistant_progress', frame('tool_proposed',
    {tool_call_id: 'call_1', tool_name: 'read_tool_result'}, 51));
  process.receive('done', {job_id: JOB, answer: '', seq: 52,
    turn_envelope: {display_status: 'failed'}});
  assert.equal(actions(body).length, 1);
  process.receive('agent_recovery', {job_id: JOB, status: 'admitted', seq: 53, recovery_cursor: 53});
  process.receive('assistant_progress', frame('tool_proposed',
    {tool_call_id: 'call_2', tool_name: 'execute'}, 54));
  assert.equal(actions(body).length, 2);
  assert.match(actions(body)[0].textContent, /未记录结束状态/);
  assert.match(actions(body)[1].textContent, /已提交/);
});

test('a client without the native projection keeps working unchanged', () => {
  const b = browser();
  delete b.ctx.CitrusTaskState.createNativeStream;
  delete b.ctx.CitrusTaskCards.renderNativeStream;
  let process;
  assert.doesNotThrow(() => {
    process = b.ctx.createResearchProcessPanel('核对实验结果');
    b.ctx.chatContainer.appendChild(process.element);
    process.receive('assistant_progress', frame('tool_proposed',
      {tool_call_id: 'call_1', tool_name: 'execute'}, 61));
    process.receive('tool_call_start', {tool_call_id: 'call_1', tool_name: 'execute',
      logical_key: 'logic-1', started_at_ms: 1700000000000});
    process.receive('done', {job_id: JOB, answer: '', seq: 62});
  });
  assert.equal(rows(process.body).length, 0);
  assert.ok(process.body.querySelector('.research-process-timeline'),
    'the legacy timeline still owns the panel');
});

test('the native surface is styled by app.css and stays reachable on small screens', () => {
  const css = fs.readFileSync(join(ROOT, 'assets', 'app.css'), 'utf8');
  for (const selector of ['.task-native-stream', '.task-native-row', '.task-native-title',
    '.task-native-detail', '.task-native-preview', '.task-native-preview-text',
    '.task-native-row.is-unrecorded', '.task-native-row.is-active']) {
    assert.ok(css.includes(selector), 'missing style for ' + selector);
  }
  assert.match(css, /@media \(max-width: 560px\)[^}]*\.task-native-stream/,
    'small screens get a compact layout');
  assert.match(css, /grid-template-columns: 10px minmax\(0, 1fr\)/,
    'rows use a shrinkable text column so long names never overflow');
});


test('one card owns proposal, execution, and receipt in either order', () => {
  const {process, body} = panel();
  process.receive('job', {job_id: JOB});
  const receipt = {job_id: JOB, tool_call_id: 'call_1', tool_name: 'read_tool_result', status: 'complete'};
  const proposed = nativeEvents()[1];
  process.receive('assistant_progress', proposed);
  const original = actions(body)[0];
  assert.match(original.textContent, /已提交/);
  process.receive('tool_call_start', Object.assign({}, receipt, {status: 'running'}));
  assert.equal(actions(body)[0], original);
  assert.match(original.textContent, /处理中/);
  process.receive('operation', Object.assign({}, receipt, {operation_id: 'op-1'}));
  process.receive('tool_result', receipt);
  process.receive('assistant_progress', proposed);
  assert.equal(actions(body).length, 1);
  assert.equal(actions(body)[0], original);
  assert.match(original.textContent, /已完成/);

  process.resetDetails();
  process.receive('job', {job_id: JOB});
  process.receive('tool_result', receipt);
  process.receive('assistant_progress', proposed);
  process.receive('tool_executing', Object.assign({}, receipt, {status: 'running'}));
  assert.equal(actions(body).length, 1);
  assert.match(actions(body)[0].textContent, /已完成/);
  assert.equal(rows(body).length, 0);

  process.receive('operation', Object.assign({}, receipt, {operation_id: 'op-1', attempt: 2, status: 'failed'}));
  assert.equal(actions(body).length, 2);
  assert.match(actions(body)[1].textContent, /第 2 次尝试/);
});

test('continuation rejects proposals from another job or run', () => {
  const {process, body} = panel();
  process.receive('job', {job_id: 'child-job'});
  process.receive('assistant_progress', nativeEvents()[1]);
  const proposal = frame('tool_proposed', {job_id: 'child-job', run_id: 'stale-run',
    tool_call_id: 'same-call', tool_name: 'read_tool_result'}, 1);
  process.receive('assistant_progress', proposal);
  assert.equal(actions(body).length, 0);
  process.receive('assistant_progress', Object.assign({}, proposal, {run_id: 'child-job'}));
  assert.equal(actions(body).length, 1);
});
