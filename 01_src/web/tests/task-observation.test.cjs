const test = require('node:test');
const assert = require('node:assert/strict');
require('../assets/task-state.js');
require('../assets/task-transport.js');

const envelope = {display_status: 'completed'};
const job = {job_id: 'job-1', status: 'completed', event_cursor: 3, terminal_snapshot: envelope};
const terminal = {seq: 3, event: 'done', data: {job_id: job.job_id, answer: 'Final answer', artifact_ids: ['file-1'], turn_envelope: envelope}};
const running = {seq: 1, event: 'job', data: {job_id: job.job_id, status: 'running'}};
const output = {seq: 2, event: 'answer_start', data: {}};

function reply(data, status = 200) {
  return {json: async () => data, ok: status === 200, status};
}

function page(events, next, snapshot = {}) {
  return reply({job_id: job.job_id, protocol: 'citrus-task/1', events, next_cursor: next, has_more: false, ...snapshot});
}

function observation(responses, owns = () => true) {
  const requests = [], frames = [], errors = [];
  const store = CitrusTaskState.create({onEvent: (type, data) => frames.push({type, data})});
  const transport = CitrusTaskTransport.create({store, owns, onError: error => errors.push(error),
    fetch: async (url, options) => {
      requests.push([url, options.method || 'GET']);
      assert.ok(responses.length, `unexpected ${url}`);
      return responses.shift();
    }});
  return {requests, frames, errors, store, transport};
}

test('terminal between replay and status delivers answer and files once', async t => {
  const {transport, store, frames, requests} = observation([
    page([running], 1, {status: 'running'}), reply(job),
    page([running, output, terminal], 3, {status: job.status, terminal_snapshot: envelope})
  ]);
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(store.state.cursor, 3);
  assert.equal(CitrusTaskState.isTerminal(store.state), true);
  assert.deepEqual(frames.filter(frame => frame.type === 'done'), [{type: 'done', data: {...terminal.data, seq: 3}}]);
  assert.deepEqual(requests, [
    ['/jobs/job-1/events?after=0&limit=200', 'GET'], ['/jobs/job-1', 'GET'],
    ['/jobs/job-1/events?after=1&limit=200', 'GET']
  ]);
});

test('a missing terminal page is retryable and never hydrates ahead of consumption', async t => {
  const {transport, store, frames, errors} = observation([
    page([running], 1, {status: 'running'}), reply(job), page([], 1, {status: 'running'}),
    page([terminal], 3, {status: job.status, terminal_snapshot: envelope}),
    page([output, terminal], 3, {status: job.status, terminal_snapshot: envelope}), reply(job)
  ]);
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(store.state.cursor, 1);
  assert.equal(CitrusTaskState.isTerminal(store.state), false);
  assert.equal(errors.length, 1);
  await transport.follow(job.job_id);
  assert.equal(store.state.cursor, 1);
  assert.equal(CitrusTaskState.isTerminal(store.state), false);
  assert.equal(errors.length, 2);
  await transport.follow(job.job_id);
  assert.equal(store.state.cursor, 3);
  assert.equal(frames.filter(frame => frame.type === 'done').length, 1);
});

test('a lost first frame discovers only the original request and never submits', async t => {
  const {transport, store, requests} = observation([
    reply({job_id: job.job_id}), page([running, output, terminal], 3, {terminal_snapshot: envelope}), reply(job)
  ]);
  t.after(() => transport.stop());
  const found = [];
  await transport.followRequest('session a', 'request-1', id => found.push(id), assert.fail);
  assert.deepEqual(found, [job.job_id]);
  assert.equal(store.state.cursor, 3);
  assert.equal(CitrusTaskState.isTerminal(store.state), true);
  assert.deepEqual(requests[0], ['/api/v2/session/session%20a/task-by-request/request-1', 'GET']);
  assert.ok(requests.every(([, method]) => method === 'GET'));
});

test('stopping an observer aborts its read and ignores a late claim after navigation', async () => {
  let signal, complete;
  const store = CitrusTaskState.create();
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: (_, opts) => {
    signal = opts.signal;
    return new Promise(resolve => { complete = resolve; });
  }});
  const lookup = transport.followRequest('session-a', 'request-1', assert.fail, assert.fail);
  transport.stop();
  assert.equal(signal.aborted, true);
  complete(reply({job_id: job.job_id}));
  await lookup;
  assert.equal(store.state.job_id, '');
});

test('running recovery between replay and status supersedes an older terminal event', async t => {
  const recovered = {seq: 4, event: 'agent_recovery', data: {job_id: job.job_id, status: 'admitted'}};
  const {transport, store, requests} = observation([
    page([running, output, terminal], 3, {status: 'completed', terminal_snapshot: envelope}),
    reply({job_id: job.job_id, status: 'running', event_cursor: 4, terminal_snapshot: {}}),
    page([recovered], 4, {status: 'running', terminal_snapshot: {}})
  ]);
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(store.state.cursor, 4);
  assert.equal(store.state.status, 'running');
  assert.equal(CitrusTaskState.isTerminal(store.state), false);
  assert.deepEqual(requests.slice(-1), [['/jobs/job-1/events?after=3&limit=200', 'GET']]);
});

test('an admission failure without an envelope stops polling after replay', async t => {
  const failed = {job_id: job.job_id, event_cursor: 0, status: 'failed', terminal_snapshot: {}};
  const {transport, store, frames, requests} = observation([page([], 0, {status: 'failed'}), reply(failed)]);
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(store.state.display_status, 'failed');
  assert.equal(CitrusTaskState.isTerminal(store.state), true);
  assert.equal(store.state.terminal_snapshot, null);
  assert.deepEqual(frames, []);
  assert.equal(requests.length, 2);
});


