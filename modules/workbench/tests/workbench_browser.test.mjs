// SPDX-License-Identifier: AGPL-3.0-only
// Optional Chromium interaction suite. WORKBENCH_PLAYWRIGHT points at a
// Playwright module and WORKBENCH_CHROME can select its Chromium executable.
// All routes are fulfilled in-process, and no network listener is started.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {test} from 'node:test';

const entry = process.env.WORKBENCH_PLAYWRIGHT;
const web = new URL('../wb/web/',import.meta.url);
const root = 'https://workbench.example';
const hash = 'a'.repeat(64);
const card = {preview_ref:'preview_a',preview_version:1,figure_ordinal:0,label:'三面板研究图',thumbnail_url:'preview-cards/preview_a/thumbnail?version=1',preview_sha256:hash,capability:'native',source_state:'verified'};
// Deliberately synthetic chart used to demonstrate visual placement, not a production output.
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="800" height="550" viewBox="0 0 800 550">
  <rect width="800" height="550" fill="white"/>
  <g font-family="sans-serif" font-size="16" fill="#362C26">
    <text x="134" y="95" font-size="20" font-weight="bold">Synthetic study · layout fixture</text>
    <text x="75" y="139" font-weight="bold">A</text><text x="453" y="139" font-weight="bold">B</text><text x="453" y="341" font-weight="bold">C</text>
    <text x="190" y="490">Measurement interval</text><text x="538" y="307">Treatment</text><text x="530" y="513">Response score</text>
    <text x="96" y="283" transform="rotate(-90,96,283)">Normalized signal</text>
  </g>
  <g fill="none" stroke="#594d43" stroke-width="1.6"><path d="M126 154v300h247 M481 155v125h228 M481 357v129h228"/>
  <path opacity=".23" d="M126 214h247m-247 60h247m-247 60h247m-247 60h247 M481 197h228m-228 41h228 M481 400h228m-228 43h228"/></g>
  <g fill="none" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"><path stroke="#CE6B35" d="m138 407 33-21 32-63 32 18 30-56 29-57 33 12 35-57"/><path stroke="#4f7669" d="m138 419 33-10 32-35 32 13 30-41 29-39 33-16 35-38"/></g>
  <g fill="#D77B46"><rect x="505" y="235" width="27" height="45"/><rect x="551" y="210" width="27" height="70"/><rect x="597" y="190" width="27" height="90"/><rect x="643" y="173" width="27" height="107"/></g>
  <g fill="#668a79"><circle cx="505" cy="451" r="6"/><circle cx="531" cy="446" r="6"/><circle cx="550" cy="429" r="6"/><circle cx="575" cy="435" r="6"/><circle cx="596" cy="406" r="6"/><circle cx="615" cy="414" r="6"/><circle cx="638" cy="390" r="6"/><circle cx="662" cy="382" r="6"/><circle cx="690" cy="378" r="6"/></g>
  <path d="M501 459L695 374" stroke="#668a79" stroke-dasharray="7 5"/>
