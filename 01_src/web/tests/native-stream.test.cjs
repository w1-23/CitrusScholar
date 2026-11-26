'use strict';
// F-14: native LangGraph stream projection in the browser realm. The reducer,
// the DOM renderer and the ordered task store are the production scripts; only
// the browser/network APIs are harness doubles.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {join} = require('node:path');
const {Node, browser, flush, framesPage, reply} = require('./frontend-harness.cjs');

const ROOT = join(__dirname, '..');
const JOB = 'job-native-1';
const NOTE = 'stream:' + 'a'.repeat(64);
const SUB = 'sa:' + 'b'.repeat(16);
const CALL = 'mdl:' + 'c'.repeat(16);

function load(script) {
  const b = browser(script);
  if (script === 'task-cards.js') {
    for (const name of ['task-state.js', 'task-transport.js']) {
      vm.runInContext(fs.readFileSync(join(ROOT, 'assets', name), 'utf8'), b.ctx, {filename: name});
    }
    vm.runInContext(fs.readFileSync(join(ROOT, 'assets', script), 'utf8'), b.ctx, {filename: script});
  }
  return b;
}

function frame(kind, extra) {
  const status = {model_preview: 'streaming', tool_proposed: 'proposed',
    subagent_started: 'started', subagent_completed: 'completed',
    subagent_failed: 'failed', interrupt: 'requested', resume: 'resumed'}[kind];
  return Object.assign({origin: 'langgraph', source: 'native_stream', visibility: 'public',
    schema_version: 1, job_id: JOB, run_id: JOB, status, kind, at_ms: 1700000000000,
    note_id: NOTE}, extra || {});
}

function events() {
  return [
    Object.assign(frame('subagent_started', {subagent_id: SUB, subagent_depth: 1}), {seq: 1}),
    Object.assign(frame('tool_proposed', {tool_call_id: 'call_1', tool_name: 'read_tool_result'}), {seq: 2}),
    Object.assign(frame('model_preview', {call_id: CALL, delta_index: 0, text: '第一段结论'}), {seq: 3}),
    Object.assign(frame('interrupt', {interrupt_id: 'a1b2c3d4e5f60718293a4b5c6d7e8f90'}), {seq: 4}),
    Object.assign(frame('model_preview', {call_id: CALL, delta_index: 1, text: '第二段结论'}), {seq: 5})
  ];
}

test('the native projection is a versioned, exactly keyed contract', () => {
  const b = load('task-state.js');
  const state = b.ctx.CitrusTaskState;
  assert.equal(state.nativeStreamEvent(frame('tool_proposed', {tool_call_id: 'call_1', tool_name: 'execute'})), true);
  assert.equal(state.nativeStreamEvent(frame('model_preview', {call_id: CALL, delta_index: 0, text: '结果'})), true);
  assert.equal(state.nativeStreamEvent(frame('subagent_started', {subagent_id: SUB, subagent_depth: 2})), true);
  assert.equal(state.nativeStreamEvent(frame('resume', {reason_code: 'recovery'})), true);

  // Unknown kinds and newer schema versions are ignored, never thrown on.
  assert.equal(state.nativeStreamEvent(Object.assign(frame('resume', {reason_code: 'recovery'}), {kind: 'future_kind'})), false);
  assert.equal(state.nativeStreamEvent(Object.assign(frame('resume', {reason_code: 'recovery'}), {schema_version: 2})), false);
  assert.equal(state.nativeStreamEvent(Object.assign(frame('resume', {reason_code: 'recovery'}), {status: 'resumed', extra: 1})), false);
  // Graph internals can never be rendered as a public native fact.
  for (const leaked of [{checkpoint_ns: 'supervisor:1'}, {channel: 'messages'}, {ns: ['task:1']},
    {metadata: {langgraph_node: 'supervisor'}}]) {
    assert.equal(state.nativeStreamEvent(Object.assign(frame('resume', {reason_code: 'recovery'}), leaked)), false);
  }
  assert.equal(state.nativeStreamEvent(frame('subagent_started', {subagent_id: 'task:abc', subagent_depth: 1})), false);
  assert.equal(state.nativeStreamEvent(frame('model_preview', {call_id: 'run-1', delta_index: 0, text: 'x'})), false);
  // The legacy public-todo gate is unchanged and stays separate.
  assert.equal(state.nativeProgress(frame('model_preview', {call_id: CALL, delta_index: 0, text: 'x'})), false);
});

