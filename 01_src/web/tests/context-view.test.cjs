const test = require('node:test');
const {strict: assert} = require('node:assert');
const {project, short, percent, treemap, gridSlots} = require('../assets/context-view.js');

const components = {system:2962, messages:914, tool_arguments:0, tool_schemas:19432, images:0, protocol:24};
const snapshot = {estimate_scope:'full_request_estimate', estimated_input_tokens:23332, input_limit:128000,
  model_window:null, output_tokens:4096, estimate_method:'character_heuristic', logical_call_id:'synthetic-call',
  attempt_id:'synthetic-attempt', components};
const usageIdentity = {scope:'single_call', attempt_id:snapshot.attempt_id, logical_call_id:snapshot.logical_call_id};

function slots(data) { return gridSlots(data.rows, data.used, data.chartTotal); }

test('uses a known model window without confusing the configured input cap or provider usage', () => {
  const data = project({...snapshot, model_window:500000, provider_usage:{...usageIdentity, input_tokens:24354}});
  assert.equal(data.used, 23332);
  assert.equal(data.modelWindow, 500000);
  assert.equal(data.inputLimit, 128000);
  assert.equal(data.capacity, 500000);
  assert.equal(data.limitKind, 'window');
  assert.equal(data.ratio, 23332 / 500000);
  assert.equal(data.free, 476668);
  assert.equal(data.usage.input, 24354);
  assert.equal(short(data.used), '23.3K');
  assert.equal(percent(data.ratio), '5%');
});

test('uses actual current app limit only as a labelled fallback for an unknown model window', () => {
  const data = project(snapshot);
  assert.equal(data.limitKind, 'input');
  assert.equal(data.modelWindow, null);
  assert.equal(data.capacity, 128000);
  assert.equal(data.ratio, 23332 / 128000);
  assert.equal(data.free, 104668);
  assert.equal(data.breakdown, 'complete');
  assert.equal(slots(data).length, 100);
  assert.equal(slots(data).filter(key => key === 'free').length, 82);
  assert.equal(percent(data.ratio), '18%');
  const tiles = treemap(data.rows);
  assert.ok(tiles.every(({row, w, h}) => Math.abs(w * h - row.value / 128000) < 1e-10));
  assert.equal(tiles.some(({row}) => row.key === 'images'), false);
});

test('does not invent free area or capacity when both limits are missing', () => {
  const data = project({...snapshot, input_limit:0});
  assert.equal(data.capacity, null);
  assert.equal(data.ratio, null);
  assert.equal(data.free, null);
  assert.equal(slots(data).length, 100);
  assert.equal(slots(data).some(key => key === 'free'), false);
  assert.equal(project(null).used, null);
  assert.equal(project({...snapshot, estimated_input_tokens:null}).chartTotal, 0);
  const historical = project({...snapshot, estimate_scope:'historical_estimate', model_window:128000});
  assert.equal(historical.historical, true);
  assert.equal(historical.capacity, null);
  assert.deepEqual(historical.rows, []);
});

test('keeps zero, missing components, and measured remainder distinct', () => {
  const data = project({...snapshot, components:{messages:0, system:5000}});
  assert.equal(data.breakdown, 'partial');
  assert.equal(data.rows.find(row => row.key === 'tool_arguments').value, null);
  assert.equal(data.rows.find(row => row.key === 'messages').value, 0);
  assert.equal(data.rows.find(row => row.key === 'other').value, 18332);
  const invalid = project({...snapshot, components:{messages:94000}});
  assert.equal(invalid.breakdown, 'inconsistent');
  assert.equal(invalid.chartTotal, 0);
  const over = project({...snapshot, input_limit:20000});
  assert.equal(over.state, 'critical');
  assert.equal(over.free, 0);
  assert.equal(slots(over).length, 100);
  assert.equal(slots(over).some(key => key === 'free'), false);
});

test('shows cache coverage only if directly reported counters cover reported input for this attempt', () => {
  const usage = {...usageIdentity, input_tokens:24000, cache_read:6000, cache_write:0, cache_uncached:18000};
  assert.equal(project({...snapshot, provider_usage:usage}).usage.coverage, .25);
  assert.equal(project({...snapshot, provider_usage:{...usage, cache_uncached:0}}).usage.coverage, null);
  assert.equal(project({...snapshot, provider_usage:{...usage, cache_uncached:null}}).usage.coverage, null);
  assert.equal(project({...snapshot, provider_usage:{...usage, cache_read:0, cache_uncached:24000}}).usage.coverage, 0);
  assert.equal(project({...snapshot, provider_usage:{...usage, attempt_id:'foreign'}}).usage, null);
});


test('keeps the sent cap, provider usage, elapsed time, and cumulative task budget distinct', () => {
  const data = project({...snapshot, output_tokens:6144, actual_output_tokens:6144, attempt_elapsed_ms:39610,
    model_window:20000, input_limit:13728, task_budget:{calls:2, max_calls:8, tokens:8530, max_tokens:30000},
    provider_usage:{...usageIdentity, output_tokens:1210}});
  assert.equal(data.outputReserve, 6144);
  assert.equal(data.actualOutputLimit, 6144);
  assert.equal(data.attemptElapsed, 39610);
  assert.deepEqual(data.budget, {calls:2, maxCalls:8, tokens:8530, maxTokens:30000});
  assert.equal(data.usage.output, 1210);
  assert.equal(data.inputLimit, 13728);
  assert.equal(data.modelWindow, 20000);
  assert.equal(project(snapshot).actualOutputLimit, null);
  assert.equal(project({...snapshot, attempt_id:'', actual_output_tokens:6144}).actualOutputLimit, null);
  assert.equal(project({...snapshot, task_budget:{calls:0}}).budget, null);
});


test('the header ring measures the application cap independently of the model drawer', () => {
  const view = project({estimate_scope: 'full_request_estimate', estimated_input_tokens: 7000,
    application_input_limit: 8000, input_limit: 8500, model_window: 100000});
  assert.equal(view.capacity, 100000);
  assert.equal(view.ratio, .07);
  assert.equal(view.ringLimit, 8000);
  assert.equal(view.ringRatio, .875);
  assert.equal(project({estimate_scope: 'full_request_estimate', estimated_input_tokens: 7000,
    model_window: 100000}).ringRatio, null);
});
