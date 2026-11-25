'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');

const tick = async () => { await new Promise(resolve => setImmediate(resolve)); };
const ok = data => ({ok:true, status:200, json:async () => data});
const failed = detail => ({ok:false, status:500, json:async () => ({detail})});
const event = {preventDefault() {}};
function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return {promise, resolve};
}
function harness(script, fetch) {
  const elements = new Map();
  const listeners = new Map();
  const create = () => ({value:'', innerHTML:'', textContent:'', dataset:{}, attrs:{}, children:[],
    hidden:true, disabled:false, classList:{add(){}, remove(){}},
    replaceChildren(...children) { this.children = children; }, append(...children) { this.children.push(...children); },
    setAttribute(name,value) { this.attrs[name]=value; }, addEventListener(name, handler) { this['on'+name]=handler; },
    removeAttribute(){}, focus(){}, close(){this.open=false;} });
  const el = id => {
    if (!elements.has(id)) elements.set(id, create());
    return elements.get(id);
  };
  const location = {search:'', replace(){}, reload(){}};
  const window = {addEventListener(name, fn) { listeners.set(name, fn); }};
  const document = {getElementById:id => id === 'corpusList' ? null : el(id), createElement:create,
    addEventListener(name, fn) { listeners.set(name, fn); }};
  vm.runInNewContext(readFileSync(join(__dirname,'..','assets',script),'utf8'),
    {fetch, window, document, location, AbortController, URLSearchParams, setTimeout:()=>1, clearTimeout(){}});
  if (listeners.has('DOMContentLoaded')) listeners.get('DOMContentLoaded')();
  return el;
}

function app(username) { return {username, status:'pending', application_id:username}; }
function device(mode) { return {mode, selected_providers:[mode], available_providers:[mode]}; }

test('admin publishes only the newest application and device responses', async () => {
  const firstApps = deferred(), secondApps = deferred(), oldDevice = deferred(), switchDevice = deferred();
  let appLoads = 0, deviceLoads = 0;
  const el = harness('admin.js', (url, options={}) => {
    if (url.endsWith('/auth/me')) return ok({user:{role:'admin', username:'fixture'}});
    if (url.endsWith('/users')) return ok({users:[{username:'account fixture', active:true}]});
    if (url.endsWith('/queue')) return ok({active:2, max_active:4, queued:0, max_pending:8});
    if (url.endsWith('/config')) return ok({model:{main:'fixture'}});
    if (url.endsWith('/providers')) return ok({providers:[]});
    if (url.includes('/requests?') || url.includes('/invites?')) return ok({});
    if (url.includes('/applications?')) return (++appLoads === 1 ? firstApps : secondApps).promise;
    if (url.endsWith('/onnx/device') && options.method === 'PUT') {
      assert.deepEqual(JSON.parse(options.body),{mode:'cpu'});
      return switchDevice.promise;
    }
    if (url.endsWith('/onnx/device')) {
      deviceLoads++;
      return oldDevice.promise;
    }
    throw Error(url);
  });
  await tick();
  assert.match(el('usersTable').innerHTML,/account fixture/);
  assert.match(el('activeCount').textContent,/2 \/ 4/);
  assert.equal(deviceLoads,1);
  el('applicationStatus').value='approved';
  el('applicationStatus').onchange();
  secondApps.resolve(ok({applications:[app('latest')]}));
  await tick();
  firstApps.resolve(ok({applications:[app('obsolete')]}));
  await tick();
  assert.match(el('applicationsTable').innerHTML,/latest/);
  assert.doesNotMatch(el('applicationsTable').innerHTML,/obsolete/);
  el('onnxDeviceMode').value='cpu';
  el('onnxDeviceMode').onchange();
  const applying=el('onnxDeviceApply').onclick();
  switchDevice.resolve(ok(device('cpu')));
  await applying;
  oldDevice.resolve(ok(device('gpu')));
  await tick();
  assert.equal(el('onnxDeviceMode').value,'cpu');
  assert.match(el('onnxDeviceStatus').textContent,/模式：cpu/);
});

test('review save preserves the second finding, exposes a failed refresh, and fences navigation', async () => {
  const findings = id => [1,2].map(index => ({finding_id:id+index,rule:'exact_column',summary:id+index,
    review:{state:'pending',note:'',revision:index},review_history:[],locations:[]}));
  const record = id => ({review_id:id,report:{common_findings:findings(id)}});
  const writes = [];
  let aReads = 0, pendingSave;
  const el=harness('integrity-review.js',(url,options={}) => {
    if (url.endsWith('/reviews')) return ok({reviews:[{review_id:'a'},{review_id:'b'}]});
    if (options.method === 'PUT') {
      writes.push({url,body:JSON.parse(options.body)});
      return pendingSave ? pendingSave.promise : ok({});
    }
    if (url.endsWith('/a')) return ++aReads===3 ? failed('fixture refresh failure') : ok(record('a'));
    if (url.endsWith('/b')) return ok(record('b'));
    throw Error(url);
  });
  await tick();
  el('ir-findings').children[1].onclick();
  await el('ir-form').onsubmit(event);
  assert.equal(el('ir-findings').children[1].attrs['aria-current'],'true');
  assert.match(el('ir-status').textContent,/决定已保存/);
  await el('ir-form').onsubmit(event);
  assert.match(el('ir-status').textContent,/决定已保存，但重新读取失败/);
  assert.match(el('ir-status').textContent,/fixture refresh failure/);
  el('ir-reload').onclick();
  await tick();
  assert.equal(el('ir-findings').children[1].attrs['aria-current'],'true');
  pendingSave=deferred();
  const old=el('ir-form').onsubmit(event);
  el('ir-reviews').value='b';
  el('ir-reviews').onchange();
  await tick();
  pendingSave.resolve(ok({}));
  await old;
  assert.equal(el('ir-summary').textContent,'b1');
  assert.doesNotMatch(el('ir-status').textContent,/决定已保存/);
  assert.ok(writes.every(item=>item.url.endsWith('/a/findings/a2') && item.body.expected_revision===2));
});
