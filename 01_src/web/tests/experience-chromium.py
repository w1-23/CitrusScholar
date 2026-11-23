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


class Browser:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.errors = []
        self.frames = []

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
        check('Queue rank and no running indicator',
              f"({text('.turn-queue-state')}).includes('第 3 位') && !document.querySelector('[data-state=waiting]')",
              timeout=30)
        capture('queued')
        phase('start')
        check('One indicator shown at execution start', "document.querySelectorAll('[data-state=waiting]').length === 1 && document.querySelectorAll('.research-task-orbit i').length === 3")
        elapsed = check('Execution clock populated', f"({text('.research-task-elapsed')}).match(/\\d+/) && ({text('.research-task-elapsed')})")
        capture('waiting')
        browser.call('Page.reload', ignoreCache=True)
        check('Refresh preserves durable clock', "document.querySelector('[data-state=waiting]') && Number((document.querySelector('.research-task-elapsed')?.innerText||'').replace(/\\D/g,'')) >= " + ''.join(filter(str.isdigit, elapsed)))
        browser.click('#ctxCircleBtn')
        browser.click('#ctxUsageBlock summary')
        check('Missing model capacity and cache', f"({text('#ctxBarLabel')}).includes('模型窗口未知') && ({text('#ctxCacheNote')}).includes('未提供')")
        capture('context-missing')
        browser.click('#ctxDrawer button')
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
        check('Explicit zeros with zero denominator suppress cache ratio',
              f"({text('#ctxCacheNote')}).includes('读取：0') && ({text('#ctxCacheNote')}).includes('未缓存：0') && !({text('#ctxCacheNote')}).includes('%')")
        capture('context-zero')
        phase('usage')
        check('Known capacity and provider cache ratio', f"({text('#ctxCacheNote')}).includes('25%') && ({text('#ctxBarLabel')}).includes('131,072')")
        capture('context-reported')
        evaluate("pickTheme('dark'); setReduceMotion(true)")
        check('Reduced motion in dark theme', "document.documentElement.dataset.theme === 'dark' && getComputedStyle(document.querySelector('.research-task-orbit')).animationName === 'none'")
        capture('dark-reduced-motion')
        browser.click('#ctxDrawer button')
        phase('whitespace')
        check('Thirty completed tools preserve metrics and whitespace keeps waiting',
              f"({text('.research-process-body')}).includes('共 30 次尝试') && ({text('#ctxCacheNote')}).includes('25%') && document.querySelector('[data-state=waiting]')")
        phase('answer')
        check('First visible answer stops dots', f"([...document.querySelectorAll('.turn-answer')].some(node => node.textContent.includes('表格核对结果'))) && !document.querySelector('[data-state=waiting]')")
        capture('answer')
        check('Only explicitly public progress is rendered',
              "document.querySelector('.research-process').textContent.includes('公开进度：正在核查列名') && !document.querySelector('#chat-container').textContent.includes('INTERNAL_FIXTURE_MARKER')")
        check('Browse defaults off', "document.querySelector('#browse-requested').checked === false")
        fill('待发送的新消息草稿')
        browser.click('.task-update-toggle')
        check('Task update reuses the single composer and locks new-request controls',
              "document.querySelectorAll('.input-box textarea, .task-controls textarea').length === 1 && !document.querySelector('#composer-supplement').hidden && document.querySelector('#input-tools-menu').hidden && document.querySelector('#composer-browse').hidden && document.activeElement.id === 'user-input'")
        fill('被拒绝的补充')
        evaluate("experienceUpdateMode('conflict')")
        browser.click('#send-btn')
        check('Conflict preserves the update draft without queueing a new turn',
              "experienceRequests().updates.length === 1 && experienceRequests().posts.length === 0 && document.querySelector('#user-input').value === '被拒绝的补充' && document.querySelector('#composer-task-status').textContent.includes('核对最新状态')")
        fill('回执会丢失的补充')
        evaluate("experienceUpdateMode('lost', true)")
        browser.click('#send-btn')
        check('Lost update receipt uses its own ID and preserves the draft',
              "experienceRequests().updates.length === 2 && experienceRequests().lookups[0] === experienceRequests().updates[1].update_request_id && document.querySelector('#user-input').value === '回执会丢失的补充' && !document.querySelector('#send-btn').disabled")
        browser.call('Page.reload', ignoreCache=True)
        check('Reload resumes in new-message mode and keeps that draft',
              "document.querySelector('#user-input')?.value === '待发送的新消息草稿' && document.querySelector('#composer-supplement').hidden && document.querySelector('.task-update-toggle:not([hidden])')")
        evaluate("experienceUpdateMode('lost', false)")
        browser.click('.task-update-toggle')
        check('Lookup after reload confirms the original update without reposting',
              "experienceRequests().updates.length === 2 && experienceRequests().lookups.at(-1) === experienceRequests().updates[1].update_request_id && document.querySelector('#user-input').value === '' && !document.querySelector('#send-btn').disabled")
        fill('终态保留的补充草稿')
        switch_session('另一个会话')
        check('Different session receives an independent new-message draft',
              "document.querySelector('#user-input').value === '' && document.querySelector('#composer-supplement').hidden")
        fill('另一个会话的草稿')
        switch_session('表格核对')
        check('Returning restores only the original new-message draft',
              "document.querySelector('#user-input').value === '待发送的新消息草稿' && document.querySelector('.task-update-toggle:not([hidden])')")
        browser.click('.task-update-toggle')
        check('The same job restores its own update draft', "document.querySelector('#user-input').value === '终态保留的补充草稿'")
        phase('done')
        check('Terminal state displays completion', "document.querySelector('.turn-answer[data-display-status=completed]') && !document.querySelector('.is-running') && true")
        capture('completed')
        check('Terminal job restores new-message mode and makes the unsubmitted update readable',
              "document.querySelector('#composer-supplement').hidden && document.querySelector('#user-input').value === '待发送的新消息草稿' && document.querySelector('.task-update-recovery:not([hidden]) p').textContent === '终态保留的补充草稿'")
        browser.click('#input-tools-menu summary')
        check('The plus menu exposes attachment selection and the real methods catalog',
              "document.querySelector('#attachment-btn') && document.querySelector('[data-skill-id=data-audit]')")
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
        browser.click('#send-btn')
        check('Send associates the screenshot and a frozen method with only its queue entry',
              "experienceRequests().posts[0]?.attachments[0]?.name.endsWith('.png') && experienceRequests().posts[0].selected_skills[0]?.id === 'data-audit' && !('browse_requested' in experienceRequests().posts[0]) && document.querySelector('.sent-attachment-gallery img') && document.querySelector('#selected-skills').hidden")
        browser.click('#browse-requested')
        fill('第二条独立消息')
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
        check('Unavailable queue retains known rows and reports the sync gap',
              "document.querySelector('#composer-task-status').textContent.includes('队列状态待同步') && document.querySelectorAll('.turn-queue-cancel').length === 3")
        evaluate('experienceQueueAvailability(true)')
        browser.click('.turn-queue-cancel')
        check('A single queue cancellation preserves other queued requests',
              "document.querySelector('#chat-container').textContent.includes('已取消，这条消息不会执行') && document.querySelectorAll('.turn-queue-cancel').length === 2")
        evaluate('experienceScale(100, 10000)')
        started = time.monotonic()
        browser.call('Page.reload', ignoreCache=True)
        check('Reload restores a cancellation before dispatch',
              "document.querySelector('.turn-answer[data-display-status=cancelled]')?.textContent.includes('不会执行') && document.querySelectorAll('.turn-queue-cancel').length === 2")
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
        check('Export verifies the saved revision', "experienceRequests().exports[0] === '2'")
        until(lambda: (output / 'fixture.svg').is_file(), 'The fixture SVG did not download')
    except Exception:
        capture('failure')
        diagnostic = evaluate("({ url: location.href, queue: document.querySelector('.turn-queue-state')?.textContent, body: document.body.innerText.slice(-5000), states:[...document.querySelectorAll('[data-state]')].map(x=>x.dataset.state) })")
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
