"""Exercise the real static UI in Chrome with isolated task and account responses.

Run with --chrome and --output. DevTools serves checkout assets and fixture
responses to a virtual origin without starting an HTTP server. The Chrome profile is temporary. Requires websocket-client
and Pillow. Evidence goes to the explicitly selected output directory.
"""
import argparse
import base64
import json
from hashlib import sha256
from io import BytesIO
import mimetypes
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from PIL import Image
import websocket


FIGURE_PATH = '/api/v2/figure-workbench/projects/' + 'a' * 32
EXPORT_PATH = FIGURE_PATH + '/export/svg?revision=2'
WEB = Path(__file__).resolve().parents[1]
SAVED_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="360" height="180" viewBox="0 0 360 180">'
             '<text id="fixture-title" x="20" y="40">已更新标题</text></svg>').encode('utf-8')
ORIGINAL_SVG = SAVED_SVG.decode().replace('已更新标题', '示例标题').encode()


class Browser:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.errors = []
        self.frames = []
        self.export_requests = []

    def respond(self, event):
        request = event['request']
        url = urlparse(request['url'])
        resource = event['requestId']
        method = request['method']
        if url.path in {FIGURE_PATH + '/preview/svg', FIGURE_PATH + '/export/svg'}:
            if url.path.endswith('/export/svg'):
                self.export_requests.append((method, request['url']))
            revision = '2' if url.query == 'revision=2' else '1'
            body = SAVED_SVG if revision == '2' else ORIGINAL_SVG
            headers = {'Content-Type': 'image/svg+xml', 'X-Figure-Revision': revision,
                       'X-Figure-SHA256': sha256(body).hexdigest()}
        else:
            routes = {'/': 'index.html', '/figures': 'figure-workbench.html', '/figures/legacy': 'figures.html'}
            file = (WEB / routes.get(url.path, url.path.lstrip('/'))).resolve()
            if (url.path not in routes and not url.path.startswith('/assets/')) or not file.is_relative_to(WEB) or not file.is_file():
                self.send('Fetch.fulfillRequest', requestId=resource, responseCode=404)
                return
            body = file.read_bytes()
            mime = 'text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file)[0]
            headers = {'Content-Type': mime or 'application/octet-stream'}
        self.send('Fetch.fulfillRequest', requestId=resource, responseCode=200,
                  responseHeaders=[{'name': key, 'value': value} for key, value in headers.items()],
                  body=base64.b64encode(body if method != 'HEAD' else b'').decode('ascii'))

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
                self.respond(data)
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


