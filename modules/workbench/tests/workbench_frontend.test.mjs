// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createApi, importSource, importPlan, selectedHash, previewPoint, exportOptions, safeReturn} from '../wb/web/static/api.mjs';

test('script and static format map to the OpenAPI source contract', () => {
  assert.deepEqual(importSource('PY', 'asset_123'), {kind:'script', language:'python', entrypoint_ref:'asset_123'});
  assert.deepEqual(importSource('pdf','library:abc'), {kind:'rendered_asset', language:'unknown', entrypoint_ref:'library:abc'});
  assert.throws(() => importSource('zip','any'));
});

test('pick coordinates use the rendered image rectangle and top-left origin', () => {
  const box = {width:240,height:120,left:110,top:30};
  assert.deepEqual(previewPoint(box,170,120), {x:.25,y:.75});
  assert.deepEqual(previewPoint(box,90,200), {x:0,y:1});
  assert.equal(previewPoint({...box,width:0},110,110), null);
});

test('export values are numeric and optional dimensions omit empty inputs', () => {
  const values = new Map([['format','pdf'], ['width_mm',''], ['height_mm','50.3'], ['dpi','120']]);
  assert.deepEqual(exportOptions(values), {format:'pdf',height_mm:50.3,dpi:120});
});

test('signed return navigation cannot leave the origin', () => {
  const origin = 'https://app.test';
  assert.equal(safeReturn('/?figure_session=abc',origin), `${origin}/?figure_session=abc`);
  for (const url of ['//example.org', '/\\example.org', 'https://example.org', '/\texample.org']) assert.equal(safeReturn(url,origin),null);
});

test('workbench API transport sends revision checks and parses server failures', async () => {
  const api = createApi('https://app.test/prefix/workbench/static/api.mjs');
  const saved = globalThis.fetch;
  let expected;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url.pathname, expected.path);
      assert.equal(init.method, expected.method);
      assert.equal(init.credentials,'same-origin');
      assert.deepEqual(JSON.parse(init.body), expected.body);
      return new Response(JSON.stringify(expected.response), {status:expected.status ?? 200});
    };
    expected = {path:'/prefix/api/v1/figures/a/pick',method:'POST',body:{x:.1,y:.2,revision:4,locked_gids:['a']},response:{picked_gid:'object'}};
    assert.deepEqual(await api.pick('a',expected.body), expected.response);
    expected = {path:'/prefix/api/v1/figures/a/groups/grp/scale',method:'POST',body:{expected_revision:4,factor:1.1},response:{revision:5}};
    assert.deepEqual(await api.transform('a','grp','scale',expected.body), expected.response);
    expected = {path:'/prefix/api/v1/figures/a/native-overrides',method:'PATCH',body:{expected_revision:4,patches:[{gid:'axes_0.title',prop:'text',value:'New',identity:'x'}]},response:{detail:'conflict'},status:409};
    await assert.rejects(api.patch('a',true,expected.body), {message:'conflict',status:409});
  } finally { globalThis.fetch = saved; }
});


test('defaults to editable native source and rejects static in that flow', () => {
  assert.deepEqual(importPlan('py', 'library:python'), {source:{kind:'script', language:'python', entrypoint_ref:'library:python'}, mode:'editable'});
  assert.equal(importPlan('R', 'library:r').mode, 'editable');
  assert.throws(() => importPlan('svg', 'library:svg'));
  assert.throws(() => importPlan('py', 'library:python', 'static'));
  assert.deepEqual(importPlan('svg', 'library:svg', 'static'), {source:{kind:'rendered_asset', language:'unknown', entrypoint_ref:'library:svg'}, mode:'preview_only'});
});


test('browser refuses a selection without a pinned file version', () => {
  assert.equal(selectedHash('a'.repeat(64)), 'a'.repeat(64));
  assert.throws(() => selectedHash(undefined));
  assert.throws(() => selectedHash('download-latest'));
});

import {cardEditable} from '../wb/web/static/views.mjs';
import {movePatch, moveTarget, nativeText, rulerTicks, visibleBounds} from '../wb/web/static/viewport.mjs';
import {push, treeKeyAction} from '../wb/web/static/tavotto-primitives.mjs';

