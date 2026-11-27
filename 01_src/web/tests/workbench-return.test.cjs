const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const app = read('assets/app.js');
const start = app.match(/^async function startAuthenticatedApp\(\) \{[\s\S]*?^}/m)[0];
const navigation = read('assets/workbench/navigation.js');
const sourceSession = 'ca7d045c-04d3-4b2f-b6f8-5e07b26d8f22';
const remembered = '81b5ed88-b2fc-4d54-adc9-349c4bcb678a';

async function startup(query, status, changeAccount = false) {
  const activity = { probes: [], written: [], events: [], visible: [], opened: [], url: null };
  const context = {
    URL, localStorage: { getItem: () => remembered, setItem: (_, value) => activity.written.push(value) },
    sessionStorageKey: () => 'account-bound', authEpoch: 1, currentAccount: { role: 'user' }, sessionId: null,
    location: { href: 'https://fixture.test/' + query }, history: {
      replaceState: (_, __, url) => { activity.url = url.pathname + url.search; },
    },
    accountFetch: async (url) => {
      if (url === '/api/v2/config') return { json: async () => ({}) };
      activity.probes.push(url);
      if (changeAccount) context.authEpoch++;
      return { ok: status === 200 };
    },
    window: { dispatchEvent: (event) => activity.events.push(event.detail.session_id) },
    CustomEvent: function (_, options) { this.detail = options.detail; },
    showAuthGate: (hidden) => activity.visible.push(hidden),
    restoreComposerSession() {},
    ensureSession: async () => { activity.opened.push(context.sessionId); },
    clearTimeout() {}, sharedMaintenanceTimer: null, displaySharedMaintenance() {}, refreshSharedMaintenance() {},
    applyAppVersion() {},
  };
  await vm.runInNewContext(start + '\nstartAuthenticatedApp()', context);
  return activity;
}

test('Workbench Back only generates an internal session URL or root', async () => {
  const { sessionReturnUrl } = await import('data:text/javascript,' + encodeURIComponent(navigation));
  assert.equal(sessionReturnUrl({ return_session_id: sourceSession }), '/?figure_session=' + sourceSession);
  assert.equal(sessionReturnUrl({ return_session_id: '//outside.test/' }), '/');
  assert.equal(sessionReturnUrl(null), '/');
});

test('chat Back restores a verified source session without opening the library', async () => {
  const visited = await startup('?figure_session=' + sourceSession, 200);
  assert.deepEqual(visited, {
    probes: ['/api/v2/session/' + sourceSession + '/turns?limit=1'],
    written: [sourceSession], events: [sourceSession], visible: [false], opened: [sourceSession], url: '/',
  });
});

test('invalid, foreign and account-stale source sessions cannot replace the remembered chat', async () => {
  const invalid = await startup('?figure_session=%2F%2Foutside.test', 200);
  assert.deepEqual(invalid.probes, []);
  assert.deepEqual(invalid.opened, [remembered]);
  const denied = await startup('?figure_session=' + sourceSession, 404);
  assert.deepEqual(denied.written, []);
  assert.deepEqual(denied.opened, [remembered]);
  const stale = await startup('?figure_session=' + sourceSession, 200, true);
  assert.deepEqual(stale.written, []);
  assert.deepEqual(stale.opened, []);
});
