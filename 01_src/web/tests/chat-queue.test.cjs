'use strict';
// Focused offline regressions for the durable per-session turn queue.
// Loads the production scripts with no vendor code, network or credentials.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const flush = async () => { for (let i = 0; i < 80; i++) await Promise.resolve(); };
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this.style = {setProperty(k, v) { this[k] = v; }, getPropertyValue(k) { return this[k] || ''; },
      removeProperty(k) { delete this[k]; }};
    this.dataset = {}; this.attrs = {}; this.listeners = {};
    this.className = ''; this.value = ''; this._text = ''; this.hidden = false; this.disabled = false;
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set(this.className.split(/\s+/).concat(names))].join(' ').trim(); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(n => !names.includes(n)).join(' '); },
      toggle: (name, on) => { on = on === undefined ? !this.classList.contains(name) : on; this.classList[on ? 'add' : 'remove'](name); return on; }
    };
  }
  appendChild(node) { node.remove(); this.children.push(node); node.parentNode = this; return node; }
  insertBefore(node, before) {
    if (!before) return this.appendChild(node);
    node.remove(); const index = this.children.indexOf(before);
    assert.ok(index >= 0);
    this.children.splice(index, 0, node); node.parentNode = this; return node;
  }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); this.parentNode = null; }
  replaceChildren(...nodes) { this.children.forEach(n => { n.parentNode = null; }); this.children = []; this._text = ''; nodes.forEach(n => this.appendChild(n)); }
  set textContent(text) { this.replaceChildren(); this._text = String(text); }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(''); }
  set innerHTML(html) {
    this.replaceChildren(); this._html = String(html);
    const stack = [this];
    for (const token of String(html).match(/<[^>]*>|[^<]+/g) || []) {
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (!token.startsWith('<')) { stack.at(-1)._text += token; continue; }
      const tag = token.match(/^<([a-z][\w-]*)/i); if (!tag) continue;
      const node = new Element(tag[1]);
      for (const a of token.matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(a[1], a[2]);
      stack.at(-1).appendChild(node);
      if (!/^(img|input|br|hr|meta|link)$/i.test(tag[1])) stack.push(node);
    }
  }
  get innerHTML() { return this._html || ''; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { const p = this.parentNode; return p ? p.children[p.children.indexOf(this) + 1] || null : null; }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) {
    this.attrs[name] = String(value);
    if (name === 'class') this.className = String(value);
    if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
  }
  getAttribute(name) { return this.attrs[name] === undefined ? null : this.attrs[name]; }
  removeAttribute(name) { delete this.attrs[name]; }
  matches(selector) {
    if (selector.startsWith('.')) return selector.slice(1).split('.').every(c => this.classList.contains(c));
    if (selector.startsWith('[')) {
      const m = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (!m) return false;
      const val = m[1].startsWith('data-')
        ? this.dataset[m[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())]
        : this.attrs[m[1]];
      return m[2] === undefined ? val !== undefined && val !== null : String(val) === m[2];
    }
    return this.tagName.toLowerCase() === selector;
  }
  querySelectorAll(selector) { return this.children.flatMap(n => (n.matches(selector) ? [n] : []).concat(n.querySelectorAll(selector))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { return this.matches(selector) ? this : this.parentNode ? this.parentNode.closest(selector) : null; }
  contains(node) { return node === this || this.children.some(n => n.contains(node)); }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  focus() {} scrollIntoView() {}
}

function storage() {
  const map = new Map();
  return {getItem: k => map.get(k) === undefined ? null : map.get(k), setItem: (k, v) => map.set(k, String(v)),
    removeItem: k => map.delete(k), key: i => [...map.keys()][i], get length() { return map.size; }};
}

function browser() {
  const nodes = new Map(), requests = [], timers = new Map(); let timerId = 0;
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = {readyState: 'loading', getElementById: get, createElement: tag => new Element(tag),
    querySelector: () => null, querySelectorAll: () => [], body: new Element('body'),
    documentElement: new Element('html'), addEventListener() {}};
  document.body.appendChild(get('chat-container'));
  for (const id of ['ctxInTokens', 'ctxOutTokens']) { const card = new Element(); card.className = 'ctx-token-card'; card.appendChild(get(id)); }
  new Element().appendChild(get('ctxBar'));
  const context = {console, document, AbortController, AbortSignal, DOMException, TextDecoder, URLSearchParams, navigator: {},
    marked: {Renderer: class {}, parse: text => '<p>' + String(text) + '</p>'},
    URL: class extends URL { static revokeObjectURL() {} }, FileReader: class { readAsDataURL() { if (this.onload) this.onload(); } },
    localStorage: storage(), sessionStorage: storage(), history: {replaceState() {}},
    location: {origin: 'https://chat.example.test', search: '', hash: '', pathname: '/'}, confirm: () => true, performance: {now: () => 10},
    setTimeout: (fn, ms) => { timers.set(++timerId, {fn, ms: ms || 0}); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, ms) => { timers.set(++timerId, {fn, ms: ms || 0, interval: true}); return timerId; },
    clearInterval: id => timers.delete(id),
    requestAnimationFrame: fn => { timers.set(++timerId, {fn, ms: 0}); return timerId; },
    Event: class {}, CustomEvent: class {}, addEventListener() {}, dispatchEvent() {},
    fetch: (url, opts) => { const req = {url, opts: opts || {}, settled: false, ...deferred()}; requests.push(req); return req.promise; }};
  context.window = context;
  vm.createContext(context);
  for (const file of ['task-state.js', 'task-transport.js', 'task-cards.js', 'task-controls.js', 'app.js', 'corpus_reader.js']) {
    vm.runInContext(readFileSync(join(__dirname, '../assets', file), 'utf8'), context, {filename: file});
  }
  context.currentAccount = {user_id: 'test', username: 'test'};
  context.sessionId = 'session';
  context.authEpoch = 1;
  context.accountDocumentRetired = false;
  context.historyRestoreVersion = 0;
  context.currentRoundId = 0;
  const api = {
    context, requests, timers, get, document,
    userInput: get('user-input'), chatContainer: get('chat-container'),
    last(predicate) {
      const pending = [...requests].reverse().filter(r => !r.settled);
      return typeof predicate === 'string'
        ? pending.find(r => r.url.includes(predicate))
        : pending.find(r => predicate(r));
    },
    respond(predicate, data, status) {
      const req = api.last(predicate);
      if (!req) throw new Error('no pending request matching ' + String(predicate));
      req.settled = true;
      req.resolve({ok: (status || 200) < 400, status: status || 200,
        headers: {get: () => 'application/json'}, json: async () => data});
      return req;
    },
    async runTimers() {
      const ids = [...timers.keys()];
      for (const id of ids) {
        const timer = timers.get(id);
        if (!timer) continue;
        if (!timer.interval) timers.delete(id);
        timer.fn();
        await flush();
      }
    }
  };
  return api;
}

function turnEntry(over) {
  return Object.assign({session_id: 'session', client_request_id: 'crid-test', queue_seq: 1, state: 'queued',
    active_child_job_id: '', accepted_at: 't0', dispatch_at: null, started_at: null, finished_at: null,
    updated_at: 't0', history_saved: false, clear_reason: '', job_id: 'root-1', queue_position: 1,
    status_url: '/jobs/root-1', events_url: '/jobs/root-1/events', query: '问题', attachments: []}, over || {});
}

test('ordinary send posts once to the durable turn queue and never streams /api/v2/chat', async () => {
  const b = browser();
  b.userInput.value = '核对资料并生成报告';
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  assert.ok(post, 'expected one admission POST');
  assert.equal(post.url, '/api/v2/session/session/turn-queue');
  const body = JSON.parse(post.opts.body);
  assert.equal(body.query, '核对资料并生成报告');
  assert.match(body.client_request_id, /^crid-/);
  assert.equal(b.requests.filter(r => r.url === '/api/v2/chat').length, 0);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: body.client_request_id, queue_position: 1}), 202);
  await flush();
  const userBubble = b.chatContainer.querySelector('.user');
  assert.ok(userBubble.textContent.includes('核对资料并生成报告'));
  assert.equal(userBubble.querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.equal(b.chatContainer.querySelectorAll('.turn-user-queue-state').length, 1);
  assert.ok(b.chatContainer.querySelector('.bot').classList.contains('turn-awaiting-dispatch'));
  assert.equal(b.chatContainer.querySelector('.turn-queue-cancel'), null);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
});

