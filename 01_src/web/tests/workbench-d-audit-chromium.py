"""Real Chromium through Playwright interception and a socket-free ASGI fixture."""
from __future__ import annotations

import argparse
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import median, quantiles
from time import perf_counter

from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

ORIGIN = 'http://workbench-fixture.test'
API = '/api/v2/figure-workbench/projects'


class Interception:
    def __init__(self, requests, responses):
        self.requests = requests
        self.responses = responses
        self.posts = []
        self.mutate = None

    def call(self, method, url, data=None, headers=None):
        if data is not None and not isinstance(data, str):
            data = json.dumps(data)
            headers = {**headers, 'content-type': 'application/json'} if headers else {'content-type': 'application/json'}
        self.requests.write(json.dumps({'method': method, 'url': url, 'content': data, 'headers': headers}) + '\n')
        row = json.loads(self.responses.readline())
        row['body'] = base64.b64decode(row['body'])
        return row

    def route(self, route):
        request = route.request
        if not request.url.startswith(ORIGIN + '/'):
            route.abort()
            return
        url = request.url.removeprefix(ORIGIN)
        if request.method == 'POST':
            self.posts.append((url, request.post_data_json))
        result = self.call(request.method, url, request.post_data, dict(request.headers))
        if self.mutate:
            self.mutate(url, result)
        route.fulfill(**result)


def project(api, title):
    projects = json.loads(api.call('GET', API)['body'])['projects']
    row = next(item for item in projects if item['title'] == title)
    return f"{API}/{row['figure_id']}"


def open_project(page, path):
    start = perf_counter()
    page.goto(ORIGIN + '/figure-workbench?project=' + path.rsplit('/', 1)[1])
    expect(page.locator('#preview')).to_be_visible(timeout=20000)
    return (perf_counter() - start) * 1000


def tree(page, id):
    return page.locator(f'[role=treeitem][data-group="{id}"]')


def chosen(page):
    return page.locator('[role=treeitem][aria-selected=true]').get_attribute('data-group')


def geometry(page, obj, *, min_hits=1, expected=None):
    return page.evaluate('''({ids,minHits,expected}) => {
      const mount = document.querySelector('#inkScene').shadowRoot;
      const svg = mount.querySelector('svg');
      const image = document.querySelector('#preview');
      const canvas = document.querySelector('#canvas');
      const view = canvas.getBoundingClientRect();
      const ui = window.CitrusTheme.scale.effective();
      const vp = (p, ctm) => new DOMPoint(p.x,p.y).matrixTransform(ctm);
      function visible(point) {
        return point.x > view.left && point.x < view.right && point.y > view.top && point.y < view.bottom &&
          document.elementFromPoint(point.x, point.y) === image;
      }
      const wanted = new Set(ids);
      function grid(shape) {
        const box = shape.getBBox();
        const points = [];
        for (let i = 0; i <= 20; i++) for (let j = 0; j <= 12; j++) {
          points.push({x: box.x + box.width*i/20, y: box.y + box.height*j/12});
        }
        return points;
      }
      for (const id of ids) {
        const group = mount.getElementById(id);
        if (!group) continue;
        const shapes = group.matches('path,rect,circle,ellipse,line,polyline,polygon,text') ? [group] :
          [...group.querySelectorAll('path,rect,circle,ellipse,line,polyline,polygon,text')];
        for (const shape of shapes) {
          const box = shape.getBBox();
          const origin = vp({x:box.x+box.width/2, y:box.y+box.height/2}, shape.getScreenCTM());
          canvas.scrollLeft += (origin.x-(view.left+view.right)/2)/ui;
          canvas.scrollTop += (origin.y-(view.top+view.bottom)/2)/ui;
          const ctm = shape.getScreenCTM();
          const points = shape.getPointAtLength ? [...Array(32)].map((_,i)=>shape.getPointAtLength(shape.getTotalLength()*(i+.5)/32)) : grid(shape);
          points.push(...grid(shape));
          for (const point of points) {
            const raw = vp(point, ctm);
            const screen = {x: Math.round(raw.x), y: Math.round(raw.y)};
            if (!visible(screen)) continue;
            const hits = mount.elementsFromPoint(screen.x, screen.y).filter(node => node.tagName === 'path' || node.tagName === 'text' || node === shape);
            if (!hits.some(node => node === shape || shape.contains(node)) || hits.length < minHits) continue;
            if (expected && hits[0].closest(`[id="${id}"]`) === null) continue;
            if (shape.tagName === 'text') {
              const mask = new window.FigureHitScene(new DOMParser().parseFromString(new XMLSerializer().serializeToString(svg), 'image/svg+xml'), {objects:ids.map(gid=>({gid}))},false);
              if (!mask.hits(mask.point(screen.x,screen.y,svg.getBoundingClientRect()),false).some(id=>wanted.has(id))) continue;
            }
            return {x:screen.x,y:screen.y,id};
          }
        }
      }
      throw new Error(`No ink point for ${ids}, minHits=${minHits}`);
    }''', {'ids': obj['geometry']['svgIds'], 'minHits': min_hits, 'expected': expected})