</svg>`;
const contentTypes = {html:'text/html',css:'text/css',mjs:'application/javascript',svg:'image/svg+xml'};
const csp = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";

test('three-column workbench explicitly renders and opens pinned cards, then edits and restores native revisions', {skip:!entry,timeout:60_000}, async () => {
  const {chromium} = await import(entry);
  const browser = await chromium.launch({executablePath:process.env.WORKBENCH_CHROME || undefined,args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const context = await browser.newContext({viewport:{width:1480,height:763}});
    await context.grantPermissions(['clipboard-read','clipboard-write'],{origin:root});
    const page = await context.newPage();
    const capture = async name => { if (process.env.WORKBENCH_SCREENSHOTS) await page.screenshot({path:join(process.env.WORKBENCH_SCREENSHOTS,name+'-mock.png')}); };
    const requests = [], errors = [];
    page.on('pageerror',e => errors.push(e.message));
    let prepared = false, opened = false, canEdit = true, revision = 1;
    const names = new Map();
    const pins = new Map([[1,{text:'Original title',anchor:[.17,.18],size:[180,123.75],color:'#ffffff',stroke:false}]]);
    const docs = rev => {
      const pin = pins.get(rev);
      const axes = {gid:'axes_1',role:'axes',label:'面板 A',bbox:[.17,.25,.28,.58],editable:[{prop:'position',value:[.17,.17,.28,.58]}]};
      const title = {gid:'title',role:'title',identity:'native-title',label:'图件标题',bbox:[pin.anchor[0],pin.anchor[1]-.07,.22,.07],draggable:true,drag_prop:'pos_frac',anchor:pin.anchor,editable:[{prop:'stroke_enabled',type:'bool',value:pin.stroke},{prop:'weight',type:'enum',value:'normal',options:['normal','bold']},{prop:'style',type:'enum',value:'normal',options:['normal','italic']},{prop:'color',type:'color',value:'#332211'},{prop:'fontsize',type:'number',value:14,unit:'pt',min:3,max:200},{prop:'fontfamily',type:'enum',value:'serif',options:['serif','sans-serif']},{prop:'text',type:'text',value:pin.text}]};
      const figure = {gid:'figure',role:'figure',label:'整张图',editable:[{prop:'size_mm',value:pin.size},{prop:'facecolor',value:pin.color},{prop:'transparent',value:false}]};
      return {figure_id:'figure_a',revision:rev,name:card.label,editability:canEdit?'semantic':'preview_only',native_editability:canEdit?'semantic':null,panels:[{id:'plot',label:'A'}],native_manifest:canEdit?{size_mm:pin.size,elements:[figure,title,axes],groups:[{gid:'group_a',label:'自动分组',members:['axes_1']}]}:null,objects:[],limitations:[],source:{language:'python'}};
    };
    await page.route('**/*',async route => {
      const request = route.request(), url = new URL(request.url());
      const method = request.method(), path = url.pathname;
      requests.push({method,path,body:method === 'GET' ? null : request.postDataJSON()});
      const json = value => route.fulfill({json:value});
      if (/^\/prefix\/workbench\/(?:static\/[a-z.-]+)?$/.test(path)) {
        const name = path.split('/workbench/')[1] || 'index.html';
        return route.fulfill({contentType:contentTypes[name.split('.').pop()],body:await readFile(new URL(name,web)),headers:{'Content-Security-Policy':csp}});
      }
      const suffix = path.replace(/^\/prefix\/api\/v1\//,'');
      if (suffix === 'session') return json({return_to:'/?figure_session=signed',source_assets:['source']});
      if (suffix === 'figures') return json({figures:opened?[{figure_id:'figure_a',revision,name:card.label}]:[]});
      if (suffix === 'preview-cards') return json({cards:prepared?[card]:[]});
      if (suffix === 'library') return json({assets:url.searchParams.get('type') === 'script' ? [{asset_ref:'library:source',name:'figure.py',format:'py',version:hash}] : url.searchParams.get('type') === 'dependency' ? [{asset_ref:'library:data',name:'data.csv',format:'csv',version:hash}] : []});
      if (suffix === 'preview-runs') { prepared = true; return json({operation_id:'operation_a',status:'ready',cards:[card]}); }
      if (suffix === 'figures/import') {
        opened = true;
        canEdit = requests.at(-1).body.options.parse_mode !== 'preview_only';
        return json({figure_id:'figure_a',status:'ready'});
      }
      if (suffix === 'preview-cards/preview_a/thumbnail' || suffix === 'figures/figure_a/preview') return route.fulfill({contentType:'image/svg+xml',body:svg});
      if (suffix === 'figures/figure_a/history') return json({versions:[...pins.keys()].reverse().map(revision => ({revision,created_at:'2026-11-27 12:01:01',name:names.get(revision)}))});
      if (suffix === 'figures/figure_a' || suffix === 'figures/figure_a/objects') return json(docs(Number(url.searchParams.get('revision')) || revision));
      const naming = /^figures\/figure_a\/history\/(\d+)\/name$/.exec(suffix);
      if (naming) {
        const revision = Number(naming[1]), {name} = requests.at(-1).body;
        names.set(revision,name);
        return json({revision,created_at:'2026-11-27 12:01:01',name});
      }
      if (suffix === 'figures/figure_a/pick') return json({candidate_gids:['title','axes_1'],picked_gid:'title'});
      if (suffix === 'figures/figure_a/native-overrides') {
        const {expected_revision,patches:[patch]} = requests.at(-1).body;
        assert.equal(expected_revision,revision);
        pins.set(++revision,{...pins.get(expected_revision),...({pos_frac:{anchor:patch.value},size_mm:{size:patch.value},text:{text:patch.value},stroke_enabled:{stroke:patch.value}}[patch.prop] ?? {})});
        return json(docs(revision));
      }
      if (suffix === 'figures/figure_a/restore') {
        const body = requests.at(-1).body;
        assert.equal(body.expected_revision,revision);
        pins.set(++revision,pins.get(body.revision));
        return json(docs(revision));
      }
      return route.fulfill({status:404,body:''});
    });
    const waitReady = () => page.locator('#studio[aria-busy=false]').waitFor();
    await page.goto(root+'/prefix/workbench/');
    await waitReady();
    assert.ok(!requests.some(r => r.method !== 'GET' || r.path.endsWith('/library')));
    assert.equal(await page.locator('#return').getAttribute('href'),root+'/?figure_session=signed');
    const geometry = await page.locator('#studio > .panel').evaluateAll(panels => panels.map(panel => { const {x,y,width}=panel.getBoundingClientRect(); return {x,y,width}; }));
    assert.ok(geometry.every(p => p.y === geometry[0].y) && geometry[1].width > 2*geometry[2].width);
    assert.equal(await page.locator('body').evaluate(el => getComputedStyle(el).backgroundColor),'rgb(245, 243, 239)');

    await page.locator('#import-open').click();
    assert.ok(!requests.some(r => r.method === 'POST'));
    await page.locator('#source-library').selectOption('library:source');
    await page.locator('#library-dependencies').selectOption('library:data');
    await capture('02-prepare-script-and-data');
    await page.locator('#import-submit').click();
    await waitReady();
    const prep = requests.find(r => r.path.endsWith('/preview-runs'));
    assert.deepEqual(prep.body,{source:{kind:'script',language:'python',entrypoint_ref:'library:source',assets:['library:data'],hashes:{'library:source':hash,'library:data':hash}}});
    assert.ok(!requests.some(r => r.path.endsWith('/figures/import')));
    await page.locator('#assets .preview-card img').waitFor();
    await page.locator('#assets .preview-card').click();
    assert.equal(await page.locator('#card-mode').inputValue(),'editable');
    await capture('03-rendered-card-picker');
    await page.locator('#card-form button[type=submit]').click();
    await waitReady();
    assert.deepEqual(requests.find(r => r.path.endsWith('/figures/import')).body,{preview_ref:'preview_a',preview_version:1,options:{parse_mode:'editable'}});
    await page.locator('#preview').evaluate(image => image.decode());
    await page.locator('#object-search').fill('面板 A');
    assert.equal(await page.locator('#object-list [role=treeitem]').count(),2);
    await page.locator('#object-search').fill('');
    const rect = await page.locator('#preview').boundingBox();
    assert.ok(rect.width > 500 && rect.height > 300);

    await page.locator('#zoom-in').click();
    assert.equal(await page.locator('#zoom-reset').textContent(),'125%');
    await page.locator('#zoom-reset').click();
    await page.locator('#grid-toggle').click();
    assert.equal(await page.locator('#paper').getAttribute('data-grid'),'true');
    await page.locator('#ruler-toggle').click();
    assert.equal(await page.locator('#ruler-x').isVisible(),false);
    await page.locator('#ruler-toggle').click();
    const paper = await page.locator('#preview').boundingBox();
    const x = paper.x+paper.width*.22, y = paper.y+paper.height*.145;
    await page.mouse.dblclick(x,y,{delay:40});
    await page.locator('#inline-form').waitFor();
    assert.equal(await page.locator('#inline-text').inputValue(),'Original title');
    const inspector = page.locator('#properties .native-editor');
    assert.equal(await inspector.locator('.native-field').first().getAttribute('data-prop'),'text');
    assert.equal(await inspector.getByLabel('字号（pt）').inputValue(),'14');
    assert.equal(await inspector.getByLabel('字重').locator('option:checked').textContent(),'常规');
    assert.equal(await inspector.getByLabel('字形').locator('option').last().textContent(),'斜体');
    assert.equal(await inspector.getByLabel('文字颜色').getAttribute('type'),'color');
    assert.equal(await inspector.getByLabel('启用文字描边').isVisible(),false);
    await capture('04-native-inline-editor');
    await page.evaluate(() => navigator.clipboard.writeText('New title'));
    await page.locator('#inline-paste').click();
    await page.waitForFunction(() => document.querySelector('#inline-text').value === 'New title');
    assert.equal(await page.locator('#inline-text').inputValue(),'New title');
    assert.ok(!requests.some(r => r.path.endsWith('/native-overrides')));
    await page.locator('#inline-form button[type=submit]').click();
    await waitReady();
    assert.equal(await page.locator('#revision').textContent(),'版本 2');
    await page.locator('#copy-text').click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()),'New title');
    await page.locator('#preview').evaluate(image => image.decode());
    const next = await page.locator('#preview').boundingBox();
    const dx = 22, dy = 13, from = {x:next.x+next.width*.22,y:next.y+next.height*.145};
    await page.mouse.move(from.x,from.y);
    await page.mouse.down();
    await page.mouse.move(from.x+dx,from.y+dy,{steps:6});
    await page.mouse.up();
    await waitReady();
    assert.equal(await page.locator('#revision').textContent(),'版本 3');
    const patches = requests.filter(r => r.path.endsWith('/native-overrides')).map(r => r.body.patches[0]);
    assert.deepEqual(patches[0],{gid:'title',prop:'text',value:'New title',identity:'native-title'});
    assert.equal(patches[1].prop,'pos_frac');
    assert.ok(patches[1].value[0] > .17 && patches[1].value[1] > .18);
    await page.locator('#undo').click();
    await waitReady();
    await page.locator('#redo').click();
    await waitReady();
    assert.deepEqual(requests.filter(r => r.path.endsWith('/restore')).map(r => r.body),[{expected_revision:3,revision:2},{expected_revision:4,revision:3}]);
    await page.locator('#tab-canvas').click();
    assert.equal(await page.locator('#canvas-width').inputValue(),'180');
    await capture('05-canvas-physical-controls');
    await page.locator('#canvas-width').fill('170');
    await page.locator('#canvas-save').click();
    await waitReady();
    assert.equal(await page.locator('#canvas-width').inputValue(),'170');
    assert.deepEqual(requests.filter(r => r.path.endsWith('/native-overrides')).at(-1).body.patches,[{gid:'figure',prop:'size_mm',value:[170,123.75]}]);
    await page.locator('#tab-history').click();
    assert.equal(await page.locator('#version-list .version').count(),6);
    await page.locator('#version-list .version').first().getByRole('button',{name:'命名'}).click();
    await page.locator('#version-name').fill('封面图 · 最终构图');
    await page.locator('#rename-form button[type=submit]').click();
    await waitReady();
    assert.ok((await page.locator('#version-list .version').first().textContent()).includes('封面图 · 最终构图'));
    assert.deepEqual(requests.find(r => r.path.endsWith('/history/6/name')),{method:'PUT',path:'/prefix/api/v1/figures/figure_a/history/6/name',body:{name:'封面图 · 最终构图'}});
    await capture('06-version-timeline');
    await page.locator('#tab-properties').click();
    await page.getByRole('treeitem',{name:'图件标题'}).click();
    await waitReady();
    await capture('07-compact-inspector');
    await inspector.locator('summary').click();
    await inspector.getByLabel('启用文字描边').check();
    await inspector.getByRole('button',{name:'保存对象修改'}).click();
    await waitReady();
    assert.deepEqual(requests.filter(r => r.path.endsWith('/native-overrides')).at(-1).body,{expected_revision:6,patches:[{gid:'title',prop:'stroke_enabled',value:true,identity:'native-title'}]});
    await page.locator('#tab-assistant').click();
    const writes = () => requests.filter(r => r.path.endsWith('/native-overrides')).length;
    const before = writes();
    await page.locator('#assistant-action').selectOption('text');
    await page.locator('#assistant-text').fill('Confirmed locally');
    await page.getByRole('button',{name:'预览建议'}).click();
    assert.equal(await page.locator('#assistant-before').textContent(),'New title');
    assert.equal(await page.locator('#assistant-after').textContent(),'Confirmed locally');
    assert.equal(writes(),before);
    await capture('08-assistant-local-preview');
    await page.locator('#assistant-cancel').click();
    assert.equal(await page.locator('#assistant-preview').isVisible(),false);
    assert.equal(writes(),before);
    await page.getByRole('button',{name:'预览建议'}).click();
    await page.locator('#assistant-confirm').click();
    await waitReady();
    assert.equal(writes(),before+1);
    assert.deepEqual(requests.filter(r => r.path.endsWith('/native-overrides')).at(-1).body,{expected_revision:7,patches:[{gid:'title',prop:'text',value:'Confirmed locally',identity:'native-title'}]});
    await page.locator('#undo').click();
    await waitReady();
    assert.equal(await page.locator('#assistant-text').inputValue(),'New title');
    assert.ok(!requests.some(r => new URL(r.path,root).pathname.startsWith('/prefix/api/') && r.path.includes('assistant')));
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});


test('opens an offered Host revision only after explicit confirmation', {skip:!entry,timeout:25_000}, async () => {
  const {chromium} = await import(entry);
  const browser = await chromium.launch({executablePath:process.env.WORKBENCH_CHROME || undefined});
  try {
    const page = await browser.newPage();
    page.setDefaultTimeout(5000);
    const imports = [], errors = [], ref = 'b'.repeat(32);
    let offered = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), path = new URL(request.url()).pathname;
      const file = /^\/prefix\/workbench\/(static\/[a-z.-]+)?$/.exec(path);
      if (file) {
        const name = file[1] || 'index.html';
        return route.fulfill({contentType:contentTypes[name.split('.').pop()],body:await readFile(new URL(name,web)),headers:{'Content-Security-Policy':csp}});
      }
      const suffix = path.replace(/^\/prefix\/api\/v1\//,'');
      if (suffix === 'session') return route.fulfill({json:{worker_only:true, return_to:'/', ...(offered ? {frozen_project:ref,frozen_render_version:2} : {})}});
      if (suffix === 'figures') return route.fulfill({json:{figures:[]}});
      if (suffix === 'figures/import-frozen') {
        imports.push([request.method(),request.postDataJSON()]);
        return route.fulfill({status:201,json:{figure_id:'figure_saved'}});
      }
      if (suffix === 'figures/figure_saved') return route.fulfill({json:{figure_id:'figure_saved',revision:1,name:'Edited fixture',editability:'preview_only',panels:[],objects:[],limitations:[],source:{language:'python'}}});
      if (suffix === 'figures/figure_saved/history') return route.fulfill({json:{versions:[]}});
      if (suffix === 'figures/figure_saved/objects') return route.fulfill({json:{objects:[]}});
      if (suffix === 'preview-cards') return route.fulfill({json:{cards:[]}});
      if (suffix === 'figures/figure_saved/preview') return route.fulfill({contentType:'image/svg+xml',body:svg});
      return route.fulfill({status:404,body:''});
    });
    await page.goto(root+'/prefix/workbench/');
    const button = page.locator('#frozen-import');
    await page.locator('#worker-source-note').waitFor({state:'visible'});
    assert.equal(await button.isVisible(),false);
    for (const id of ['import-open','empty-prepare','static-open','file','dependencies']) {
      assert.equal(await page.locator('#'+id).isVisible(),false);
    }
    assert.equal(await page.locator('#return').isVisible(),true);
    offered = true;
    await page.reload();
    await button.waitFor({state:'visible'});
    for (const id of ['import-open','empty-prepare','static-open','file','dependencies']) {
      assert.equal(await page.locator('#'+id).isVisible(),false);
    }
    assert.deepEqual(imports,[]);
    await button.click();
    await button.waitFor({state:'hidden'});
    assert.deepEqual(imports,[['POST',{project_ref:ref,render_version:2}]]);
    await page.reload();
    await button.waitFor({state:'visible'});
    assert.equal(imports.length,1);
    assert.deepEqual(errors,[]);
  } finally { await browser.close(); }
});