test('only the original user bubble shows pending and the observer waits for dispatch', async () => {
  const b = browser();
  b.userInput.value = '第三条';
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  const body = JSON.parse(post.opts.body);
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: body.client_request_id, queue_position: 3}), 202);
  await flush();
  assert.equal(b.chatContainer.querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.doesNotMatch(b.chatContainer.textContent, /第 3 位|排队中/);
  assert.ok(b.chatContainer.querySelector('.bot').classList.contains('turn-awaiting-dispatch'));
  assert.notEqual(b.chatContainer.querySelector('.research-task-line').dataset.state, 'waiting');
  assert.equal(b.requests.filter(r => r.url.includes('/events?after=0')).length, 0);

  await b.runTimers();
  const poll = b.last(r => r.url.includes('/turn-queue?limit=100'));
  assert.ok(poll, 'queued turn polls the queue');
  b.respond(r => r === poll, {turns: [turnEntry({
    client_request_id: body.client_request_id, job_id: 'root-2', state: 'claimed'})]});
  await flush();
  assert.equal(b.chatContainer.querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.ok(b.chatContainer.querySelector('.bot').classList.contains('turn-awaiting-dispatch'));
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '发送消息');
  assert.equal(b.requests.filter(r => r.url.includes('/events?after=0')).length, 0);

  await b.runTimers();
  b.respond(r => r.url.includes('/turn-queue?limit=100'), {turns: [turnEntry({
    client_request_id: body.client_request_id, job_id: 'root-2', state: 'running', active_child_job_id: ''})]});
  await flush();
  const events = b.last(r => r.url.includes('/jobs/root-2/events?after=0'));
  assert.ok(events, 'dispatched turn follows its job events');
  b.respond(r => r.url.includes('/jobs/root-2/events'), {job_id: 'root-2', protocol: 'citrus-task/1',
    events: [{seq: 1, event: 'job', data: {job_id: 'root-2', status: 'running'}}], next_cursor: 1, has_more: false});
  await flush();
  b.respond(r => r.url === '/jobs/root-2', {job_id: 'root-2', status: 'running', event_cursor: 1});
  await flush();
  assert.equal(b.chatContainer.querySelector('.turn-user-queue-state'), null);
  assert.equal(b.chatContainer.querySelector('.bot').classList.contains('turn-awaiting-dispatch'), false);
  assert.equal(b.chatContainer.querySelector('.research-task-line').dataset.state, 'waiting');
  assert.match(b.chatContainer.querySelector('.research-task-elapsed').textContent, /等待开始时间/);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
});

