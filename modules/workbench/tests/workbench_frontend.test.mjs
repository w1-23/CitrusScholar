// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createApi, importSource, previewPoint, exportOptions, safeReturn} from '../wb/web/static/api.mjs';

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
