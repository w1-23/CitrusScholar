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

test('native progress identity survives recovery while interrupted tool attempts stay separate', () => {
  const process = CitrusTaskState.createProcess();
  const first = {job_id: 'job', note_id: 'todo:' + 'a'.repeat(64), visibility: 'public',
    origin: 'langgraph', source: 'write_todos', status: 'in_progress', text: 'Initial stage'};
  process.begin('job');
  assert.ok(process.note(first));
  assert.equal(process.note(first), null);
  process.action({tool_call_id: 'call', status: 'running'});
  process.begin('job/4');
  assert.equal(process.note(first), null);
  assert.ok(process.note({...first, note_id: 'todo:' + 'b'.repeat(64), text: 'After recovery'}));
  process.action({tool_call_id: 'call', status: 'running'});
  assert.deepEqual(process.entries.filter(item => item.kind === 'note').map(item => item.text), ['Initial stage', 'After recovery']);
  assert.deepEqual(process.entries.filter(item => item.kind === 'action').map(item => item.data.status), ['interrupted', 'running']);
});


test('first durable legacy cursor can catch a newer SSE frame without skipping an interior gap', async t => {
  const store = CitrusTaskState.create({request_id: 'original-client'});
  const calls = [];
  const first = {seq: 21, event: 'job', data: {job_id: job.job_id}};
  const note = {seq: 22, event: 'assistant_progress', data: {request_id: 'original-server'}};
  store.receive('done', {...terminal.data, seq: 23});
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: async url => {
    calls.push(url);
    return calls.length === 1 ? page([first], 21, {has_more: true}) : page([note], 22);
  }});
  t.after(() => transport.stop());
  await transport.replay(job.job_id);
  assert.equal(store.state.cursor, 23);
  assert.equal(CitrusTaskState.isTerminal(store.state), true);
  assert.deepEqual(calls.map(url => new URL(url, 'https://example.test').searchParams.get('after')), ['0', '21']);
});

test('online transient status and replay reads have separate recovery signals', async t => {
  const responses = [page([running], 1), reply({}, 503), reply({}, 503),
    page([output, terminal], 3, {status: 'completed', terminal_snapshot: envelope}), reply(job)];
  const store = CitrusTaskState.create();
  const reads = [];
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: async () => responses.shift(),
    onReadState: (source, failed) => { if (failed) reads.push(source); }, onStreamInterrupted: assert.fail});
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(store.state.status, 'running');
  await transport.follow(job.job_id);
  assert.equal(store.state.status, 'running');
  await transport.follow(job.job_id);
  assert.equal(store.state.status, 'completed');
  assert.deepEqual(reads, ['status', 'replay']);
});

test('one bounded session lane reaches the 33rd watch behind the first window', async t => {
  const batches = [], adopted = [];
  const observer = CitrusTaskTransport.createSessionObserver({sessionId: 'session', owns: () => true,
    url: '/queue', fetch: async url => {
      const query = new URL(url, 'https://example.test').searchParams;
      const watch = query.getAll('watch');
      batches.push([query.get('after_seq'), watch]);
      return reply({turns: watch.map(id => ({client_request_id: id, session_id: 'session',
        state: id === 'old-33' ? 'terminal' : 'running', generation: 1})), next_after_seq: 500});
    }});
  t.after(() => observer.stop());
  for (let i = 1; i <= 33; i++) observer.watch('old-' + i, row => adopted.push(row));
  await observer.snapshot();
  await observer.snapshot();
  assert.equal(batches[0][0], null);
  assert.equal(batches[0][1].length, 32);
  assert.deepEqual(batches[1], ['500', ['old-33']]);
  assert.equal(adopted.filter(row => row.state === 'terminal')[0].client_request_id, 'old-33');
});

test('SSE end discards an incomplete tail for durable replay', () => {
  const frames = [];
  const parse = CitrusTaskTransport.parser((event, data) => frames.push([event, data]));
  parse.push('event: text\r\ndata: {"content":"已"}\r\n\r\nevent: done\ndata: {"answer":"unfinished"}', false);
  parse.push('', true);
  assert.deepEqual(frames, [['text', {content: '已'}]]);
});


test('first sequenced SSE job binds its exact replay before a legacy gap is filled', async t => {
  const frames = [], requests = [];
  const store = CitrusTaskState.create({request_id: 'my-client', onEvent: event => frames.push(event)});
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: async url => {
    requests.push(url);
    return page([{...running, seq: 21}, {...terminal, seq: 22}], 22, {status: 'completed'});
  }});
  t.after(() => transport.stop());
  const sseFrame = (client, target) => 'id: 21\nevent: job\ndata: ' +
    JSON.stringify({client_request_id: client, job_id: target, status: 'running', seq: 21}) + '\n\n';
  const response = new Response(sseFrame('foreign', 'another-job') + sseFrame('my-client', job.job_id));
  await transport.stream(response, new AbortController().signal);
  assert.deepEqual(requests, ['/jobs/job-1/events?after=0&limit=200']);
  assert.deepEqual(frames, ['job', 'done']);
  assert.equal(store.state.cursor, 22);
});


test('status recovers the identified committed payload after replay lacked the final text', async t => {
  const terminalSnapshot = {...envelope, terminal_seq: 3};
  const recovered = {...terminal.data, seq: 3, turn_envelope: terminalSnapshot};
  const snapshot = {...job, terminal_snapshot: terminalSnapshot, terminal_delivery: recovered};
  const pages = [page([running, output, {seq: 3, event: 'done', data: {}}], 3, {status: 'completed', terminal_snapshot: terminalSnapshot}),
    reply(snapshot), reply({...snapshot, terminal_delivery: {...recovered, job_id: 'old-parent'}})];
  const received = [];
  const store = CitrusTaskState.create({});
  const transport = CitrusTaskTransport.create({store, owns: () => true, fetch: async () => pages.shift(),
    onTerminalDelivery: data => received.push(data.answer)});
  t.after(() => transport.stop());
  await transport.follow(job.job_id);
  assert.equal(CitrusTaskState.isTerminal(store.state), true);
  await transport.refresh(job.job_id, false);
  assert.deepEqual(received, ['Final answer']);
});