async function startRunningTurn(b) {
  b.userInput.value = '第一条';
  await b.context.sendMsg();
  await flush();
  const firstPost = b.last(r => r.opts.method === 'POST');
  const firstId = JSON.parse(firstPost.opts.body).client_request_id;
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: firstId, job_id: 'root-1', state: 'running'}), 202);
  await flush();
  b.respond(r => r.url.includes('/jobs/root-1/events'), {job_id: 'root-1', protocol: 'citrus-task/1',
    events: [{seq: 1, event: 'job', data: {job_id: 'root-1', status: 'running'}}], next_cursor: 1, has_more: false});
  await flush();
  b.respond(r => r.url === '/jobs/root-1', {job_id: 'root-1', status: 'running', event_cursor: 1, task_version: 3});
  await flush();

  return b.context.activeTurnComposer;
}

function pressEnter(b, options = {}) {
  let prevented = false;
  b.userInput.listeners.keydown[0]({key: 'Enter', preventDefault() { prevented = true; }, ...options});
  return prevented;
}

const posts = b => b.requests.filter(r => r.opts.method === 'POST');
const isAdmission = r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue');
const assertNoUpdates = b => assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);

test('the shared button stops the active job while Enter admits exactly one independent message', async () => {
  const b = browser();
  const binding = await startRunningTurn(b);
  const button = b.get('send-btn');
  assert.equal(button.getAttribute('aria-label'), '停止当前任务');
  assert.equal(button.classList.contains('is-stop-mode'), true);
  b.userInput.value = '下一条独立消息';

  await b.context.activateComposer();
  await b.context.activateComposer();
  await flush();
  assert.deepEqual(posts(b).map(r => r.url), [
    '/api/v2/session/session/turn-queue', '/api/v2/chat/jobs/root-1/cancel'
  ]);
  assert.equal(b.userInput.value, '下一条独立消息');
  assert.equal(button.disabled, true);
  b.respond('/jobs/root-1/cancel', {accepted: true});
  await flush();
  assert.equal(binding.stopping, true, 'HTTP acknowledgment does not confirm a terminal transition');

  assert.equal(pressEnter(b), true);
  await flush();
  const request = b.last(isAdmission);
  const id = JSON.parse(request.opts.body).client_request_id;
  assert.equal(JSON.parse(request.opts.body).query, '下一条独立消息');
  b.respond(r => r === request, turnEntry({client_request_id: id, job_id: 'follower', queue_position: 2}), 202);
  await flush();
  assert.equal(posts(b).filter(isAdmission).length, 2);
  assert.equal(b.chatContainer.querySelectorAll('.turn-user-queue-state').length, 1);
  assert.equal(b.context.activeTurnComposer, binding);
  assert.equal(b.requests.filter(r => r.url.includes('/jobs/follower/')).length, 0);
  assertNoUpdates(b);
});