test('replay of the same page never duplicates a shown native fact', () => {
  const b = load('task-state.js');
  const stream = b.ctx.CitrusTaskState.createNativeStream({job_id: JOB});
  events().forEach(event => assert.equal(stream.receive('assistant_progress', event), true));
  const first = stream.view();
  assert.equal(first.tools.length, 1);
  assert.equal(first.subagents.length, 1);
  assert.equal(first.subagents[0].status, 'active');
  assert.equal(first.interrupt.interrupt_id, 'a1b2c3d4e5f60718293a4b5c6d7e8f90');
  assert.equal(first.preview.text, '第一段结论第二段结论');
  assert.equal(first.preview.delta_index, 1);

  // A reconnect replays the identical frames, including out of order.
  events().slice().reverse().forEach(event => stream.receive('assistant_progress', event));
  const replayed = stream.view();
  assert.equal(replayed.tools.length, 1);
  assert.equal(replayed.subagents.length, 1);
  assert.equal(replayed.preview.text, first.preview.text);
  assert.equal(replayed.preview.delta_index, first.preview.delta_index);
});

test('the committed answer replaces the preview and terminal freezes the stream', () => {
  const b = load('task-state.js');
  const stream = b.ctx.CitrusTaskState.createNativeStream({job_id: JOB});
  events().forEach(event => stream.receive('assistant_progress', event));
  assert.equal(stream.commit(), true);
  assert.equal(stream.view().preview, null);
  // A late token cannot resurrect a preview after the answer was delivered.
  stream.receive('assistant_progress', Object.assign(frame('model_preview',
    {call_id: CALL, delta_index: 9, text: '迟到的片段'}), {seq: 6}));
  assert.equal(stream.view().preview, null);

  stream.receive('assistant_progress', Object.assign(frame('tool_proposed',
    {tool_call_id: 'call_2', tool_name: 'execute'}), {seq: 7}));
  assert.equal(stream.terminal(), true);
  const closed = stream.view();
  assert.equal(closed.closed, true);
  assert.equal(closed.preview, null);
  // No ending was recorded for the running sub-agent or the pending tool call;
  // the projection says so instead of inventing completion.
  assert.equal(closed.tools.find(item => item.tool_call_id === 'call_2').status, 'unrecorded');
  assert.equal(closed.subagents[0].status, 'unrecorded');
  // sub-agent plus both proposed tool calls ended without a recorded ending.
  assert.equal(closed.unrecorded, 3);
  assert.equal(stream.receive('assistant_progress', Object.assign(frame('resume',
    {reason_code: 'answer'}), {seq: 8})), false);
});

test('sub-agent outcomes never move backwards and are never fabricated', () => {
  const b = load('task-state.js');
  const stream = b.ctx.CitrusTaskState.createNativeStream({job_id: JOB});
  stream.receive('assistant_progress', Object.assign(frame('subagent_started',
    {subagent_id: SUB, subagent_depth: 1}), {seq: 1}));
  stream.receive('assistant_progress', Object.assign(frame('subagent_completed',
    {subagent_id: SUB, reason_code: 'completed'}), {seq: 2}));
  assert.equal(stream.view().subagents[0].status, 'completed');
  // A completed row reads as "已完成", never "已完成 · completed".
  const cards = load('task-cards.js').ctx.CitrusTaskCards;
  const model = cards.nativeStreamModel(stream.view(), {preview: false});
  assert.equal(model.rows[0].detail, '已完成');
  stream.receive('assistant_progress', Object.assign(frame('subagent_started',
    {subagent_id: SUB, subagent_depth: 1}), {seq: 3}));
  assert.equal(stream.view().subagents[0].status, 'completed');
  stream.receive('assistant_progress', Object.assign(frame('subagent_failed',
    {subagent_id: SUB, reason_code: 'failed'}), {seq: 4}));
  assert.equal(stream.view().subagents[0].status, 'completed');

  // Fresh stream, no start observed: a late terminal fact is reported as-is.
  const late = b.ctx.CitrusTaskState.createNativeStream({job_id: JOB});
  late.receive('assistant_progress', Object.assign(frame('subagent_failed',
    {subagent_id: SUB, reason_code: 'failed'}), {seq: 1}));
  assert.equal(late.view().subagents[0].status, 'failed');
  assert.equal(late.view().subagents[0].started_seq, 0);
  // No native frame can create an ending for a sub-agent that never reported one.
  const only = b.ctx.CitrusTaskState.createNativeStream({job_id: JOB});
  only.receive('assistant_progress', Object.assign(frame('subagent_started',
    {subagent_id: SUB, subagent_depth: 1}), {seq: 1}));
  assert.equal(JSON.stringify(only.view().subagents.map(item => item.status)), JSON.stringify(['active']));
});

