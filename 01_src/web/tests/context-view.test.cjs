const test = require('node:test');
const { strict: assert } = require('node:assert');
const { project,short,percent } = require('../assets/context-view.js');

test('uses actual capacity and one estimate only, independent of cached or output work', () => {
  assert.deepEqual(project({estimate_scope:'full_request_estimate',estimated_input_tokens:165000,
    model_window:500000,input_limit:128000,model:'ignored',output_tokens:999,provider_usage:{input_tokens:123}}),
    {used:165000,capacity:500000,ratio:.33,state:'healthy',historical:false});
  assert.equal(short(165000), '165K');
  assert.equal(percent(.33),'33%');
});

test('does not manufacture a window from the working limit or a historical text estimate', () => {
  assert.deepEqual(project({estimate_scope:'full_request_estimate',estimated_input_tokens:87288,input_limit:128000}),
    {used:87288,capacity:null,ratio:null,state:'unknown',historical:false});
  assert.deepEqual(project({estimate_scope:'historical_estimate',estimated_tokens:12,model_window:500000}),
    {used:12,capacity:null,ratio:null,state:'unknown',historical:true});
  assert.equal(project(null).used,null);
  assert.equal(project({estimate_scope:'full_request_estimate',estimated_tokens:12,model_window:0}).capacity,null);
  assert.equal(project({estimate_scope:'full_request_estimate',estimated_tokens:501000,model_window:500000}).state,'critical');
});