test('Enter behind an active task freezes attachments, Skills, and Browse on its own queue admission', async () => {
  const b = browser();
  const binding = await startRunningTurn(b);
  b.userInput.value = '第二条';
  b.context._selectedAccountFileRefs.push({file_id: 'stored-file', name: 'data.csv'});
  b.get('browse-requested').checked = true;
  const skills = {items: [{id: 's1', version: '2', digest: 'digest'}]};
  let acknowledged;
  b.context.skillPicker = {capture: () => acknowledged ? {items: []} : skills,
    acknowledge: selection => { acknowledged = selection; }};
  assert.equal(pressEnter(b), true);
  await flush();
  assert.equal(posts(b).length, 2);
  const second = posts(b)[1];
  assert.equal(second.url, '/api/v2/session/session/turn-queue');
  const payload = JSON.parse(second.opts.body);
  assert.equal(payload.query, '第二条');
  assert.deepEqual(payload.file_refs, ['stored-file']);
  assert.deepEqual(payload.selected_skills, skills.items);
  assert.equal(payload.browse_requested, true);
  assert.equal(b.get('browse-requested').checked, false);
  assertNoUpdates(b);
  b.respond(r => r === second, turnEntry({client_request_id: payload.client_request_id,
    job_id: 'root-2', state: 'queued', queue_position: 2}), 202);
  await flush();
  const queuedUser = b.chatContainer.querySelectorAll('.user').find(n => n.textContent.includes('第二条'));
  assert.equal(queuedUser.querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.equal(acknowledged, skills);
  assert.equal(b.context.activeTurnComposer, binding);
});

test('admissions are serialized so queue order matches send order', async () => {
  const b = browser();
  b.userInput.value = '第一条';
  await b.context.sendMsg();
  await flush();
  b.userInput.value = '第二条';
  await b.context.sendMsg();
  await flush();
  let posts = b.requests.filter(r => r.opts.method === 'POST');
  assert.equal(posts.length, 1, 'second admission waits for the first');
  assert.equal(posts[0].opts.signal.aborted, false, 'second send leaves the first admission in flight');
  const firstBody = JSON.parse(posts[0].opts.body);
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: firstBody.client_request_id}), 202);
  await flush();
  posts = b.requests.filter(r => r.opts.method === 'POST');
  assert.equal(posts.length, 2);
  assert.deepEqual(posts.map(p => JSON.parse(p.opts.body).query), ['第一条', '第二条']);
  const secondBody = JSON.parse(posts[1].opts.body);
  assert.notEqual(secondBody.client_request_id, firstBody.client_request_id);
});