test('the ordered task store applies native frames once across a replayed page', async () => {
  const b = load('task-state.js');
  const store = b.ctx.CitrusTaskState.create({request_id: 'request-1'});
  const page = events();
  page.forEach(event => store.receive('assistant_progress',
    Object.assign({}, event, {job_id: JOB}), String(event.seq)));
  assert.equal(store.state.cursor, 5);
  assert.equal(store.state.native_stream.tools.length, 1);
  assert.equal(store.state.native_stream.subagents.length, 1);
  assert.equal(store.state.native_stream.preview.text, '第一段结论第二段结论');

  // Replaying the same sequence numbers is refused by the cursor, and a gap
  // holds later frames until the missing one arrives.
  page.forEach(event => store.receive('assistant_progress',
    Object.assign({}, event, {job_id: JOB}), String(event.seq)));
  assert.equal(store.state.native_stream.preview.delta_index, 1);
  const gap = Object.assign(frame('tool_proposed', {tool_call_id: 'call_3', tool_name: 'execute'}), {seq: 7, job_id: JOB});
  assert.equal(store.receive('assistant_progress', gap, '7'), true);
  assert.equal(store.hasGap(), true);
  assert.equal(store.state.native_stream.tools.length, 1);
  store.receive('assistant_progress',
    Object.assign(frame('tool_proposed', {tool_call_id: 'call_4', tool_name: 'execute'}), {seq: 6, job_id: JOB}), '6');
  assert.equal(store.hasGap(), false);
  assert.equal(JSON.stringify(store.state.native_stream.tools.map(item => item.tool_call_id)),
    JSON.stringify(['call_1', 'call_4', 'call_3']));
});

test('committed text and terminal events reach the same store projection', () => {
  const b = load('task-state.js');
  const store = b.ctx.CitrusTaskState.create({request_id: 'request-1'});
  events().forEach(event => store.receive('assistant_progress',
    Object.assign({}, event, {job_id: JOB}), String(event.seq)));
  store.receive('text', {content: '最终答复', delivery: 'committed', seq: 6}, '6');
  assert.equal(store.state.native_stream.preview, null);
  store.receive('done', {job_id: JOB, answer: '最终答复', seq: 7,
    turn_envelope: {display_status: 'completed'}}, '7');
  assert.equal(store.state.native_stream.closed, true);
  assert.equal(store.state.native_stream.unrecorded, 2);
  assert.equal(store.state.display_status, 'completed');
});

test('the renderer shows lifecycle rows, the live preview and unrecorded endings', () => {
  const b = load('task-cards.js');
  const state = b.ctx.CitrusTaskState;
  const cards = b.ctx.CitrusTaskCards;
  const stream = state.createNativeStream({job_id: JOB});
  events().forEach(event => stream.receive('assistant_progress', event));
  const model = cards.nativeStreamModel(stream.view(), {
    toolName: name => name === 'read_tool_result' ? '查看工具记录' : name,
    clean: text => String(text)
  });
  assert.equal(JSON.stringify(model.rows.map(row => row.kind)),
    JSON.stringify(['subagent', 'tool', 'interrupt']));
  assert.equal(model.rows[1].title, '查看工具记录');
  assert.equal(model.rows[0].detail, '执行中');
  assert.equal(model.previewing, true);
  assert.equal(model.preview, '第一段结论第二段结论');
  // A product may hide provisional model narration and keep lifecycle rows.
  const quiet = cards.nativeStreamModel(stream.view(), {preview: false});
  assert.equal(quiet.previewing, false);
  assert.equal(quiet.rows.length, 3);

  const container = new Node('div');
  const host = cards.renderNativeStream(container, model);
  assert.equal(host.querySelectorAll('.task-native-row').length, 3);
  assert.equal(host.querySelector('.task-native-row.is-active').textContent.startsWith('子任务'), true);
  assert.equal(host.querySelector('.task-native-preview').querySelector('.task-native-preview-text').textContent,
    '第一段结论第二段结论');
  assert.equal(host.getAttribute('data-unrecorded'), '0');
  assert.equal(host.hidden, false);

  // Terminal state: preview gone, running work marked as unrecorded.
  stream.terminal();
  const closed = cards.nativeStreamModel(stream.view(), {clean: text => String(text)});
  assert.equal(closed.previewing, false);
  assert.equal(closed.rows.filter(row => row.status === 'unrecorded').length, 2);
  const closedHost = cards.renderNativeStream(container, closed);
  assert.equal(closedHost.querySelectorAll('.task-native-preview').length, 0);
  assert.equal(closedHost.getAttribute('data-unrecorded'), '2');

  // An unknown/other payload produces no native surface at all.
  assert.equal(cards.nativeStreamModel(null), null);
  assert.equal(cards.nativeStreamModel({schema_version: 2, rows: []}), null);
});

test('unknown native kinds stay invisible to the legacy progress surface', () => {
  const b = load('task-cards.js');
  const state = b.ctx.CitrusTaskState;
  const future = Object.assign(frame('resume', {reason_code: 'answer'}), {kind: 'model_reasoning_summary'});
  assert.equal(state.nativeStreamEvent(future), false);
  const stream = state.createNativeStream({job_id: JOB});
  assert.equal(stream.receive('assistant_progress', future), false);
  assert.deepEqual(stream.view().rows, undefined);
  assert.equal(stream.view().resume, null);
});
