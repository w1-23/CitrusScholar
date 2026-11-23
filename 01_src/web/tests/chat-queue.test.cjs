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
    location: {search: '', hash: '', pathname: '/'}, performance: {now: () => 10},
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
  const state = b.chatContainer.querySelector('.turn-queue-state');
  assert.ok(state, 'pending bubble exposes queue state');
  assert.match(state.textContent, /排队中/);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
});

test('a queued turn reports its position and only follows the job after dispatch', async () => {
  const b = browser();
  b.userInput.value = '第三条';
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  const body = JSON.parse(post.opts.body);
  b.respond(r => r.opts.method === 'POST', turnEntry({client_request_id: body.client_request_id, queue_position: 3}), 202);
  await flush();
  assert.match(b.chatContainer.querySelector('.turn-queue-state').textContent, /第 3 位/);
  assert.notEqual(b.chatContainer.querySelector('.research-task-line').dataset.state, 'waiting');
  assert.equal(b.requests.filter(r => r.url.includes('/events?after=0')).length, 0);

  await b.runTimers();
  const poll = b.last(r => r.url.includes('/turn-queue?limit=100'));
  assert.ok(poll, 'queued turn polls the queue');
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
  assert.equal(b.chatContainer.querySelector('.turn-queue-state').hidden, true);
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

function clickComposerMode(b) {
  b.get('composer-new-message').listeners.click[0]();
}

test('the composer targets the executing job and shows receipt separately from application', async () => {
  const b = browser();
  const binding = await startRunningTurn(b);
  assert.equal(b.get('composer-new-message').textContent, '作为新任务');
  assert.match(b.get('composer-task-target').textContent, /第一条.*任务版本 3/);
  assert.equal(b.get('composer-task-target').title, 'root-1');
  assert.equal(b.chatContainer.querySelector('.task-update-toggle'), null);
  assert.equal(b.get('input-tools-menu').hidden, true);
  b.userInput.value = '先核对数据单位';
  const submitted = b.context.sendMsg();
  await flush();
  const update = b.last(r => r.opts.method === 'POST' && r.url.endsWith('/updates'));
  assert.equal(update.url, '/api/v2/chat/jobs/root-1/updates');
  assert.deepEqual(JSON.parse(update.opts.body), {
    update_request_id: JSON.parse(update.opts.body).update_request_id,
    expected_version: 3, instruction: '先核对数据单位'
  });
  assert.equal(b.requests.filter(r => r.opts.method === 'POST' && r.url.endsWith('/turn-queue')).length, 1);
  b.respond(r => r === update, {job_id: 'root-1', version: 4, status: 'pending'});
  await submitted;
  const status = b.chatContainer.querySelector('.task-control-status');
  assert.match(status.textContent, /已接收第 4 版补充，等待应用/);
  assert.equal(b.userInput.value, '');
  assert.match(b.get('composer-task-target').textContent, /任务版本 4/);
  binding.store.receive('task_update', {job_id: 'root-1', version: 4, status: 'applied'});
  assert.match(status.textContent, /已应用第 4 版/);
  assert.equal(b.context.sessionStorage.getItem(b.context.composerDraftKey('root-1') + '.received'), null);
});

test('the new-task override queues the draft with its attachments, Skills, and browse choice', async () => {
  const b = browser();
  await startRunningTurn(b);
  b.userInput.value = '第二条';
  clickComposerMode(b);
  assert.equal(b.userInput.value, '第二条');
  assert.equal(b.get('input-tools-menu').hidden, false);
  assert.equal(b.get('composer-new-message').textContent, '补充当前任务');
  b.context._selectedAccountFileRefs.push({file_id: 'stored-file', name: 'data.csv'});
  b.get('browse-requested').checked = true;
  const skills = {items: [{id: 's1', version: '2', digest: 'digest'}]};
  let acknowledged;
  b.context.skillPicker = {capture: () => acknowledged ? {items: []} : skills,
    acknowledge: selection => { acknowledged = selection; }};
  await b.context.sendMsg();
  await flush();
  const posts = b.requests.filter(r => r.opts.method === 'POST');
  assert.equal(posts.length, 2);
  const second = posts[posts.length - 1];
  assert.equal(second.url, '/api/v2/session/session/turn-queue');
  const payload = JSON.parse(second.opts.body);
  assert.equal(payload.query, '第二条');
  assert.deepEqual(payload.file_refs, ['stored-file']);
  assert.deepEqual(payload.selected_skills, skills.items);
  assert.equal(payload.browse_requested, true);
  assert.equal(b.requests.filter(r => r.url.includes('/updates')).length, 0);
  const secondId = JSON.parse(second.opts.body).client_request_id;
  b.respond(r => r.opts.method === 'POST' && JSON.parse(r.opts.body).client_request_id === secondId,
    turnEntry({client_request_id: secondId, job_id: 'root-2', state: 'queued', queue_position: 2}), 202);
  await flush();
  assert.equal(b.chatContainer.querySelectorAll('[data-request-id]').filter(n => n.dataset.requestId === secondId).length > 0, true);
  assert.equal(acknowledged, skills);
  assert.equal(b.get('composer-task-target').title, 'root-1');
  assert.equal(b.get('composer-new-message').textContent, '作为新任务');
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
  assert.match(b.chatContainer.querySelector('.turn-queue-state').textContent, /排队中/);
});

test('reloading renders queued turns from the queue listing and cancels one by id', async () => {
  const b = browser();
  const restored = b.context.restoreSessionHistory();
  await flush();
  b.respond(r => r.url.includes('/messages?'), {messages: [], turns: [], has_more: false});
  await flush();
  b.respond(r => r.url.includes('/turn-queue?limit=100'), {turns: [turnEntry({
    client_request_id: 'q1', job_id: 'rootq', state: 'queued', queue_position: 2, query: '旧的排队消息'})]});
  await restored;
  await flush();
  const statusNode = b.chatContainer.querySelectorAll('[data-request-id]')
    .filter(n => n.dataset.requestId === 'q1')
    .map(n => n.querySelector('.turn-queue-state')).find(Boolean);
  assert.ok(statusNode, 'restored turn shows its queue state');
  assert.match(statusNode.textContent, /排队中/);
  const cancel = statusNode.querySelector('.turn-queue-cancel');
  assert.ok(cancel);
  cancel.onclick();
  await flush();
  const del = b.last(r => r.opts.method === 'DELETE');
  assert.ok(del, 'cancel deletes the exact queued turn');
  assert.match(del.url, /\/turn-queue\/q1$/);
  b.respond(r => r.opts.method === 'DELETE', turnEntry({
    client_request_id: 'q1', job_id: 'rootq', state: 'terminal', clear_reason: 'cancelled', queue_position: 0}));
  await flush();
  assert.match(statusNode.textContent, /已取消/);
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

function observedTask(b, job, transport, taskVersion = 1) {
  const store = b.context.CitrusTaskState.create({});
  store.hydrate({job_id: job, status: 'running', task_version: taskVersion});
  const container = new Element('div');
  const controls = b.context.CitrusTaskControls.create({container, store, owns: () => true,
    readDraft: b.context.readSupplementDraft, onReconcile: b.context.reconcileSupplement,
    onChange: b.context.selectActiveTurn, transport: {refresh: async () => {}, ...transport}});
  b.context.registerTurnBinding({query: job, store, controls, owns: () => true});
  return {store, controls};
}

test('clearing a preserved new-task draft restores the active-task default', async () => {
  const b = browser(), job = 'active-with-existing-draft';
  const updates = [];
  b.userInput.value = '正在写的独立草稿';
  observedTask(b, job, {post: async (target, path, payload) => {
    updates.push({target, path, payload});
    return {job_id: job, version: 2, status: 'applied'};
  }});
  assert.equal(b.userInput.value, '正在写的独立草稿');
  assert.equal(b.get('composer-new-message').textContent, '补充当前任务');
  b.userInput.value = '';
  b.userInput.listeners.input[0]();
  assert.equal(b.get('composer-new-message').textContent, '作为新任务');
  assert.equal(b.get('composer-task-target').title, job);
  assert.equal(b.chatContainer.querySelector('.task-update-toggle'), null);
  b.userInput.value = '补充检查单位';
  b.userInput.listeners.keydown[0]({key: 'Enter', preventDefault() {}});
  await flush();
  assert.equal(updates.length, 1);
  assert.equal(updates[0].target, job);
  assert.equal(updates[0].path, 'updates');
  assert.equal(updates[0].payload.instruction, '补充检查单位');
  assert.equal(b.requests.filter(r => r.opts.method === 'POST').length, 0);
});

test('clearing the last new-task option restores the active-task default', () => {
  const b = browser(), job = 'active-with-browse-draft';
  const browse = b.get('browse-requested');
  browse.checked = true;
  observedTask(b, job, {});
  assert.equal(b.get('composer-new-message').textContent, '补充当前任务');
  assert.equal(browse.checked, true);
  browse.checked = false;
  browse.listeners.change[0]();
  assert.equal(b.get('composer-new-message').textContent, '作为新任务');
  assert.equal(b.get('composer-task-target').title, job);
});

test('the explicit new-task override stays selected when its draft is empty', async () => {
  const b = browser(), job = 'active-with-explicit-override';
  observedTask(b, job, {post: () => assert.fail('new-task send must not update the running task')});
  clickComposerMode(b);
  b.userInput.listeners.input[0]();
  assert.equal(b.userInput.value, '');
  assert.equal(b.get('composer-new-message').textContent, '补充当前任务');
  b.userInput.value = '独立问题';
  b.userInput.listeners.keydown[0]({key: 'Enter', preventDefault() {}});
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  assert.equal(post.url, '/api/v2/session/session/turn-queue');
  assert.equal(JSON.parse(post.opts.body).query, '独立问题');
});

test('an update receipt keeps a draft edited back to the same text', async () => {
  const b = browser(), job = 'active-with-retyped-draft', response = deferred();
  observedTask(b, job, {post: () => response.promise});
  b.userInput.value = '核对单位';
  const sending = b.context.sendMsg();
  b.userInput.value = '';
  b.userInput.listeners.input[0]();
  b.userInput.value = '核对单位';
  b.userInput.listeners.input[0]();
  response.resolve({job_id: job, version: 2, status: 'applied'});
  await sending;
  assert.equal(b.userInput.value, '核对单位');
  const key = b.context.composerDraftKey(job);
  assert.equal(b.context.sessionStorage.getItem(key), '核对单位');
  assert.equal(b.context.sessionStorage.getItem(key + '.update'), null);
});

test('a lost update reuses its ID and preserves edits made since the first submission', async () => {
  const b = browser(), job = 'job-update';
  const posts = [], lookups = [];
  const {store, controls} = observedTask(b, job, {
    post: async (target, path, payload) => {
      posts.push([target, path, {...payload}]);
      if (posts.length === 1) throw new TypeError('lost response');
      return {job_id: job, version: 2, status: 'pending'};
    },
    lookupUpdate: async (target, id) => { lookups.push([target, id]); throw Object.assign(new Error('not found'), {status: 404}); }
  });
  b.userInput.value = '换一个角度补充';
  await b.context.sendMsg();
  assert.equal(b.userInput.value, '换一个角度补充');
  assert.equal(posts.length, 1);
  const [target, path, payload] = posts[0];
  assert.equal(target, job);
  assert.equal(path, 'updates');
  assert.equal(payload.expected_version, 1);
  assert.equal(payload.instruction, '换一个角度补充');
  assert.match(payload.update_request_id, /^update-/);
  assert.deepEqual(lookups, [[job, payload.update_request_id]]);
  b.userInput.value = '核对期间的另一份补充草稿';
  await b.context.sendMsg();
  assert.deepEqual(lookups, [[job, payload.update_request_id], [job, payload.update_request_id]]);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(b.userInput.value, '核对期间的另一份补充草稿');
  assert.equal(b.context.composerDraft(b.context.composerDraftKey(job)).pending, null);
  store.hydrate({job_id: job, terminal_snapshot: {display_status: 'partial'}});
  assert.equal(b.userInput.value, '核对期间的另一份补充草稿');
  const recovered = controls.element.querySelector('.task-update-recovery');
  assert.equal(recovered.hidden, false);
  assert.match(recovered.textContent, /第 2 版 · 换一个角度补充/);
  assert.equal(b.requests.filter(request => request.opts.method === 'POST').length, 0);
});

test('terminal rejection leaves the draft pinned until the user opts into a new task', async () => {
  const b = browser(), job = 'ending-job', response = deferred();
  const {store} = observedTask(b, job, {post: () => response.promise});
  b.userInput.value = '任务结束时的草稿';
  const sending = b.context.sendMsg();
  const otherPosts = [];
  observedTask(b, 'next-in-line', {post: (...args) => { otherPosts.push(args); }});
  store.hydrate({job_id: job, terminal_snapshot: {display_status: 'completed'}});
  response.reject(Object.assign(new Error('closed'), {status: 409, detail: {code: 'task_not_running'}}));
  await sending;
  assert.equal(b.get('composer-task-target').title, job);
  assert.equal(b.get('send-btn').disabled, true);
  assert.equal(b.userInput.value, '任务结束时的草稿');
  await b.context.sendMsg();
  assert.equal(otherPosts.length, 0);
  assert.equal(b.requests.filter(r => r.opts.method === 'POST').length, 0);
  clickComposerMode(b);
  assert.equal(b.userInput.value, '任务结束时的草稿');
  await b.context.sendMsg();
  await flush();
  const post = b.last(r => r.opts.method === 'POST');
  assert.equal(JSON.parse(post.opts.body).query, '任务结束时的草稿');
  assert.equal(post.url, '/api/v2/session/session/turn-queue');
});

test('a receipt arriving after terminal keeps the submitted text recoverable until application is known', async () => {
  const b = browser(), job = 'late-receipt', response = deferred();
  const {store, controls} = observedTask(b, job, {post: () => response.promise});
  b.userInput.value = '保留到确认应用为止';
  const sending = b.context.sendMsg();
  store.hydrate({job_id: job, terminal_snapshot: {display_status: 'cancelled'}});
  response.resolve({job_id: job, version: 2, status: 'pending'});
  await sending;
  assert.equal(b.userInput.value, '');
  assert.match(controls.element.querySelector('.task-control-status').textContent, /已接收第 2 版补充，应用状态待确认/);
  assert.match(controls.element.querySelector('.task-update-recovery').textContent, /第 2 版 · 保留到确认应用为止/);
  b.context.composerDrafts.clear();
  controls.render(store.state);
  assert.match(controls.element.querySelector('.task-update-recovery').textContent, /保留到确认应用为止/);
  store.receive('task_update', {job_id: job, version: 2, status: 'not_applied'});
  assert.match(controls.element.querySelector('.task-control-status').textContent, /第 2 版调整未应用/);
  assert.equal(controls.element.querySelector('.task-update-recovery').hidden, false);
});

test('restoring an active task looks up an unresolved receipt without posting again', async () => {
  const b = browser(), job = 'restored-update', id = 'update-original';
  const key = b.context.composerDraftKey(job);
  b.context.sessionStorage.setItem(key, '原先的草稿');
  b.context.sessionStorage.setItem(key + '.update', JSON.stringify({text: '原先的草稿',
    payload: {update_request_id: id, expected_version: 1, instruction: '原先的草稿'}}));
  const lookups = [];
  observedTask(b, job, {post: () => assert.fail('lookup must never post'), lookupUpdate: async (...args) => {
    lookups.push(args);
    return {job_id: job, version: 2, status: 'applied'};
  }});
  await flush();
  assert.deepEqual(lookups, [[job, id]]);
  assert.equal(b.userInput.value, '');
  assert.equal(b.context.sessionStorage.getItem(key + '.update'), null);
});

test('a stale version preserves the draft and resubmission uses the refreshed version', async () => {
  const b = browser(), job = 'versioned-job';
  const versions = [];
  const {store} = observedTask(b, job, {post: async (_job, _path, payload) => {
    versions.push(payload.expected_version);
    if (versions.length === 1) throw Object.assign(new Error('conflict'), {status: 409, detail: {code: 'task_version_conflict'}});
    return {job_id: job, version: 7, status: 'applied'};
  }, refresh: async () => store.hydrate({job_id: job, task_version: 6})});
  b.userInput.value = '版本冲突时的草稿';
  await b.context.sendMsg();
  assert.equal(b.userInput.value, '版本冲突时的草稿');
  assert.match(b.get('composer-task-target').textContent, /任务版本 6/);
  assert.match(b.get('composer-task-status').textContent, /草稿已保留/);
  await b.context.sendMsg();
  assert.deepEqual(versions, [1, 6]);
  assert.equal(b.userInput.value, '');
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

test('turn sources are collapsed and deduplicated by record, URL, DOI, and shared version', async () => {
  const b = browser(), bubble = new Element('div');
  b.chatContainer.appendChild(bubble);
  const reader = b.context.CitrusCorpusReader;
  reader.attachTurnSources(bubble, 'sources-request');
  const sharedRef = {document_id: 'shared-doc', version_id: 'v1', index_generation: 'g1'};
  b.respond('/turn-sources?', {sources: [
    {kind: 'local', record_key: 'paper_1', origin: 'history', title: 'old local duplicate'},
    {kind: 'local', record_key: 'paper_1', origin: 'current', doi: '10.1000/ORIGINAL', title: 'current local'},
    {kind: 'web', url: 'https://doi.org/10.1000/original', title: 'DOI duplicate'},
    {kind: 'web', url: 'https://example.org/paper#section', title: 'web source'},
    {kind: 'web', origin: 'history', url: 'https://example.org/paper', title: 'URL duplicate'},
    {kind: 'web', url: 'javascript:alert(1)', title: 'invalid URL'},
    {kind: 'shared', read_ref: sharedRef, title: 'shared version'},
    {kind: 'shared', read_ref: {...sharedRef, start: 100}, title: 'shared duplicate'}
  ]});
  await flush();
  const panel = bubble.querySelector('.cr-turn-sources');
  assert.equal(panel.tagName, 'DETAILS');
  assert.equal(panel.open, false);
  assert.equal(panel.querySelector('summary').textContent, '候选来源（3）');
  assert.deepEqual(panel.querySelectorAll('.cr-ts-chip').map(chip => chip.textContent), ['shared version', 'current local', 'web source']);
  reader.attachTurnSources(bubble, 'sources-request');
  assert.equal(b.requests.filter(r => r.url.includes('/turn-sources?')).length, 1);
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