test('the request payload freezes query and skills at send time', async () => {
  const b = browser();
  b.context.skillPicker = {capture: () => ({items: [{id: 's1', name: '技能一'}]}), acknowledge() {}};
  b.userInput.value = '冻结内容';
  await b.context.sendMsg();
  b.context.skillPicker.capture = () => ({items: [{id: 's2', name: '技能二'}]});
  b.userInput.value = '后来编辑';
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  const body = JSON.parse(post.opts.body);
  assert.equal(body.query, '冻结内容');
  assert.deepEqual(body.selected_skills.map(item => item.id), ['s1']);
});

test('a lost admission response is reconciled by request identity without re-posting', async () => {
  const b = browser();
  b.userInput.value = '网络中断';
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  const body = JSON.parse(post.opts.body);
  post.settled = true;
  post.reject(new Error('network'));
  await flush();
  const lookupPath = '/api/v2/session/session/task-by-request/' + body.client_request_id;
  assert.ok(b.last(r => r.url === lookupPath), 'uncertain send looks up the exact request');
  b.respond(r => r.url === lookupPath, turnEntry({
    client_request_id: body.client_request_id, job_id: 'root-9', state: 'queued', queue_position: 1}));
  await flush();
  assert.equal(b.requests.filter(r => r.opts.method === 'POST').length, 1);
  assert.equal(b.chatContainer.querySelector('.turn-user-queue-state').textContent, '待发送');
});

test('reloading restores one original queued bubble without rank or a cancellation action', async () => {
  const b = browser();
  const restored = b.context.restoreSessionHistory();
  await flush();
  b.respond(r => r.url.includes('/messages?'), {messages: [], turns: [], has_more: false});
  await flush();
  b.respond(r => r.url.includes('/turn-queue?limit=100'), {turns: [turnEntry({
    client_request_id: 'q1', job_id: 'rootq', state: 'queued', queue_position: 2, query: '旧的排队消息'})]});
  await restored;
  await flush();
  assert.equal(b.chatContainer.querySelectorAll('.user').length, 1);
  const user = b.chatContainer.querySelector('.user');
  assert.match(user.textContent, /旧的排队消息/);
  assert.equal(user.querySelector('.turn-user-queue-state').textContent, '待发送');
  assert.ok(b.chatContainer.querySelector('.bot').classList.contains('turn-awaiting-dispatch'));
  assert.equal(b.chatContainer.querySelector('.turn-queue-cancel'), null);
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '发送消息');
  assert.equal(posts(b).length, 0);
});

test('a queue reconciliation hold preserves the explicit exact-request action', async () => {
  const b = browser();
  b.userInput.value = '待核对的消息';
  await b.context.sendMsg();
  await flush();
  const request = b.last(isAdmission);
  const id = JSON.parse(request.opts.body).client_request_id;
  b.respond(r => r === request, turnEntry({client_request_id: id, state: 'reconcile_required'}), 202);
  await flush();
  const action = b.chatContainer.querySelector('.turn-queue-reconcile');
  assert.equal(action.textContent, '确认并继续');
  assert.equal(b.chatContainer.querySelector('.turn-user-queue-state'), null);
  action.onclick();
  await flush();
  const expected = '/api/v2/session/session/turn-queue/' + id + '/reconcile';
  assert.deepEqual(posts(b).map(r => r.url), [request.url, expected]);
  b.respond(expected, turnEntry({client_request_id: id, state: 'terminal', clear_reason: 'reconciled'}));
  await flush();
  assert.equal(b.chatContainer.querySelector('.turn-queue-reconcile'), null);
  assertNoUpdates(b);
});