def transformed(page, id):
    return page.evaluate("id => document.querySelector('#inkScene').shadowRoot.getElementById(id).getAttribute('transform')", id)


def image(page):
    return Image.open(BytesIO(page.locator('#canvas').screenshot())).convert('RGB')


def metrics(values):
    return {'n': len(values), 'p50_ms': round(median(values), 2),
            'p95_ms': round(quantiles(values, n=20, method='inclusive')[-1], 2) if len(values) > 1 else round(values[0], 2)}


def verify_geometry(page):
    """Probe known ink and blank coordinates with live Chromium SVG geometry."""
    actual = page.evaluate("""async () => {
      const {SvgScene} = await import('/assets/workbench/scene.js?v=1');
      const mount = document.createElement('div');
      mount.style.cssText='position:fixed;left:0;top:0;width:200px;height:200px;z-index:100;pointer-events:none';
      document.body.append(mount);
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
        <defs><clipPath id="clip"><rect x="0" y="0" width="25" height="100"/></clipPath></defs>
        <path id="clipped" d="M0 20H40V40H0Z" fill="red" clip-path="url(#clip)"/>
        <path id="hole" d="M50 20H90V45H50Z M60 25H80V40H60Z" fill="blue" fill-rule="evenodd"/>
        <path id="dashed" d="M10 60H90" fill="none" stroke="black" stroke-width="2" stroke-dasharray="10 10"/>
        <rect id="invisible" x="50" y="50" width="30" height="40" fill="red" opacity="0"/>
        <rect id="only-stroke" x="20" y="75" width="20" height="15" fill="red" fill-opacity="0" stroke="blue"/>
      </svg>`;
      const ids = ['clipped','hole','dashed','invisible','only-stroke'];
      const scene = new SvgScene(mount, svg, Object.fromEntries(ids.map(id=>[id,{geometry:{svgIds:[id]}}])));
      const probes = [[18,30],[31,30],[53,32],[70,32],[15,60],[25,60],[65,54],[25,84],[20,84]];
      const matrix = scene.svg.getScreenCTM();
      const result = probes.map(([x,y]) => {
        const screen = new DOMPoint(x,y).matrixTransform(matrix);
        return scene.ownersAt(screen.x,screen.y);
      });
      mount.remove();
      return result;
    }""")
    assert actual == [['clipped'], [], ['hole'], [], ['dashed'], [], [], [], ['only-stroke']], actual


def measure_api(api, path, revision, source, id, property, values):
    saved, drafted = [], []
    for i, value in enumerate(values, 1):
        body = {'expected_revision': revision, 'draft_revision': 100 + i, 'source_sha256': source,
                'commands': [{'group_id': id, 'property': property, 'value': value}]}
        for route, record in [('preview', drafted), ('save', saved)]:
            start = perf_counter()
            response = api.call('POST', f'{path}/{route}', body)
            record.append((perf_counter() - start) * 1000)
            assert response['status'] == 200, response['body']
        revision += 1
    return {'api_preview': metrics(drafted), 'api_save': metrics(saved)}