def run(browser, origin, output, *, observation_only=False, delivery_only=False):
    script = Path(__file__).with_name('experience-fixture.js').read_text(encoding='utf-8')
    browser.call('Runtime.enable')
    browser.call('Page.enable')
    browser.call('Network.enable')
    browser.call('Page.setDownloadBehavior', behavior='allow', downloadPath=str(output.resolve()))
    export_url = origin + EXPORT_PATH
    browser.call('Fetch.enable', patterns=[{'urlPattern': origin + '/*'}])
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
        if delivery_only:
            phase('done')
            browser.call('Page.reload', ignoreCache=True)
            check('One saved answer, its two citations, its artifact and eligible source',
                  "!!document.querySelector('.turn-answer[data-display-status=completed]') && "
                  "document.querySelectorAll('.turn-citation-bubbles .turn-citation-bubble').length === 2 && "
                  "document.querySelectorAll('.turn-file-card').length === 1 && "
                  "document.querySelector('.turn-files-heading')?.textContent === '本轮产出' && "
                  "document.querySelectorAll('.turn-explicit-continuation').length === 1")
            browser.click('.turn-explicit-continuation')
            check('Verified input becomes a named attachment chip',
                  "document.querySelector('.continuation-attachment-reference')?.textContent === 'Measurements.csv'")
            capture('delivery-and-attachment')
            browser.click('#clear-composer-continuation')
            check('Clearing inheritance removes the reference chip',
                  "document.querySelector('#composer-continuation').hidden && !document.querySelector('.continuation-attachment-reference')")
            fill('继续核对表头')
            browser.click('.turn-explicit-continuation')
            browser.click('#send-btn')
            check('The next submission sends the exact verified prior turn',
                  "experienceRequests().posts.length === 1 && experienceRequests().posts[0].continue_from === 'browser-fixture-request'")
            return
        capture('queued')
        phase('start')
        check('One indicator shown at execution start', "document.querySelectorAll('[data-state=waiting]').length === 1 && document.querySelectorAll('.research-task-orbit i').length === 3 && !document.querySelector('.turn-user-queue-state, .turn-awaiting-dispatch')")
        elapsed = check('Execution clock populated', f"({text('.research-task-elapsed')}).match(/\\d+/) && ({text('.research-task-elapsed')})")
        capture('waiting')
        browser.call('Page.reload', ignoreCache=True)
        check('Refresh preserves durable clock', "document.querySelector('[data-state=waiting]') && Number((document.querySelector('.research-task-elapsed')?.innerText||'').replace(/\\D/g,'')) >= " + ''.join(filter(str.isdigit, elapsed)))
        browser.click('#ctxCircleBtn')
        check('Chart uses the application input limit when model capacity is unknown',
              f"({text('#ctxLegend [data-category=free] .ctx-item-value')}) === '76.8K' "
              "&& !document.querySelector('#ctxViewGrid').disabled")
        capture('context-missing')
        browser.click('#ctxClose')
        phase('tool')
        check('Running tool replaces dot phase', "document.querySelector('[data-state=tool]') && document.querySelector('.is-running') && document.querySelector('.research-task-status').textContent.includes('正在执行 ·')")
        capture('tool')
        evaluate("experienceReadFailure('status')")
        check('Online status 503 leaves task running',
              "document.querySelector('[data-state=tool]') && document.querySelector('.research-task-observation:not([hidden])')?.textContent.includes('暂时无法读取任务状态') && navigator.onLine")
        evaluate("experienceReadFailure('replay')")
        check('Online replay 503 reports its own read source',
              "document.querySelector('[data-state=tool]') && document.querySelector('.research-task-observation:not([hidden])')?.textContent.includes('暂时无法补读任务事件') && navigator.onLine")
        evaluate("experienceReadFailure('')")
        check('Distinct read failures clear after successful reads',
              "document.querySelector('[data-state=tool]') && document.querySelector('.research-task-observation').hidden")
        offline(True)
        check('Disconnect stops the running display', "document.querySelector('[data-state=disconnected]') && !document.querySelector('.is-running')")
        capture('disconnected')
        phase('zero')
        offline(False)
        check('Reconnection resumes the prior cursor and displays the missed result',
              "document.querySelector('[data-state=waiting]') && !document.querySelector('.is-running') "
              "&& experienceRequests().replayCursors.includes(3) && experienceRequests().replayCursors.includes(5) "
              "&& experienceRequests().cancels.length === 0 && experienceRequests().posts.length === 0")
        check('Chromium ReadableStream interruption consumes one durable terminal tail', '''
              (async () => {
                const store = CitrusTaskState.create();
                const observations = [], paths = [];
                const started = {job_id: 'synthetic-sse', status: 'running', seq: 18};
                const tail = {seq: 19, event: 'done', data: {turn_envelope: {display_status: 'completed'}}};
                const status = {job_id: 'synthetic-sse', status: 'completed', event_cursor: 19,
                  terminal_snapshot: {display_status: 'completed'}};
                const transport = CitrusTaskTransport.create({store, owns: () => true,
                  onStreamInterrupted: () => observations.push('interruption'),
                  fetch: async path => {
                    paths.push(path);
                    return new Response(JSON.stringify(path.includes('/events?') ? {...status,
                      protocol: 'citrus-task/1', next_cursor: 19, has_more: false,
                      events: [{seq: 18, event: 'job', data: started}, tail]} : status));
                  }});
                let calls = 0;
                const stream = new ReadableStream({pull(controller) {
                  if (calls++ === 0) {
                    controller.enqueue(new TextEncoder().encode('event: job\\ndata: {"job_id":"synthetic-sse","status":"running"}\\n\\n'));
                  } else controller.error(new TypeError('synthetic connection loss'));
                }});
                try {
                  await transport.stream(new Response(stream), new AbortController().signal);
                  return store.state.cursor === 19 && store.state.status === 'completed' &&
                    observations.length === 1 && paths[0].includes('after=0');
                } finally { transport.stop(); }
              })()''')
        browser.click('#ctxCircleBtn')
        check('Provider reporting does not overwrite context occupancy',
              f"({text('#ctxLegend [data-category=free] .ctx-item-value')}) === '76.8K' && !({text('#ctxUsageRows')}).includes('缓存覆盖率')")
        capture('context-zero')
        phase('usage')
        check('Chart uses known model capacity while the header reports app occupancy', f"({text('#ctxLegend [data-category=free] .ctx-item-value')}) === '121K' && ({text('#ctxTriggerPct')}) === '13%'")
        capture('context-reported')
        evaluate("setReduceMotion(true)")
        check('Reduced motion in Porcelain theme', "document.documentElement.dataset.theme === 'porcelain' && getComputedStyle(document.querySelector('.research-task-orbit')).animationName === 'none'")
        capture('porcelain-reduced-motion')
        browser.click('#ctxClose')
        phase('whitespace')
        check('Thirty completed tools preserve metrics and whitespace keeps waiting',
              f"({text('.research-process-body')}).includes('共 30 条尝试记录') && ({text('#ctxLegend [data-category=free] .ctx-item-value')}) === '121K' && document.querySelector('[data-state=waiting]')")
        check('Thirty attempts remain in one collapsed disclosure', "document.querySelectorAll('.research-action-group').length === 1 && !document.querySelector('.research-action-group').open && document.querySelectorAll('.research-action-group .term-line').length === 30")
        phase('answer')
        check('First visible answer stops dots', f"([...document.querySelectorAll('.turn-answer')].some(node => node.textContent.includes('表格核对结果'))) && !document.querySelector('[data-state=waiting]')")
        capture('answer')
        check('Only explicitly public progress is rendered',
              "document.querySelector('.research-process').textContent.includes('核查列名') && !document.querySelector('#chat-container').textContent.includes('INTERNAL_FIXTURE_MARKER')")
        check('One visible group shows verified titles and preserves literal citation examples',
              "Array.from(document.querySelectorAll('.message.bot .turn-citation-bubbles .turn-citation-bubble'), el => el.textContent).join('|') === '联网：公开表格说明|本地：本地表格字典' "
              "&& document.querySelector('.turn-citation-bubbles a')?.href === 'https://example.org/cited-table' "
              "&& Array.from(document.querySelectorAll('.turn-answer a'), el => el.href).join('|') === 'https://example.org/ordinary' "
              "&& document.querySelectorAll('.turn-citation-bubbles[aria-label=本轮引证]').length === 1 "
              "&& document.querySelector('.turn-answer code').textContent === '【w1，w2】' "
              "&& !document.querySelector('.cr-turn-sources') && experienceRequests().sourceReads.length === 0")
        check('The only composer action is Stop while the original task is executing',
              "document.querySelectorAll('.input-box textarea, .task-controls textarea').length === 1 "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '停止当前任务' "
              "&& document.querySelector('#send-btn use').getAttribute('href') === '/assets/citrus-icons.svg#ui-stop'  "
              "&& !document.querySelector('#stop-btn, #composer-supplement, #composer-new-message') "
              "&& !document.querySelector('#input-tools-menu').hidden && !document.querySelector('#browse-requested, #research-method')")
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
        fill('第二条独立消息')
        browser.click('#send-btn')
        check('Clicking Stop cancels just the executing job and does not submit the draft or cancel its follower',
              "experienceRequests().cancels.length === 1 && experienceRequests().cancels[0] === experienceActiveJob "
              "&& experienceRequests().queueDeletes.length === 0 && experienceRequests().posts.length === 1 "
              "&& document.querySelector('#user-input').value === '第二条独立消息' "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '正在停止当前任务'")
        phase('done')
        check('Terminal answer returns the shared control to Send with the draft preserved',
              "document.querySelector('.turn-answer[data-display-status=completed]') && !document.querySelector('.is-running') "
              "&& document.querySelector('#send-btn').getAttribute('aria-label') === '发送消息' "
              "&& document.querySelector('#user-input').value === '第二条独立消息'")
        check('Server timings distinguish admission, provider packet, and visible answer',
              "document.querySelector('.turn-timings')?.textContent.includes('提交到首段可见答复 12.1 秒') && document.querySelector('.turn-timings')?.textContent.includes('模型调用到服务商首包 0.9 秒') && document.querySelector('.turn-timings')?.textContent.includes('队列等待 3.0 秒')")
        check('Verified file card is restored once from the terminal artifact tail',
              "document.querySelectorAll('.turn-file-card').length === 1 "
              "&& document.querySelector('.turn-file-name')?.textContent === '核验结果.csv' "
              "&& document.querySelector('.turn-delivery')?.dataset.verifiedCount === '1'")
        capture('completed')
        if observation_only:
            before = evaluate("document.querySelector('.turn-answer[data-display-status=completed]').innerHTML")
            browser.call('Page.reload', ignoreCache=True)
            check('Reopening history retains the same answer, citation group and file card',
                  f"document.querySelector('.turn-answer[data-display-status=completed]')?.innerHTML === {json.dumps(before)} "
                  "&& document.querySelectorAll('.turn-citation-bubbles .turn-citation-bubble').length === 2 "
                  "&& document.querySelectorAll('.turn-file-card').length === 1")
            check('Reopened history offers only verified attachment inheritance',
                  "document.querySelectorAll('.turn-explicit-continuation').length === 1")
            browser.click('.turn-explicit-continuation')
            check('Verified restored input appears as an attachment reference',
                  "!document.querySelector('#composer-continuation').hidden && document.querySelector('#composer-continuation').textContent.includes('Measurements.csv')")
            capture('history-reopened')
            return
        browser.call('Page.reload', ignoreCache=True)
        check('Completed input is eligible after reload', "!!document.querySelector('.turn-explicit-continuation')")
        browser.click('.turn-explicit-continuation')
        check('Selecting a completed turn makes attachment inheritance explicit', "!document.querySelector('#composer-continuation').hidden")
        browser.click('#clear-composer-continuation')
        check('Cancelling inheritance restores ordinary message mode', "document.querySelector('#composer-continuation').hidden")
        browser.click('#send-btn')
        check('Separate FIFO admissions keep their request identity',
              "experienceRequests().posts.length === 2 && experienceRequests().posts[0].client_request_id !== experienceRequests().posts[1].client_request_id && experienceRequests().posts[1].query === '第二条独立消息' && !('continue_from' in experienceRequests().posts[1])")
        check('Independent requests do not need a Browse flag',
              "experienceRequests().posts.every(post => !('browse_requested' in post))")
        fill('继续核对表头')
        browser.click('.turn-explicit-continuation')
        browser.click('#send-btn')
        check('Explicit continuation sends only the exact selected prior request',
              "experienceRequests().posts.length === 3 && experienceRequests().posts[2].continue_from === 'browser-fixture-request' && !('browse_requested' in experienceRequests().posts[2]) && document.querySelector('#composer-continuation').hidden")
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
        check('The real workbench entry navigates to /figures', "location.pathname === '/figures' && document.querySelector('#groupTree') && !document.querySelector('#importPanel')")
        check('Inspector drawer starts collapsed', "document.querySelector('#toggleInspector')?.getAttribute('aria-expanded') === 'false'")
        browser.click('#openLibrary')
        check('The real workbench dialog lists the first 12 projects', "document.querySelector('#library').open && document.querySelectorAll('#assetList .asset-button').length === 12")
        for expected in (24, 26):
            browser.click('#assetMore')
            check(f'Project paging reaches {expected} entries', f"document.querySelectorAll('#assetList .asset-button').length === {expected}")
        browser.click('#assetList .asset-button[aria-label="隔离夹具图件"]')
        check('Opening a figure verifies the initial preview and closes the picker',
              "!document.querySelector('#library').open && !document.querySelector('#canvasSheet').hidden && document.querySelector('#revision').textContent === '版本 1'")
        evaluate("if (document.querySelector('#toggleGroups').getAttribute('aria-expanded') === 'false') document.querySelector('#toggleGroups').click()")
        browser.click('#groupTree [data-group="category:root:text"] > .tree-row .tree-arrow')
        browser.click('#groupTree [data-group=fixture-title-group] .tree-name')
        check('The group inspector exposes the title field', "document.querySelector('#propertyPanel [name=text]')")
        evaluate("const editor = document.querySelector('#propertyPanel [name=text]'); editor.value = '已更新标题'; editor.dispatchEvent(new Event('input', {bubbles: true}));")
        browser.click('#propertyPanel form button.primary')
        check('The workbench verifies the group draft before enabling Save', "experienceRequests().previews.length === 1 && experienceRequests().previews[0].draft_revision > 0 && !document.querySelector('#save').disabled")
        capture('figure-preview')
        browser.click('#save')
        check('The workbench saves group commands against the current revision',
              "experienceRequests().saves.length === 1 && experienceRequests().saves[0].expected_revision === 1 && experienceRequests().saves[0].commands.some(edit => edit.group_id === 'fixture-title-group' && edit.property === 'text' && edit.value === '已更新标题') && document.querySelector('#revision').textContent === '版本 2'")
        browser.click('#exports summary')
        browser.click('#exportMenu button:first-of-type')
        downloaded = output / '隔离夹具图件-v2.svg'

        def download_completed():
            evaluate('true')
            return downloaded.is_file()

        until(download_completed, 'The fixture SVG did not download')
        assert browser.export_requests == [('GET', export_url)], browser.export_requests
        assert downloaded.read_bytes() == SAVED_SVG, 'Downloaded SVG does not match the saved revision bytes'
        results.append({'check': 'Export verifies and downloads the saved SVG revision', 'status': 'PASS',
                        'observed': {'revision': 2, 'methods': ['GET'], 'sha256': sha256(SAVED_SVG).hexdigest()}})
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
    parser.add_argument('--origin', default='http://citrus-experience.test')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--task-observation-only', action='store_true', help='Stop after chat observation and terminal history recovery.')
    parser.add_argument('--phase1-delivery-only', action='store_true', help='Exercise restored artifact and verified attachment inheritance only.')
    args = parser.parse_args()
    origin = args.origin.rstrip('/')
    assert origin == 'http://citrus-experience.test', 'Only use the isolated virtual fixture origin'
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
                run(Browser(ws), origin, args.output, observation_only=args.task_observation_only,
                    delivery_only=args.phase1_delivery_only)
            finally:
                ws.close()
        finally:
            chrome.terminate()
            chrome.wait(timeout=10)


if __name__ == '__main__':
    main()