test('continuation starts at cursor zero and ignores the retired job response', async t => {
  const requests = [], received = [];
  let lateResponse, oldSignal;
  const store = CitrusTaskState.create({onEvent: (_, data) => received.push(data)});
  store.receive('job', {...running.data, seq: 1});
  const child = {job_id: 'child', status: 'completed', event_cursor: 1, terminal_snapshot: envelope};
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: (url, options) => {
    requests.push(url);
    if (url.startsWith('/jobs/job-1/')) {
      oldSignal = options.signal;
      return new Promise(resolve => { lateResponse = resolve; });
    }
    if (url.includes('/events')) return reply({job_id: child.job_id, protocol: 'citrus-task/1',
      events: [{seq: 1, event: 'done', data: {...terminal.data, job_id: child.job_id, answer: 'Child answer'}}],
      next_cursor: 1, has_more: false});
    return reply(child);
  }});
  t.after(() => transport.stop());
  const old = transport.follow(job.job_id);
  await transport.followContinuation(child.job_id);
  assert.equal(oldSignal.aborted, true);
  lateResponse(page([output, terminal], 3));
  await old;
  assert.deepEqual(requests, ['/jobs/job-1/events?after=1&limit=200', '/jobs/child/events?after=0&limit=200', '/jobs/child']);
  assert.equal(store.state.job_id, child.job_id);
  assert.equal(store.state.cursor, 1);
  assert.deepEqual(received.filter(data => data.answer).map(data => data.answer), ['Child answer']);
});


test('one session read multiplexes watched requests and retains backfilled terminal status', async t => {
  const replies = [], calls = [], seen = [], available = [];
  let release;
  const turn = (id, state, generation = 1) => ({client_request_id: id, session_id: 's', state, generation});
  const observer = CitrusTaskTransport.createSessionObserver({url: '/queue', sessionId: 's', owns: () => true,
    onAvailability: value => available.push(value), fetch: (url, {signal}) => {
      calls.push({url, signal});
      if (replies.length) return replies.shift();
      return new Promise(resolve => { release = resolve; });
    }});
  t.after(() => observer.stop());
  observer.watch('request a', entry => seen.push(entry));
  observer.watch('request/b', entry => seen.push(entry));
  const first = observer.snapshot();
  assert.equal(first, observer.snapshot());
  await Promise.resolve();
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\?limit=100&watch=request%20a&watch=request%2Fb$/);
  release(reply({turns: [turn('request a', 'running'), turn('request/b', 'queued')], next_after_seq: 99}));
  assert.equal((await first).length, 2);
  replies.push(reply({turns: [turn('request a', 'terminal', 2), turn('request/b', 'running')], next_after_seq: 99}));
  assert.equal((await observer.snapshot())[0].state, 'terminal');
  assert.match(calls[1].url, /after_seq=99&watch=request%20a&watch=request%2Fb$/);
  replies.push(reply({turns: [turn('request a', 'running', 1), turn('request/b', 'terminal')], next_after_seq: 100}));
  assert.equal((await observer.snapshot())[0].state, 'terminal');
  assert.deepEqual(available, [true, true, false]);
  assert.deepEqual(seen.map(item => item.state), ['running', 'queued', 'terminal', 'running', 'terminal']);
  const last = observer.snapshot();
  await Promise.resolve();
  observer.stop();
  assert.equal(calls.at(-1).signal.aborted, true);
  release(reply({turns: [turn('request a', 'running', 3)], next_after_seq: 101}));
  assert.equal(await last, null);
  assert.equal(seen.length, 5);
});

test('replay consumes but never projects obsolete generation frames', async t => {
  const recovery = {seq: 3, event: 'agent_recovery', data: {job_id: job.job_id, status: 'admitted'}};
  const oldQuestion = {seq: 2, event: 'question', data: {question: {id: 'old', status: 'pending'}}};
  const newQuestion = {seq: 4, event: 'question', data: {question: {id: 'current', status: 'pending'}}};
  const {transport, store, frames} = observation([
    page([running, oldQuestion, recovery, newQuestion], 4, {status: 'awaiting_input', recovery_cursor: 3})
  ]);
  t.after(() => transport.stop());
  await transport.replay(job.job_id);
  assert.equal(store.state.cursor, 4);
  assert.equal(store.state.recovery_cursor, 3);
  assert.equal(store.state.question.id, 'current');
  assert.deepEqual(frames.map(frame => frame.type), ['agent_recovery', 'question']);
  assert.equal(store.hydrate({job_id: job.job_id, event_cursor: 4, recovery_cursor: 0,
    question: {id: 'old', status: 'pending'}}), false);
});

test('public progress identities deduplicate only within one generation', () => {
  const process = CitrusTaskState.createProcess();
  const metadata = {note_id: 'same', visibility: 'public', origin: 'host', category: 'model_thinking'};
  process.begin('job');
  assert.ok(process.note('one', null, metadata));
  assert.equal(process.note('duplicate', null, metadata), null);
  process.action({tool_call_id: 'call', status: 'running'});
  process.begin('job/4');
  assert.ok(process.note('after recovery', null, metadata));
  process.action({tool_call_id: 'call', status: 'running'});
  assert.deepEqual(process.entries.filter(item => item.kind === 'note').map(item => item.text), ['one', 'after recovery']);
  assert.deepEqual(process.entries.filter(item => item.kind === 'action').map(item => item.data.status), ['interrupted', 'running']);
});
