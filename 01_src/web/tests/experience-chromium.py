"""Exercise the real static UI in Chrome with isolated task and account responses.

Serve 01_src/web on a loopback origin, then run this script with --origin,
--chrome and --output. The Chrome profile is temporary. Requires websocket-client
and Pillow. Evidence goes to the explicitly selected output directory.
"""
import argparse
import base64
import json
from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from PIL import Image
import websocket


EXPORT_PATH = '/api/v2/figures/projects/imported-project/export/svg?v=2'
SAVED_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="360" height="180" viewBox="0 0 360 180">'
             '<text id="fixture-title" x="20" y="40">已更新标题</text></svg>').encode('utf-8')


class Browser:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.errors = []
        self.frames = []
        self.export_requests = []

    def respond_to_export(self, event):
        request = event['request']
        method = request['method']
        self.export_requests.append((method, request['url']))
        if method not in {'HEAD', 'GET'}:
            self.send('Fetch.fulfillRequest', requestId=event['requestId'], responseCode=405)
            return
        self.send('Fetch.fulfillRequest', requestId=event['requestId'], responseCode=200,
                  responseHeaders=[{'name': 'Content-Type', 'value': 'image/svg+xml'},
                                   {'name': 'Content-Disposition', 'value': 'attachment; filename="fixture.svg"'},
                                   {'name': 'Content-Length', 'value': str(len(SAVED_SVG))}],
                  body=base64.b64encode(SAVED_SVG if method == 'GET' else b'').decode('ascii'))

    def send(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({'id': self.next_id, 'method': method, 'params': params}))
        return self.next_id

    def call(self, method, **params):
        request = self.send(method, **params)
        while True:
            message = json.loads(self.ws.recv())
            event = message.get('method')
            data = message.get('params', {})
            if event == 'Page.screencastFrame':
                self.frames.append((data['metadata']['timestamp'], base64.b64decode(data['data'])))
                self.send('Page.screencastFrameAck', sessionId=data['sessionId'])
            if event == 'Fetch.requestPaused':
                self.respond_to_export(data)
            if event == 'Runtime.exceptionThrown':
                self.errors.append(data['exceptionDetails'].get('exception', {}).get('description', data))
            if event == 'Runtime.consoleAPICalled' and data['type'] == 'error':
                self.errors.append([arg.get('value', arg.get('description')) for arg in data['args']])
            if message.get('id') == request:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                return message.get('result', {})

    def evaluate(self, expression):
        response = self.call('Runtime.evaluate', expression=expression, returnByValue=True, awaitPromise=True)
        if 'exceptionDetails' in response:
            raise RuntimeError(response['result'].get('description', response['exceptionDetails']))
        return response['result'].get('value')

    def click(self, selector):
        deadline = time.monotonic() + 12
        diagnostic = None
        while time.monotonic() < deadline:
            diagnostic = self.evaluate(f'''(() => {{
              const target = document.querySelector({json.dumps(selector)});
              if (!target) return {{missing:true}};
              target.scrollIntoView({{block:'nearest'}});
              const box = target.getBoundingClientRect();
              const x = box.x + box.width / 2, y = box.y + box.height / 2;
              const at = document.elementFromPoint(x, y);
              const style = getComputedStyle(target);
              return {{
                x, y,
                rect: {{x:box.x, y:box.y, width:box.width, height:box.height}},
                viewport: {{width:innerWidth, height:innerHeight, scrollX, scrollY}},
                visible: style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0,
                at: at ? `${{at.tagName.toLowerCase()}}#${{at.id}}.${{at.className}}` : null,
                hit: !!at && target.contains(at)
              }};
            }})()''')
            if diagnostic and diagnostic.get('hit'):
                point = {'x': diagnostic['x'], 'y': diagnostic['y']}
                break
            time.sleep(0.15)
        else:
            raise AssertionError(f'Click target is hidden or covered: {selector}: {diagnostic}')
        for event in ('mousePressed', 'mouseReleased'):
            self.call('Input.dispatchMouseEvent', type=event, button='left', clickCount=1, **point)


def until(check, description, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.15)
    raise AssertionError(description)