def run(browser, api, output):
    output.mkdir(exist_ok=True)
    context = browser.new_context(viewport={'width': 1470, 'height': 920}, accept_downloads=True)
    context.route('**/*', api.route)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    first, clicks, frames, previews, saves = [], [], [], [], []
    r_first, r_clicks, r_previews, r_saves = [], [], [], []
    path = project(api, 'TSS synthetic reconstruction')
    detail = json.loads(api.call('GET', path)['body'])
    objects = detail['object_contract']['objects']
    title = next(value for value in objects.values() if value['role'] == 'title')
    errorbars = [value for value in objects.values() if value['role'] == 'errorbar' and value['geometry']['svgIds']]
    lines = [value for value in objects.values() if value['role'] == 'line' and value['geometry']['svgIds']]
    assert len(errorbars) == 3 and len(lines) == 3
    assert api.call('GET', path, headers={'x-fixture-owner': '48'})['status'] == 404
    for _ in range(5):
        first.append(open_project(page, path))
    expect(page.locator('#picking')).to_be_enabled()
    assert not page.evaluate('Boolean(globalThis.crypto?.subtle)')
    verify_geometry(page)
    for zoom in [125, 150, 190]:
        if zoom != 190:
            page.locator(f'[data-ui-scale-pick="{zoom}"]').click()
        field = page.get_by_label('自定义画布缩放百分比')
        field.fill(str(zoom))
        page.get_by_role('button', name='应用画布缩放').click()
        point = geometry(page, lines[-1], min_hits=2)
        for _ in range(4):
            page.evaluate('''() => {
              const selection = document.querySelector('#groupTree');
              window.pickMs = null;
              let start;
              document.querySelector('#preview').addEventListener('click', () => {start = performance.now();}, {once:true,capture:true});
              const watcher = new MutationObserver(() => {if (start != null) {window.pickMs = performance.now()-start; watcher.disconnect();}});
              watcher.observe(selection, {childList:true});
            }''')
            page.mouse.click(point['x'], point['y'])
            clicks.append(page.evaluate('window.pickMs'))
        before = chosen(page)
        page.keyboard.down('Alt')
        page.mouse.click(point['x'], point['y'])
        page.keyboard.up('Alt')
        assert chosen(page) != before
    # An uncertainty bounded by actual paint must remain unselected, even
    # though it lies inside the broad data series bounding box.
    start_selection = chosen(page)
    blank = page.evaluate("""() => {
      const img = document.querySelector('#preview');
      const box = img.getBoundingClientRect();
      const view = document.querySelector('#canvas').getBoundingClientRect();
      const x = (view.left + view.right) / 2, y = (view.top + view.bottom) / 2 + 35;
      const r = document.querySelector('#inkScene').shadowRoot;
      for(let dx = 0; dx < 50; dx++) for(let dy = 0; dy < 20; dy++) {
        const px = x + dx, py = y + dy;
        if (document.elementFromPoint(px, py) === img && px > box.left && r.elementsFromPoint(px, py)
          .every(node => node.tagName !== 'path' || node.getAttribute('fill') === '#ffffff')) return [px,py];
      }
      throw Error('No line-adjacent blank point available');
    }""")
    page.mouse.click(*blank)
    assert chosen(page) == start_selection
    page.get_by_role('button', name='适合窗口').click()
    point = geometry(page, title)
    # Establish selected ink before comparing screenshots so the affected pixels
    # demonstrate movement of the title, rather than only a new highlight.
    page.mouse.click(point['x'], point['y'])
    assert chosen(page) == title['objectId'], (point, title, chosen(page), page.evaluate("""p => {
      const r=document.querySelector('#inkScene').shadowRoot;
      return {nodes:r.elementsFromPoint(p.x,p.y).map(n=>[n.tagName,n.id,n.parentElement?.id]),
              canvas:document.querySelector('#canvas').getBoundingClientRect().toJSON()};
    }""", point))
    original = image(page)
    target = point['id']
    initial_transform = transformed(page, target)
    posts = len(api.posts)
    page.evaluate('''() => {
      window.frameTimes = [];
      let pending = false;
      let start;
      const image = document.querySelector('#preview');
      image.addEventListener('pointermove', () => {if (!pending) start = performance.now();}, {capture: true});
      image.addEventListener('pointermove', () => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(() => {
          window.frameTimes.push(performance.now() - start);
          pending = false;
        });
      });
    }''')
    page.mouse.move(point['x'], point['y'])
    page.mouse.down()
    for n in range(1, 19):
        page.mouse.move(point['x'] + n * 2, point['y'] + n * 1.5)
        page.evaluate('new Promise(requestAnimationFrame)')
    assert transformed(page, target) != initial_transform
    assert ImageChops.difference(original, image(page)).getbbox()
    assert len(api.posts) == posts
    frames.extend(page.evaluate('window.frameTimes')[1:])
    start = perf_counter()
    page.mouse.up()
    expect(page.locator('#status')).to_have_text('草稿预览已更新。', timeout=20000)
    previews.append((perf_counter() - start) * 1000)
    assert len(api.posts) == posts + 1
    draft = api.posts[-1][1]
    assert draft['commands'] == [{'group_id': title['objectId'], 'property': 'offset_px',
                                  'value': draft['commands'][0]['value']}]
    assert draft['source_sha256'] == detail['source_sha256']
    moved_point = geometry(page, title)
    baseline_transform = transformed(page, target)
    page.mouse.move(moved_point['x'], moved_point['y'])
    page.mouse.down()
    page.mouse.move(moved_point['x'] + 12, moved_point['y'] + 8, steps=4)
    page.evaluate('new Promise(requestAnimationFrame)')
    assert transformed(page, target) != baseline_transform
    page.keyboard.press('Escape')
    page.mouse.up()
    assert len(api.posts) == posts + 1 and transformed(page, target) == baseline_transform

    page.get_by_role('button', name='撤销', exact=True).click()
    expect(page.locator('#status')).to_have_text('已恢复保存的图件。', timeout=20000)
    expect(page.get_by_role('button', name='撤销', exact=True)).to_be_disabled()
    page.get_by_role('button', name='重做', exact=True).click()
    expect(page.locator('#status')).to_have_text('草稿预览已更新。', timeout=20000)

    series = lines[0]
    point = geometry(page, series, min_hits=2)
    page.mouse.click(point['x'], point['y'])
    if chosen(page) not in {value['objectId'] for value in lines}:
        raise AssertionError('Expected the overlaid marker to select a series')
    series_id = chosen(page)
    picker = page.get_by_role('button', name='打开系列颜色色板')
    picker.click()
    dialog = page.get_by_role('dialog', name='系列颜色色板')
    dialog.get_by_role('slider', name='色相').press('ArrowRight')
    dialog.get_by_role('textbox', name='HEX').fill('#1d557a')
    expect(dialog.get_by_label('R', exact=True)).to_have_value('29')
    expect(dialog.get_by_label('G', exact=True)).to_have_value('85')
    expect(dialog.get_by_label('B', exact=True)).to_have_value('122')
    dialog.get_by_label('B', exact=True).fill('255')
    expect(dialog.get_by_label('新颜色')).to_have_text('#1d55ff')
    dialog.get_by_role('button', name='取消', exact=True).click()
    expect(picker).to_be_focused()
    assert page.get_by_label('系列颜色', exact=True).input_value() != '#1d55ff'
    picker.click()
    dialog.get_by_label('HEX', exact=True).fill('#1d557a')
    dialog.get_by_role('button', name='使用此颜色').click()
    picker.click()
    expect(dialog.get_by_role('button', name='#1d557a')).to_be_visible()
    dialog.press('Escape')
    page.get_by_role('radio', name='菱形', exact=True).press('Space')
    page.get_by_role('radio', name='虚线', exact=True).press('Space')
    page.get_by_label('填充方式', exact=True).select_option('none')
    start = perf_counter()
    page.get_by_role('button', name='应用到此组').click()
    expect(page.locator('#status')).to_have_text('草稿预览已更新。', timeout=20000)
    previews.append((perf_counter() - start) * 1000)
    competing = {**draft, 'commands': [{**draft['commands'][0], 'value': [2, 3]}]}
    assert api.call('POST', path + '/save', {**competing, 'source_sha256': '0' * 64})['status'] == 409
    assert api.call('POST', path + '/save', competing)['status'] == 200
    page.get_by_role('button', name='保存图件').click()
    expect(page.locator('#status')).to_contain_text('草稿仍在本页', timeout=20000)
    expect(page.locator('#revision')).to_have_text('版本 1 · 草稿')
    page.get_by_role('button', name='载入最新版并恢复草稿').click()
    expect(page.locator('#status')).to_have_text('草稿预览已更新。', timeout=20000)
    assert api.posts[-1][1]['expected_revision'] == 2
    start = perf_counter()
    page.get_by_role('button', name='保存图件').click()
    expect(page.locator('#status')).to_have_text('已保存版本 3。', timeout=20000)
    saves.append((perf_counter() - start) * 1000)
    page.reload()
    expect(page.locator('#revision')).to_have_text('版本 3')
    committed = json.loads(api.call('GET', path + '?revision=3')['body'])
    assert committed['source_sha256'] == detail['source_sha256']
    assert next(group for group in committed['groups'] if group['id'] == series_id)['values']['marker'] == 'D'
    page.locator('#exports summary').click()
    for fmt, prefix in [('SVG', b'<?xml'), ('PNG', b'\x89PNG'), ('PDF', b'%PDF-')]:
        with page.expect_download(timeout=25000) as download:
            page.get_by_role('button', name=fmt, exact=False).click()
        saved = download.value.path().read_bytes()
        response = api.call('GET', path + f'/export/{fmt.lower()}?revision=3')
        assert saved.startswith(prefix) and sha256(saved).hexdigest() == response['headers']['x-figure-sha256']
    page.locator('#exports summary').click()
    page.screenshot(path=str(output / 'tss-saved.png'))

    native = project(api, 'Native frozen two-panel')
    initial = json.loads(api.call('GET', native)['body'])
    two = [g for g in initial['groups'] if g['kind'] == 'series']
    assert len({g['parent'] for g in two}) == 2
    payload = {'expected_revision': 1, 'draft_revision': 24, 'source_sha256': initial['source_sha256'],
               'commands': [{'group_id': two[0]['id'], 'property': 'color', 'value': '#5b1258'}]}
    assert api.call('POST', native + '/save', payload)['status'] == 200
    native2 = json.loads(api.call('GET', native)['body'])
    assert next(g for g in native2['groups'] if g['id'] == two[0]['id'])['values']['color'] == '#5b1258'
    assert next(g for g in native2['groups'] if g['id'] == two[1]['id'])['values'] == two[1]['values']

    r_path = project(api, 'Pinned ggplot2')
    for _ in range(5):
        r_first.append(open_project(page, r_path))
    r_detail = json.loads(api.call('GET', r_path)['body'])
    assert 'svglite' in r_detail['presentation_note']
    r_original = r_detail['original_exports']['ggplot.svg']
    original = api.call('GET', r_original['download_url'])['body']
    r_obj = next(r_detail['object_contract']['objects'][g['id']] for g in r_detail['groups']
                 if g['native_tag'] == 'polyline')
    r_point = geometry(page, r_obj, expected=r_obj['objectId'])
    for _ in range(5):
        start = perf_counter()
        page.mouse.click(r_point['x'], r_point['y'])
        assert chosen(page) == r_obj['objectId']
        r_clicks.append((perf_counter() - start) * 1000)
    labels = [g for g in r_detail['groups'] if g['native_tag'] == 'text']
    r_edit = next(g for g in labels if 'Pinned ggplot2 response' in g['label'])
    tree(page, r_edit['id']).locator(':scope > .tree-row').click()
    expect(page.get_by_label('颜色', exact=True)).to_be_visible()
    assert 'fontfamily' not in r_edit['properties'] and 'text' not in r_edit['properties']
    page.get_by_label('颜色', exact=True).fill('#386294')
    start = perf_counter()
    page.get_by_role('button', name='应用到此组').click()
    expect(page.locator('#status')).to_have_text('草稿预览已更新。', timeout=20000)
    r_previews.append((perf_counter() - start) * 1000)
    start = perf_counter()
    page.get_by_role('button', name='保存图件').click()
    expect(page.locator('#status')).to_have_text('已保存版本 2。', timeout=20000)
    r_saves.append((perf_counter() - start) * 1000)
    assert api.call('GET', r_original['download_url'])['body'] == original
    assert sha256(original).hexdigest() == r_detail['source_sha256']
    page.reload()
    expect(page.locator('#revision')).to_have_text('版本 2')
    assert b'#386294' in api.call('GET', r_path + '/export/svg?revision=2')['body']
    page.set_viewport_size({'width': 390, 'height': 760})
    expect(page.get_by_role('button', name='对象组', exact=True)).to_be_visible()
    page.screenshot(path=str(output / 'r-narrow.png'))

    ordinary = project(api, 'Ordinary SVG')
    open_project(page, ordinary)
    expect(page.get_by_role('link', name='旧版 SVG 编辑器')).to_be_visible()
    bitmap = project(api, 'Bitmap PNG')
    open_project(page, bitmap)
    expect(page.locator('#picking')).to_be_disabled()
    expect(page.locator('#status')).to_contain_text('PNG 位图')
    assert not json.loads(api.call('GET', bitmap)['body'])['groups']
    page.set_viewport_size({'width': 1470, 'height': 920})
    tss_url = path + '?revision=3'
    saved_svg = api.call('GET', path + '/preview/svg?revision=3')['body']
    saved_detail = json.loads(api.call('GET', tss_url)['body'])
    tss_objects = saved_detail['object_contract']['objects']
    title_gid = next(entry for entry in tss_objects.values() if entry['role'] == 'title')['geometry']['svgIds'][0]
    series_gid = next(entry for entry in tss_objects.values() if entry['role'] == 'line')['geometry']['svgIds'][0]
    duplicate = saved_svg.replace(f'id="{title_gid}"'.encode(), f'id="{series_gid}"'.encode(), 1)
    assert duplicate != saved_svg
    duplicate_hash = sha256(duplicate).hexdigest()

    def duplicate_response(url, response):
        if url == tss_url:
            altered = {**saved_detail, 'object_contract': {**saved_detail['object_contract'], 'renderKey': duplicate_hash}}
            response['body'] = json.dumps(altered).encode()
            response['headers']['content-length'] = str(len(response['body']))
        if url == path + '/preview/svg?revision=3':
            response['body'] = duplicate
            response['headers'].update({'x-figure-sha256': duplicate_hash, 'content-length': str(len(duplicate))})

    api.mutate = duplicate_response
    page.goto(ORIGIN + '/figure-workbench?project=' + path.rsplit('/', 1)[1] + '&revision=3')
    expect(page.locator('#preview')).to_be_visible()
    expect(page.locator('#picking')).to_be_disabled()
    expect(page.locator('#groupTree')).to_be_visible()

    def wrong_digest(url, response):
        if url == path + '/preview/svg?revision=3':
            response['body'] = duplicate
            response['headers']['content-length'] = str(len(duplicate))

    api.mutate = wrong_digest
    page.reload()
    expect(page.locator('#status')).to_contain_text('摘要不一致')
    expect(page.locator('#preview')).to_be_visible()
    expect(page.locator('#picking')).to_be_disabled()
    assert page.locator('#inkScene').evaluate('node => !node.shadowRoot?.querySelector("svg")')
    api.mutate = None
    assert not errors, errors
    tss_repeats = measure_api(api, path, 3, detail['source_sha256'], title['objectId'], 'offset_px', [[i, 5] for i in range(6)])
    r_repeats = measure_api(api, r_path, 2, r_detail['source_sha256'], r_edit['id'], 'color', [f'#5122{i:02x}' for i in range(6)])
    evidence = {'tss_synthetic': {'first_view': metrics(first), 'click_to_highlight_dom': metrics(clicks),
                                  'pointer_to_frame_callback': metrics(frames), 'authoritative_preview': metrics(previews),
                                  'save_visible': metrics(saves), **tss_repeats},
                'r_ggplot2': {'first_view': metrics(r_first), 'click_to_selected_playwright': metrics(r_clicks),
                              'authoritative_preview': metrics(r_previews), 'save_visible': metrics(r_saves), **r_repeats},
                'browser': browser.version}
    (output / 'metrics.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence))
    context.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('requests', type=Path)
    parser.add_argument('responses', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--chromium', required=True)
    options = parser.parse_args()
    with options.requests.open('w', buffering=1) as requests, options.responses.open() as responses, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=options.chromium,
                                             args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-background-networking'])
        run(browser, Interception(requests, responses), options.output)
        browser.close()