test('navigating away discards a late admission response', async () => {
  const b = browser();
  b.userInput.value = '离开的会话';
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  const body = JSON.parse(post.opts.body);
  b.context.sessionId = 'other-session';
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: body.client_request_id, state: 'running'}), 202);
  await flush();
  assert.equal(b.requests.filter(r => r.url.includes('/events')).length, 0);
});

test('a pending question leaves the ordinary send available for the next turn', async () => {
  const b = browser();
  const store = b.context.CitrusTaskState.create({});
  store.hydrate({job_id: 'job-q', status: 'running'});
  store.state.question = {id: 'q1', status: 'pending', prompt: '选择', options: []};
  const fake = {requestId: 'pending', owns: () => true, store, controls: {}, cancel() {}, stopping: false};
  b.context.activeTurnComposer = fake;
  b.context.turnQueueBindings.push(fake);
  b.userInput.value = '下一条独立消息';
  await b.context.sendMsg();
  await flush();
  const posts = b.requests.filter(r => r.opts.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/v2/session/session/turn-queue');
  assert.equal(JSON.parse(posts[0].opts.body).query, '下一条独立消息');
  assert.equal(store.state.question.status, 'pending');
  assert.equal(b.userInput.value, '');
});

test('a terminal snapshot restores Send without rerouting or dropping a draft', async () => {
  const b = browser();
  const {store} = await startRunningTurn(b);
  b.userInput.value = '完成后发送的草稿';
  store.receive('done', {answer: '已有结果', turn_envelope: {display_status: 'completed'}});
  await flush();
  assert.equal(b.get('send-btn').getAttribute('aria-label'), '发送消息');
  assert.equal(b.userInput.value, '完成后发送的草稿');
  await b.context.activateComposer();
  await flush();
  assert.equal(posts(b).length, 2);
  assert.equal(JSON.parse(b.last(isAdmission).opts.body).query, '完成后发送的草稿');
  assertNoUpdates(b);
});

test('answer UI renders API-marked final chunks, honors reset, and accepts a terminal answer', async () => {
  const b = browser();
  const {store} = await startRunningTurn(b);
  const answer = b.chatContainer.querySelector('.turn-answer');
  store.receive('answer_start', {private: true});
  store.receive('text', {content: 'unmarked'});
  await b.runTimers();
  assert.equal(answer.textContent, '');
  store.receive('answer_start', {});
  store.receive('text', {content: 'private', visibility: 'internal'});
  store.receive('text', {content: 'First '});
  await b.runTimers();
  assert.equal(answer.textContent, 'First ');
  store.receive('text', {content: 'second'});
  await b.runTimers();
  assert.equal(answer.textContent, 'First second');
  store.receive('text_reset', {});
  assert.equal(answer.textContent, '');
  store.receive('text', {content: 'stale chunk'});
  store.receive('done', {answer: 'Server final', turn_envelope: {display_status: 'completed'}});
  store.receive('answer_start', {});
  store.receive('text', {content: 'late chunk'});
  await b.runTimers();
  assert.equal(answer.style.display, '');
  assert.equal(answer.textContent, 'Server final');
  assert.doesNotMatch(answer.textContent, /stale|private/);
});

test('only API-designated citations render as answer bubbles and never as duplicate Markdown links', async () => {
  const b = browser();
  const html = readFileSync(join(__dirname, '../index.html'), 'utf8');
  const path = html.match(/<script src="([^"]*marked[^"]*)"/)[1];
  vm.runInContext(readFileSync(join(__dirname, '..', path.slice(1)), 'utf8'), b.context);
  const {store} = await startRunningTurn(b);
  const bot = b.chatContainer.querySelector('.bot');
  const answer = bot.querySelector('.turn-answer');
  const web = {kind: 'web', url: 'https://example.org/cited', title: '文献'};
  const local = {kind: 'local', reader_id: 'paper-1', title: '原文'};
  store.receive('citations', {candidates: [{...web, title: '候选不展示'}]});
  assert.equal(bot.querySelector('.turn-citation-bubbles'), null);
  store.receive('answer_start', {});
  store.receive('text', {content: '引用 [文献](https://example.org/cited#page) [本地：原文](/api/v2/corpus/source/paper-1) 以及 [其他链接](https://example.org/other)。'});
  await b.runTimers();
  store.receive('citations', {display: 'bubble_only', bubbles: [web, local, {...web, url: web.url + '#page'}, local,
    {kind: 'web', url: 'javascript:alert(1)', title: '无效链接'}]});
  await b.runTimers();
  const bubbles = bot.querySelectorAll('.turn-citation-bubble');
  assert.deepEqual(bubbles.map(n => n.textContent), ['联网：文献', '本地：原文']);
  assert.equal(bubbles[0].href, web.url);
  assert.equal(bubbles[0].rel, 'noopener noreferrer');
  const calls = [];
  b.context.CitrusCorpusReader = {openCitation: (source, scope) => calls.push([source, scope])};
  bubbles[1].listeners.click[0]();
  assert.equal(calls[0][0], local);
  assert.equal(calls[0][1].sessionId, 'session');
  assert.equal(calls[0][1].requestId, JSON.parse(posts(b)[0].opts.body).client_request_id);
  assert.deepEqual(answer.querySelectorAll('a').map(link => link.getAttribute('href')), ['https://example.org/other']);
  assert.match(answer.textContent, /文献/);
  assert.equal(bot.querySelector('.cr-turn-sources'), null);
  assert.equal(b.requests.filter(r => r.url.includes('/turn-sources?')).length, 0);
});

test('restoring a stored answer projects only its persisted citations without candidate hydration', async () => {
  const b = browser();
  const source = {kind: 'web', url: 'https://example.org/stored', title: '历史原文'};
  const restore = b.context.restoreSessionHistory();
  await flush();
  b.respond('/messages?', {turns: [{turn_request_id: 'saved-id', user: {content: '历史问题'},
    answer: {content: '已核对 [历史原文](https://example.org/stored)。', citation_bubbles: [source]},
    display_status: 'completed', candidates: [{kind: 'web', title: '不应显示的候选'}]}], messages: [], has_more: false});
  await flush();
  b.respond('/turn-queue?limit=100', {turns: []});
  await restore;
  const bot = b.chatContainer.querySelector('.bot');
  assert.equal(bot.querySelector('.turn-answer').textContent, '已核对 历史原文。');
  assert.equal(bot.querySelector('.turn-citation-bubble').href, source.url);
  assert.equal(bot.querySelectorAll('.turn-citation-bubble').length, 1);
  assert.equal(bot.querySelector('.cr-turn-sources'), null);
  assert.equal(b.requests.filter(r => r.url.includes('/turn-sources?')).length, 0);
});

test('history groups the latest continuation under its original user turn', () => {
  const {context} = browser();
  const history = context.normalizeHistoryTurns([
    {turn_request_id: 'root-request', job_id: 'root', user: {content: 'Original question'}},
    {turn_request_id: 'child-request', job_id: 'child', parent_job_id: 'root', user: {content: 'Clarification'}},
    {turn_request_id: 'last-child', job_id: 'grandchild', parent_job_id: 'child', answer: {content: 'Final answer'}},
    {turn_request_id: 'follower', job_id: 'other', user: {content: 'Next question'}},
  ]);
  const merged = context.mergeQueuedHistory(history, [turnEntry({client_request_id: 'root-request', job_id: 'root',
    active_child_job_id: 'grandchild', state: 'terminal', dispatch_at: 'started'})]);
  assert.deepEqual(Array.from(merged, turn => turn.turnRequestId), ['root-request', 'follower']);
  assert.equal(merged[0].user.content, 'Original question');
  assert.equal(merged[0].answer.content, 'Final answer');
  assert.equal(merged[0].jobId, 'grandchild');
});