def run(browser, origin, output):
    script = Path(__file__).with_name('experience-fixture.js').read_text(encoding='utf-8')
    browser.call('Runtime.enable')
    browser.call('Page.enable')
    browser.call('Network.enable')
    browser.call('Page.setDownloadBehavior', behavior='allow', downloadPath=str(output.resolve()))
    export_url = origin + EXPORT_PATH
    browser.call('Fetch.enable', patterns=[{'urlPattern': export_url}])
    browser.call('Emulation.setDeviceMetricsOverride', width=1360, height=900, deviceScaleFactor=1, mobile=False)
    browser.call('Page.addScriptToEvaluateOnNewDocument', source=script)
    browser.call('Page.startScreencast', format='jpeg', quality=82, maxWidth=1100, everyNthFrame=2)
    snapshots, results = [], []
    evaluate = browser.evaluate

    def check(label, expression, *, timeout=12):
        try:
            result = until(lambda: evaluate(f'(() => {{ const value = ({expression}); return value instanceof Element ? true : value; }})()'), expression, timeout=timeout)
            results.append({'check': label, 'status': 'PASS', 'observed': result})
            return result
        except AssertionError:
            results.append({'check': label, 'status': 'FAIL', 'expression': expression})
            raise

    def capture(title):
        path = output / f'{len(snapshots) + 1:02d}-{title}.png'
        path.write_bytes(base64.b64decode(browser.call('Page.captureScreenshot', format='png')['data']))
        snapshots.append(path)

    def phase(name):
        evaluate(f'experienceStep({json.dumps(name)})')

    def fill(value):
        evaluate(f"""(() => {{
          const input = document.querySelector('#user-input');
          input.value = {json.dumps(value, ensure_ascii=False)};
          input.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})()""")

    def press_enter():
        evaluate("document.querySelector('#user-input').focus()")
        for event in ('keyDown', 'keyUp'):
            browser.call('Input.dispatchKeyEvent', type=event, key='Enter', code='Enter', windowsVirtualKeyCode=13)

    def switch_session(title):
        selector = f'.session-title[title="{title}"]'
        check(f'Session listed: {title}', f'document.querySelector({json.dumps(selector)})')
        evaluate(f'document.querySelector({json.dumps(selector)}).click()')

    def offline(value):
        browser.call('Network.emulateNetworkConditions', offline=value, latency=0, downloadThroughput=-1, uploadThroughput=-1)

    def text(selector):
        return f'document.querySelector({json.dumps(selector)})?.textContent||""'

    try:
        browser.call('Page.navigate', url=origin + '/')
        check('Only the user bubble displays a muted pending badge before dispatch',
              "document.querySelector('.message.user .turn-user-queue-state')?.textContent === '待发送' "
              "&& document.querySelectorAll('.turn-user-queue-state').length === 1 "
              "&& getComputedStyle(document.querySelector('.message.bot.turn-awaiting-dispatch')).display === 'none' "
              "&& !document.querySelector('.turn-queue-cancel, [data-state=waiting]') "
              "&& !document.querySelector('#chat-container').innerText.includes('第 3 位')",
              timeout=30)
        capture('queued')
        phase('start')
        check('One indicator shown at execution start', "document.querySelectorAll('[data-state=waiting]').length === 1 && document.querySelectorAll('.research-task-orbit i').length === 3 && !document.querySelector('.turn-user-queue-state, .turn-awaiting-dispatch')")
        elapsed = check('Execution clock populated', f"({text('.research-task-elapsed')}).match(/\\d+/) && ({text('.research-task-elapsed')})")
        capture('waiting')
        browser.call('Page.reload', ignoreCache=True)
        check('Refresh preserves durable clock', "document.querySelector('[data-state=waiting]') && Number((document.querySelector('.research-task-elapsed')?.innerText||'').replace(/\\D/g,'')) >= " + ''.join(filter(str.isdigit, elapsed)))
        browser.click('#ctxCircleBtn')
        check('Unknown capacity shows only the known count', f"({text('#ctxUsed')}) === '9.2K' && ({text('#ctxValueNote')}).includes('容量未知') && document.querySelector('#ctxViewGrid').disabled")
        capture('context-missing')
        browser.click('#ctxClose')
        phase('tool')
        check('Running tool replaces dot phase', "document.querySelector('[data-state=tool]') && document.querySelector('.is-running')")
        capture('tool')
        offline(True)
        check('Disconnect stops the running display', "document.querySelector('[data-state=disconnected]') && !document.querySelector('.is-running')")
        capture('disconnected')
        offline(False)
        check('Reconnection restores the tool display', "document.querySelector('[data-state=tool]') && document.querySelector('.is-running')")
        phase('zero')
        browser.click('#ctxCircleBtn')
        check('Provider reporting does not overwrite context occupancy',
              f"({text('#ctxUsed')}) === '9.2K' && !document.querySelector('#ctxCacheNote')")
        capture('context-zero')
        phase('usage')
        check('Known capacity is displayed with the estimate', f"({text('#ctxUsed')}) === '10.4K / 131K' && ({text('#ctxRingPct')}) === '8%'")
        capture('context-reported')
        evaluate("pickTheme('navy'); setReduceMotion(true)")
        check('Reduced motion in dark theme', "document.documentElement.dataset.theme === 'navy' && getComputedStyle(document.querySelector('.research-task-orbit')).animationName === 'none'")
        capture('dark-reduced-motion')
        browser.click('#ctxClose')
        phase('whitespace')
        check('Thirty completed tools preserve metrics and whitespace keeps waiting',
              f"({text('.research-process-body')}).includes('共 30 次尝试') && ({text('#ctxUsed')}) === '10.4K / 131K' && document.querySelector('[data-state=waiting]')")
        phase('answer')
        check('First visible answer stops dots', f"([...document.querySelectorAll('.turn-answer')].some(node => node.textContent.includes('表格核对结果'))) && !document.querySelector('[data-state=waiting]')")
        capture('answer')
        check('Only explicitly public progress is rendered',
              "document.querySelector('.research-process').textContent.includes('公开进度：正在核查列名') && !document.querySelector('#chat-container').textContent.includes('INTERNAL_FIXTURE_MARKER')")
        check('Only cited sources appear in the answer bubble and duplicate Markdown links are removed',
              "Array.from(document.querySelectorAll('.message.bot .turn-citation-bubbles .turn-citation-bubble'), el => el.textContent).join('|') === '联网：公开表格说明|本地：本地表格字典' "
              "&& document.querySelector('.turn-citation-bubbles a')?.href === 'https://example.org/cited-table' "
              "&& Array.from(document.querySelectorAll('.turn-answer a'), el => el.href).join('|') === 'https://example.org/ordinary' "
              "&& !document.querySelector('.cr-turn-sources') && experienceRequests().sourceReads.length === 0")
        check('The only composer action is Stop while the original task is executing',
              "document.querySelectorAll('.input-box textarea, .task-controls textarea').length === 1 "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '停止当前任务' "
              "&& document.querySelector('#send-btn use').getAttribute('href') === '/assets/citrus-icons.svg#ui-stop'  "
              "&& !document.querySelector('#stop-btn, #composer-supplement, #composer-new-message') "
              "&& !document.querySelector('#input-tools-menu').hidden && !document.querySelector('#browse-requested').checked")
        fill('保留在原会话的独立草稿')
        switch_session('另一个会话')
        check('Different session receives an independent new-message draft',
              "document.querySelector('#user-input').value === '' && document.querySelector('#send-btn').getAttribute('aria-label') === '发送消息'")
        fill('另一个会话的草稿')
        switch_session('表格核对')
        check('Returning restores the original draft and the executing job control',
              "document.querySelector('#user-input').value === '保留在原会话的独立草稿' "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '停止当前任务'")
        browser.click('#input-tools-menu summary')
        check('The plus menu exposes only separate attachment choices',
              "document.querySelectorAll('#attachment-options [data-composer-source]').length === 3 && !document.querySelector('#input-tools-menu #skill-picker-panel')")
        browser.click('#skill-picker-menu summary')
        check('A separate composer control opens the real methods catalog',
              "!document.querySelector('#input-tools-menu').open && document.querySelector('[data-skill-id=data-audit]')")
        browser.click('[data-skill-id=data-audit]')
        evaluate('''(() => {
          const input = document.querySelector('#user-input');
          const buffer = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRzsAAAAASUVORK5CYII='), c => c.charCodeAt(0));
          const clip = new DataTransfer();
          clip.items.add(new File([buffer], 'clipboard', {type:'image/png'}));
          input.value = '检查粘贴截图';
          input.dispatchEvent(new ClipboardEvent('paste', {clipboardData: clip, bubbles:true, cancelable:true}));
          input.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', isComposing:true, bubbles:true, cancelable:true}));
        })()''')
        check('Synthetic image paste shows a thumbnail and composing Enter does not send',
              "document.querySelector('#attachment-list img') && experienceRequests().posts.length === 0")
        press_enter()
        check('Enter associates the screenshot and frozen method with one independent queue entry',
              "experienceRequests().posts.length === 1 && experienceRequests().posts[0]?.attachments[0]?.name.endsWith('.png') && experienceRequests().posts[0].selected_skills[0]?.id === 'data-audit' && experienceRequests().posts[0].selected_skills[0]?.version === 'fixture-v1' && experienceRequests().posts[0].selected_skills[0]?.sha256 === 'a'.repeat(64) && !('browse_requested' in experienceRequests().posts[0]) "
              "&& document.querySelector('.sent-attachment-gallery img') && document.querySelector('#selected-skills').hidden "
              "&& document.querySelectorAll('.message.user .turn-user-queue-state').length === 1 "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '停止当前任务' && experienceRequests().updates.length === 0")
        browser.click('#composer-browse')
        fill('第二条独立消息')
        browser.click('#send-btn')
        check('Clicking Stop cancels just the executing job and does not submit the draft or cancel its follower',
              "experienceRequests().cancels.length === 1 && experienceRequests().cancels[0] === experienceActiveJob "
              "&& experienceRequests().queueDeletes.length === 0 && experienceRequests().posts.length === 1 "
              "&& document.querySelector('#user-input').value === '第二条独立消息' && document.querySelector('#browse-requested').checked "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '正在停止当前任务'")
        phase('done')
        check('Terminal answer returns the shared control to Send with the draft preserved',
              "document.querySelector('.turn-answer[data-display-status=completed]') && !document.querySelector('.is-running') "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '发送消息' "
              "&& document.querySelector('#user-input').value === '第二条独立消息'")
        capture('completed')
        browser.click('#send-btn')
        check('Separate FIFO admissions keep their request identity',
              "experienceRequests().posts.length === 2 && experienceRequests().posts[0].client_request_id !== experienceRequests().posts[1].client_request_id && experienceRequests().posts[1].query === '第二条独立消息'")
        check('Browse opt-in is sent only for the explicitly selected request',
              "experienceRequests().posts[1].browse_requested === true && document.querySelector('#browse-requested').checked === false")
        fill('第三条独立消息')
        browser.click('#send-btn')
        check('Subsequent new request omits the Browse flag',
              "experienceRequests().posts.length === 3 && !('browse_requested' in experienceRequests().posts[2])")
        evaluate('experienceQueueAvailability(false)')
        check('Unavailable queue retains known user bubbles and reports the sync gap',
              "document.querySelector('#composer-task-status').textContent.includes('消息状态待同步') "
              "&& document.querySelectorAll('.message.user .turn-user-queue-state').length === 3")
        evaluate('experienceQueueAvailability(true)')
        check('Queue polling recovers without additional submissions or cancellation actions',
              "document.querySelector('#composer-task-status').hidden && experienceRequests().posts.length === 3 "
              "&& experienceRequests().queueDeletes.length === 0 && !document.querySelector('.turn-queue-cancel')")
        evaluate('experienceScale(100, 10000)')
        started = time.monotonic()
        browser.call('Page.reload', ignoreCache=True)
        check('Reload restores all pending user bubbles and no duplicate requests',
              "document.querySelectorAll('.message.user .turn-user-queue-state').length === 3 "
              "&& [...document.querySelectorAll('.message.bot.turn-awaiting-dispatch')].length === 3 "
              "&& experienceRequests().posts.length === 3 && experienceRequests().updates.length === 0 "
              "&& experienceRequests().queueDeletes.length === 0 && !document.querySelector('.turn-queue-cancel')")
        count = check('Long fixture renders its supplied history',
                      "document.querySelectorAll('.message').length >= 200 && document.querySelectorAll('.message').length",
                      timeout=30)
        results.append({'measurement': 'fixture reload to rendered chat rows (ms)', 'rows': count, 'value': round((time.monotonic() - started) * 1000)})
        browser.call('Page.stopScreencast')
        samples = evaluate('''(async () => {
          const samples = [];
          for (let i = 0; i < 3; i++) {
            const start = performance.now();
            if (!await restoreSessionHistory()) throw Error('Fixture history restore failed');
            samples.push(Math.round(performance.now() - start));
          }
          return samples;
        })()''')
        results.append({'measurement': 'fixture history restore with recording paused (ms)', 'rows': count, 'samples': samples})
        browser.call('Page.startScreencast', format='jpeg', quality=82, maxWidth=1100, everyNthFrame=2)
        started = time.monotonic()
        browser.click('#sessionFilesRailBtn')
        check('Library renders a 50-file batch from 10,000 entries', "document.querySelectorAll('#sessionFilesUploaded .session-file-item').length === 50")
        results.append({'measurement': 'fixture library click to 50 cards from 10,000 entries (ms)', 'value': round((time.monotonic() - started) * 1000)})
        capture('long-history-library')
        browser.click('.session-file-list-more')
        check('Load more appends only the next 50 file cards', "document.querySelectorAll('#sessionFilesUploaded .session-file-item').length === 100")
        browser.click('[data-file-act=preview][data-file-id=fixture-file-0]')
        check('Library CSV preview preserves quoted commas and newlines',
              "[...document.querySelectorAll('.sf-table td')].some(cell => cell.textContent === 'north, south') && [...document.querySelectorAll('.sf-table td')].some(cell => cell.textContent.includes('two\\r\\nlines') || cell.textContent.includes('two\\nlines'))")
        capture('library-preview')
        browser.click('#sessionFilePreviewOverlay .overlay-close')
        check('The preview modal closes before navigating to the workbench', "document.querySelector('#sessionFilePreviewOverlay').hidden")
        browser.click('#figureRailBtn')
        check('The real workbench entry navigates to /figures', "location.pathname === '/figures' && document.querySelector('#workbench')")
        check('The workbench opens with its inspector collapsed', "document.querySelector('#rightSidebar')?.hidden")
        evaluate("if (document.querySelector('#leftSidebar').hidden) document.querySelector('#toggleLeft').click()")
        check('The figure drawer opens through its real toolbar control',
              "!document.querySelector('#leftSidebar').hidden && document.querySelector('#toggleLeft').getAttribute('aria-expanded') === 'true' && document.querySelectorAll('#projectList [data-open]').length === 30")
        for expected in (60, 90, 120, 122):
            browser.click('#loadMoreProjects')
            check(f'Project paging reaches {expected} entries', f"document.querySelectorAll('#projectList [data-open]').length === {expected}")
        check('Desktop import opens on entry', "!document.querySelector('#importPanel').hidden && document.querySelector('#importToggle').getAttribute('aria-expanded') === 'true'")
        browser.click('#importToggle')
        check('The import panel can be collapsed', "document.querySelector('#importPanel').hidden")
        browser.click('#importToggle')
        check('CSV origin bytes are shown before mapping', "document.querySelector('#importPreviewBody')?.textContent.includes('north, south') && !document.querySelector('#importMapping').disabled")
        evaluate("const picker = document.querySelector('#dataFileSelect'); picker.value = 'workbook'; picker.dispatchEvent(new Event('change',{bubbles:true}));")
        check('Workbook preview reads real cells from the server-selected sheet', "document.querySelector('#importPreviewBody').textContent.includes('1\\t42') && document.querySelector('#importPreviewNote').textContent.includes('Measurements')")
        evaluate("const metric = document.querySelector('#importY'); metric.value = 'Value'; metric.dispatchEvent(new Event('change',{bubbles:true}));")
        browser.click('#importButton')
        check('Mapping review does not create a project', "!document.querySelector('#importConfirm').hidden && experienceRequests().imports.length === 0")
        capture('figure-preview-and-confirm')
        browser.click('#importConfirmButton')
        check('Confirm sends exactly one mapped file and selects the returned project',
              "experienceRequests().imports.length === 1 && experienceRequests().imports[0].file_id === 'workbook' && experienceRequests().imports[0].mapping.y === 'Value' && document.querySelector('#projectTitle').textContent.includes('隔离夹具')")
        check('Project creation closes the import drawer', "document.querySelector('#leftSidebar')?.hidden")
        capture('figure-created')
        browser.click('#toggleLeft')
        browser.click('#objectTree [data-gid=fixture-title]')
        evaluate("const editor = document.querySelector('#propertyPanel [data-prop=text]'); editor.value = '已更新标题'; editor.dispatchEvent(new Event('input', {bubbles: true})); editor.dispatchEvent(new Event('change', {bubbles: true}));")
        browser.click('#saveButton')
        check('The workbench saves edits against the current revision',
              "experienceRequests().saves.length === 1 && experienceRequests().saves[0].expected_revision === 1 && experienceRequests().saves[0].overrides.some(edit => edit.gid === 'fixture-title' && edit.value === '已更新标题') && document.querySelector('#downloadSvg').href.endsWith('/export/svg?v=2')")
        browser.click('#exportMenu summary')
        browser.click('#downloadSvg')
        downloaded = output / 'fixture.svg'

        def download_completed():
            evaluate('true')
            return downloaded.is_file()

        until(download_completed, 'The fixture SVG did not download')
        assert browser.export_requests == [('HEAD', export_url), ('GET', export_url)], browser.export_requests
        assert downloaded.read_bytes() == SAVED_SVG, 'Downloaded SVG does not match the saved revision bytes'
        results.append({'check': 'Export verifies and downloads the saved SVG revision', 'status': 'PASS',
                        'observed': {'revision': 2, 'methods': ['HEAD', 'GET'], 'bytes': len(SAVED_SVG)}})
    except Exception:
        capture('failure')
        diagnostic = evaluate('''({url: location.href,
          queue: [...document.querySelectorAll('.turn-user-queue-state')].map(el => el.textContent),
          composer: {label: document.querySelector('#send-btn')?.getAttribute('aria-label'),
            draft: document.querySelector('#user-input')?.value,
            textareas: document.querySelectorAll('.input-box textarea, .task-controls textarea').length,
            legacyControls: !!document.querySelector('#stop-btn, #composer-supplement, #composer-new-message')},
          body: document.body.innerText.slice(-5000), states: [...document.querySelectorAll('[data-state]')].map(el => el.dataset.state)})''')
        (output / 'failure.json').write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    finally:
        browser.call('Page.stopScreencast')
        if browser.frames:
            images = [Image.open(BytesIO(data)).convert('RGB') for _, data in browser.frames]
            times = [stamp for stamp, _ in browser.frames]
            durations = [max(40, round((end - start) * 1000)) for start, end in zip(times, times[1:])] + [1000]
            images[0].save(output / 'chromium-recording.webp', save_all=True, append_images=images[1:], duration=durations, loop=0)
        evidence = {'checks': results, 'console_errors': browser.errors, 'screencast_frames': len(browser.frames)}
        (output / 'operations.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chrome', required=True)
    parser.add_argument('--origin', default='http://127.0.0.1:8165')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    assert urlparse(origin).hostname in {'127.0.0.1', 'localhost'}, 'Only use a loopback fixture origin'
    args.output.mkdir(parents=True, exist_ok=True)
    opener = build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix='citrus-chromium-', ignore_cleanup_errors=True) as profile:
        chrome = subprocess.Popen([args.chrome, '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
                                   '--remote-debugging-port=0', f'--user-data-dir={profile}', 'about:blank'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = Path(profile) / 'DevToolsActivePort'
            until(port_file.exists, 'Chrome did not start')
            port = port_file.read_text().splitlines()[0]
            with opener.open(f'http://127.0.0.1:{port}/json/list', timeout=5) as response:
                target = next(item for item in json.load(response) if item['type'] == 'page')
            ws = websocket.create_connection(target['webSocketDebuggerUrl'], timeout=15, suppress_origin=True, http_no_proxy=['127.0.0.1'])
            try:
                run(Browser(ws), origin, args.output)
            finally:
                ws.close()
        finally:
            chrome.terminate()
            chrome.wait(timeout=10)


if __name__ == '__main__':
    main()
