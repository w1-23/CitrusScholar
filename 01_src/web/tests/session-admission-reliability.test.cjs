'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {browser, flush} = require('./frontend-harness.cjs');

const latest = (b, predicate) => b.requests.filter(predicate).at(-1);
const posts = b => b.requests.filter(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue'));
const payload = request => JSON.parse(request.opts.body);
const lists = b => b.requests.filter(r => r.url.startsWith('/api/v2/sessions?'));
const row = (i, extra = {}) => ({session_id:`page-session-${i}`, title:`Session ${i}`,
  updated_at:'2025-01-01T00:00:00', msg_count:1, ...extra});

function scope(b, sid) {
  b.ctx.rememberComposerSession();
  b.ctx.disposeTaskView();
  b.ctx.sessionId = sid;
  b.ctx.sessionNavigation++;
  b.get('chat-container').replaceChildren();
  b.ctx.restoreComposerSession();
}

for (const operation of ['clear', 'delete']) {
  test(`${operation}: busy HTTP status preserves chat and explains why`, async () => {
    const b = browser();
    const notices = [];
    b.ctx.alert = value => notices.push(value);
    b.get('chat-container').textContent = 'existing history';
    const pending = operation === 'clear' ? b.ctx.clearSession(true) : b.ctx.deleteSession('session');
    const request = b.requests[0];
    b.reply(request, {detail:{code:'turn_session_busy'}}, 409);
    assert.equal(await pending, false);
    assert.equal(b.ctx.sessionId, 'session');
    assert.equal(b.get('chat-container').textContent, 'existing history');
    assert.match(notices[0], /还有待处理的消息/);
    assert.equal(b.requests.length, 1, 'must not clear or create a UI replacement');
  });
  test(`${operation}: stale success or failure cannot alter a later view`, async () => {
    for (const status of [200,409,500]) {
      const b = browser();
      let notices = 0;
      b.ctx.alert = () => notices++;
      const pending = operation === 'clear' ? b.ctx.clearSession(true) : b.ctx.deleteSession('session');
      scope(b, 'another');
      scope(b, 'session');
      b.get('chat-container').textContent = 'later mounted view';
      b.reply(b.requests[0], {}, status);
      assert.equal(await pending, false);
      assert.equal(b.get('chat-container').textContent, 'later mounted view');
      assert.equal(notices, 0);
      assert.equal(b.requests.length, 1);
    }
  });
}

test('new session verifies the response and never invokes the destructive clear endpoint', async () => {
  const b = browser();
  const attempt = b.ctx.newSession();
  b.reply(b.requests[0], {session_id:'created'}, 201);
  assert.equal(await attempt, true);
  assert.equal(b.ctx.sessionId, 'created');
  assert.equal(b.requests.some(r => r.url.endsWith('/clear')), false);
});

test('a hanging admission in A cannot block a B POST, and the deadline reconciles the same ID', async () => {
  const b = browser(); b.ctx.TURN_POST_DEADLINE_MS = 12;
  b.get('user-input').value = 'Draft A';
  await b.ctx.sendMsg(); await flush();
  const first = posts(b)[0];
  const timerA = [...b.timers.values()].at(-1);
  scope(b, 'B');
  b.get('user-input').value = 'Draft B';
  await b.ctx.sendMsg(); await flush();
  assert.deepEqual(posts(b).map(r => payload(r).session_id), ['session','B']);
  timerA(); await flush();
  assert.equal(first.opts.signal.aborted, true);
  scope(b, 'session'); await flush();
  const lookup = latest(b, r => r.url.includes('/task-by-request/'));
  assert.match(lookup.url, /\/session\/session\/task-by-request\//);
  assert.equal(lookup.url.split('/').at(-1), payload(first).client_request_id);
  b.reply(lookup, {detail:'not resolved'}, 404); await flush();
  assert.equal(posts(b).length, 2, 'uncertain outcomes never create a replacement POST');
  assert.equal(b.get('user-input').value, '', 'unknown admission is not turned into a resubmittable draft');
});

test('pre-admission 503 returning after a switch restores only the originating draft', async () => {
  const b = browser();
  b.get('user-input').value = 'keep A';
  await b.ctx.sendMsg(); await flush();
  scope(b, 'B');
  b.get('user-input').value = 'fresh B';
  b.reply(posts(b)[0], {detail:{code:'turn_queue_unavailable'}}, 503); await flush();
  assert.equal(b.get('user-input').value, 'fresh B');
  assert.equal(b.requests.filter(r => /task-by-request/.test(r.url)).length, 0);
  scope(b, 'session');
  assert.equal(b.get('user-input').value, 'keep A');
});

test('generic 503 preserves original request identity across the browser draft store', async () => {
  const b = browser();
  b.get('user-input').value = 'might have been admitted';
  await b.ctx.sendMsg(); await flush();
  const post = posts(b)[0];
  b.reply(post, {detail:'proxy unavailable'}, 503); await flush();
  const lookup = latest(b, r => r.url.includes('task-by-request'));
  const id = payload(post).client_request_id;
  assert.equal(lookup.url.split('/').at(-1), id);
  const key = b.ctx.composerDraftKey();
  assert.deepEqual(JSON.parse(b.ctx.sessionStorage.getItem(key + '.admissions')), [
    {id, text:'might have been admitted', state:'uncertain'},
  ]);
  b.ctx.composerAdmissions.clear();
  scope(b, 'B'); scope(b, 'session');
  assert.equal(latest(b, r => r.url.includes('task-by-request')).url, lookup.url);
  assert.equal(posts(b).length, 1);
});

test('sidebar loads actual cursor pages, retries failure, and merges by session identity', async () => {
  const b = browser();
  b.ctx.sessionId = 'page-session-0';
  let pending = b.ctx.loadSessions();
  assert.equal(lists(b)[0].url, '/api/v2/sessions?limit=20');
  b.reply(lists(b)[0], {sessions:[row(0,{msg_count:0,has_tasks:true}),row(1)],next_before:'cursor-A'});
  assert.equal(await pending, true);
  assert.match(b.get('session-list').innerHTML, /Session 0/);
  pending = b.ctx.loadMoreSessions();
  assert.equal(lists(b).at(-1).url, '/api/v2/sessions?limit=20&before=cursor-A');
  b.reply(lists(b).at(-1), {}, 503);
  assert.equal(await pending, false);
  assert.match(b.get('session-list').innerHTML, /会话读取失败/);
  assert.match(b.get('session-list').innerHTML, /Session 0/);
  pending = b.ctx.retrySessionsList();
  assert.equal(lists(b).at(-1).url, '/api/v2/sessions?limit=20&before=cursor-A');
  b.reply(lists(b).at(-1), {sessions:[row(1),row(2)],next_before:null});
  assert.equal(await pending, true);
  assert.deepEqual(Array.from(b.ctx._sessions, s => s.session_id), ['page-session-0','page-session-1','page-session-2']);
  assert.equal(await b.ctx.loadMoreSessions(), false);
});

test('a stale page error cannot overwrite a newer session list, empty is distinct from failure', async () => {
  const b = browser();
  const old = b.ctx.loadSessions(), replacement = b.ctx.loadSessions();
  assert.equal(lists(b)[0].opts.signal.aborted, true);
  b.reply(lists(b)[1], {sessions:[],next_before:null});
  assert.equal(await replacement, true);
  b.reply(lists(b)[0], {}, 500);
  assert.equal(await old, false);
  assert.match(b.get('session-list').innerHTML, /暂无会话/);
  assert.doesNotMatch(b.get('session-list').innerHTML, /失败/);
});