test('cards open by pinned preview only and do not infer editable linkage from extension', async () => {
  const api = createApi('https://app.test/prefix/workbench/static/api.mjs');
  const card = {preview_ref:'preview_123',preview_version:3,source_state:'verified',capability:'native',thumbnail_url:'preview-cards/preview_123/thumbnail?version=3'};
  assert.equal(api.thumbnail(card.thumbnail_url),'https://app.test/prefix/api/v1/preview-cards/preview_123/thumbnail?version=3');
  assert.equal(api.thumbnail('https://external.test/thumb.png'),null);
  assert.equal(api.thumbnail('preview-cards/../thumbnail?version=3'),null);
  assert.equal(cardEditable(card),true);
  assert.equal(cardEditable({...card,source_state:'unavailable'}),false);
  assert.equal(cardEditable({...card,capability:'preview_only'}),false);
  const original = globalThis.fetch, calls = [];
  try {
    globalThis.fetch = async (url,options) => {
      calls.push([url.pathname,options.method ?? 'GET',options.headers?.['Idempotency-Key'],options.body && JSON.parse(options.body)]);
      return new Response('{}');
    };
    const source = {...importSource('r','asset_a'),assets:['asset_b'],hashes:{asset_a:'a'.repeat(64),asset_b:'b'.repeat(64)}};
    await api.cards();
    await api.prepare(source,'explicit-key');
    await api.prepareStatus('operation_a');
    await api.openCard(card,'editable');
    await api.openCard(card,'preview_only');
    assert.deepEqual(calls,[
      ['/prefix/api/v1/preview-cards','GET',undefined,undefined],
      ['/prefix/api/v1/preview-runs','POST','explicit-key',{source}],
      ['/prefix/api/v1/preview-runs/operation_a','GET',undefined,undefined],
      ['/prefix/api/v1/figures/import','POST',undefined,{preview_ref:'preview_123',preview_version:3,options:{parse_mode:'editable'}}],
      ['/prefix/api/v1/figures/import','POST',undefined,{preview_ref:'preview_123',preview_version:3,options:{parse_mode:'preview_only'}}],
    ]);
  } finally { globalThis.fetch = original; }
});

test('outline clips authoritative bboxes and ruler recomputes from displayed paper', () => {
  assert.deepEqual(visibleBounds([{bbox:[-.25,.125,.75,.5],clip_bbox:[.25,0,.375,.5]}]),[.25,.125,.25,.375]);
  assert.equal(visibleBounds([{bbox:[1.1,.1,.1,.1]}]),null);
  assert.deepEqual(rulerTicks(20,75,100,10).map(t => t.px),[20,30,40,50,60,70]);
  assert.deepEqual(rulerTicks(-55,40,100,10),[{px:5,label:''},{px:15,label:''},{px:25,label:''},{px:35,label:''}]);
  assert.ok(rulerTicks(0,200,200,100).length > 0);
  assert.deepEqual(rulerTicks(0,200,0,100),[]);
});

test('native drag uses only explicit capability with top-origin text and bottom-origin axes', () => {
  const text = {gid:'caption',identity:'text-id',bbox:[.1,.1,.4,.4],draggable:true,drag_prop:'pos_frac',anchor:[.1,.2],editable:[{prop:'text',value:'Original'}]};
  const raster = {gid:'image',bbox:[.125,.125,.25,.25],geom_gid:'axes'};
  const axes = {gid:'axes',identity:'axes-id',bbox:[.125,.125,.5,.5],editable:[{prop:'position',value:[.125,.25,.5,.5]}]};
  const fixed = {gid:'fixed',bbox:[.1,.1,.4,.4],draggable:false,drag_prop:'pos_frac',anchor:[.1,.2]};
  const doc = {editability:'semantic',native_manifest:{elements:[text,raster,axes,fixed]}};
  assert.equal(nativeText(doc,'caption').field.value,'Original');
  assert.equal(nativeText({...doc,editability:'preview_only'},'caption'),null);
  const spot = {x:.2,y:.2};
  const textTarget = moveTarget(doc,'caption',spot);
  assert.deepEqual(movePatch(textTarget,.125,.0625),{gid:'caption',prop:'pos_frac',value:[.225,.2625],identity:'text-id'});
  assert.equal(movePatch(textTarget,.000000001,0),null);
  assert.deepEqual(movePatch(moveTarget(doc,'image',spot),.125,.0625),{gid:'axes',prop:'position',value:[.25,.1875,.5,.5],identity:'axes-id'});
  assert.equal(moveTarget(doc,'image',spot,new Set(['axes'])),null);
  assert.equal(moveTarget(doc,'caption',{x:.8,y:.8}),null);
  assert.equal(moveTarget(doc,'fixed',spot),null);
});

test('adapted tree keys navigate and history stays bounded after a fresh edit', () => {
  const rows = [{level:1,expanded:true},{level:2},{level:1,expanded:false}];
  assert.deepEqual(treeKeyAction('ArrowRight',rows,0),{type:'focus',index:1});
  assert.deepEqual(treeKeyAction('ArrowLeft',rows,1),{type:'focus',index:0});
  assert.deepEqual(treeKeyAction('ArrowRight',rows,2),{type:'expand'});
  assert.deepEqual(push([1,2],3,2),{past:[2,3],future:[]});
});


import {suggestionFields,suggestionValue} from '../wb/web/static/assistant-ui.mjs';
test('local suggestions only admit defined scalar fields and never interpret draft text', () => {
  const fields = [{prop:'text',value:'Caption'},{prop:'color',value:'#112233'},{prop:'fontsize',value:14,min:5,max:30},{prop:'label',value:'Series'}];
  assert.equal(suggestionFields({editable:fields}).length,3);
  assert.equal(suggestionValue(fields[0],'<img src=unsafe>'),'<img src=unsafe>');
  assert.equal(suggestionValue(fields[1],'#A1B2C3'),'#a1b2c3');
  assert.throws(() => suggestionValue(fields[1],'url(example)'));
  assert.equal(suggestionValue(fields[2],' 18.5 '),18.5);
  assert.throws(() => suggestionValue(fields[2],'120'));
});
