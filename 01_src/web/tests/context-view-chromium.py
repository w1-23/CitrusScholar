"""Test the real chat shell with context metadata only and a loopback fixture."""
import argparse
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

WEB = Path(__file__).resolve().parent.parent
SNAPSHOT = {
    'snapshot_version': 1, 'scope': 'context_snapshot', 'status': 'complete', 'seq': 11,
    'session_id': 'observed', 'request_id': 'synthetic-request', 'logical_call_id': 'synthetic-call',
    'attempt_id': 'synthetic-attempt', 'estimate_scope': 'full_request_estimate',
    'estimate_method': 'character_heuristic', 'estimated_input_tokens': 23332,
    'input_limit': 128000, 'model_window': None, 'model_window_status': 'unknown', 'output_tokens': 4096,
    'components': {'system': 2962, 'messages': 914, 'tool_arguments': 0, 'tool_schemas': 19432, 'images': 0, 'protocol': 24},
    'provider_usage': {'scope': 'single_call', 'logical_call_id': 'synthetic-call', 'attempt_id': 'synthetic-attempt',
                       'input_tokens': 24354, 'output_tokens': 1858, 'cache_read': 0},
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def assert_inner_bounds(drawer):
    overflow = drawer.evaluate("""el => {
      const checks = [
        ['.ctx-drawer-header', ['.ctx-view-picker', '#ctxClose']],
        ['.ctx-drawer-body', ['#ctxTreemap', '#ctxGrid', '#ctxLegend', '#ctxCategoryDetail', '#ctxUsage']]
      ];
      return checks.flatMap(([parent, children]) => {
        const container = el.querySelector(parent), box = container.getBoundingClientRect();
        const css = getComputedStyle(container), zoom = container.currentCSSZoom || 1;
        const left = box.left + parseFloat(css.paddingLeft) * zoom;
        const right = box.right - parseFloat(css.paddingRight) * zoom;
        return children.flatMap(selector => {
          const node = el.querySelector(selector), rect = node.getBoundingClientRect();
          return !node.hidden && (rect.left < left - 1 || rect.right > right + 1 || node.scrollWidth > node.clientWidth + 1)
            ? [[selector, rect.left, rect.right, left, right, node.scrollWidth, node.clientWidth]] : [];
        });
      });
    }""")
    assert not overflow, overflow


def verify(page, origin, width, output):
    page.set_default_timeout(8000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def respond(route):
        if not route.request.url.startswith(origin + '/'):
            route.abort()
            return
        path = urlparse(route.request.url).path
        if not path.startswith('/api/'):
            route.continue_()
            return
        body = {}
        if path == '/api/v2/auth/me':
            body = {'user': {'user_id': 'context-fixture', 'username': '本地测试', 'role': 'user', 'onboarding_completed': True}}
        elif path == '/api/v2/sessions':
            body = {'sessions': [{'session_id': sid, 'title': sid, 'msg_count': 1} for sid in ('observed', 'unknown', 'empty')]}
        elif path.endswith('/messages'):
            sid = path.split('/')[-2]
            body = {'messages': [], 'turns': [], 'has_more': False}
            if sid == 'observed':
                body['context'] = SNAPSHOT
            elif sid == 'unknown':
                body['context'] = {**SNAPSHOT, 'session_id': 'unknown', 'input_limit': None, 'provider_usage': {}}
        elif path.endswith('/turn-queue'):
            body = {'turns': []}
        route.fulfill(json=body)

    page.route('**/*', respond)
    page.add_init_script("localStorage.setItem('citrus_session_id_context-fixture', 'observed')")
    page.goto(origin + '/', wait_until='domcontentloaded')
    ring, drawer = page.locator('#ctxCircleBtn'), page.locator('#ctxDrawer')
    expect(ring).to_have_text('18%')
    page.keyboard.press('Tab')
    textarea = page.locator('#user-input')
    textarea.focus()
    assert textarea.evaluate('el => el.matches(":focus-visible") && getComputedStyle(el).outlineStyle === "none"')
    focus = page.locator('.input-box').evaluate('el => ({border:getComputedStyle(el).borderTopColor, focus:getComputedStyle(el).getPropertyValue("--ui-focus").trim()})')
    assert focus == {'border': 'rgb(189, 97, 40)', 'focus': '#BD6128'}, focus
    page.screenshot(path=output / f'composer-{width}.png', animations='disabled')
    assert abs(float(page.locator('#ctxBtnFg').get_attribute('stroke-dasharray').split()[0]) - 23332 / 128000 * 100) < 1e-8
    ring.click()
    expect(page.locator('#ctxUsed')).to_have_text('23.3K / 128K')
    expect(page.locator('#ctxValueNote')).to_contain_text('应用单次输入上限')
    expect(page.locator('#ctxMetadata')).to_contain_text('模型窗口未知')
    expect(page.locator('#ctxTreemap')).to_be_visible()
    assert_inner_bounds(drawer)
    assert drawer.evaluate('el => {const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth + 1 && el.scrollWidth <= el.clientWidth;}')
    free = page.locator('#ctxTreemap [data-category="free"]')
    assert abs(free.evaluate('el => parseFloat(el.style.width) * parseFloat(el.style.height) / 10000') - 104668 / 128000) < 1e-6
    page.screenshot(path=output / f'treemap-{width}.png', animations='disabled')
    page.locator('#ctxLegend [data-category="tool_schemas"]').click()
    expect(page.locator('#ctxCategoryDetail')).to_contain_text('19,432 令牌')
    expect(page.locator('#ctxCategoryDetail')).to_contain_text('工具名称、说明和参数定义')
    page.locator('#ctxViewGrid').check()
    expect(page.locator('#ctxGrid')).to_be_visible()
    expect(page.locator('#ctxGrid i')).to_have_count(100)
    expect(page.locator('#ctxGrid i.is-used')).to_have_count(18)
    expect(page.locator('#ctxGrid .ctx-slot--1')).to_have_count(82)
    assert_inner_bounds(drawer)
    page.screenshot(path=output / f'grid-{width}.png', animations='disabled')
    page.locator('#ctxUsage summary').click()
    expect(page.locator('#ctxUsageRows')).to_contain_text('缓存读取')
    expect(page.locator('#ctxUsageRows')).not_to_contain_text('缓存覆盖率')
    page.keyboard.press('Escape')
    expect(ring).to_be_focused()
    page.evaluate("switchSession('unknown')")
    expect(ring).to_have_text('23.3K')
    assert page.locator('#ctxBtnFg').get_attribute('stroke-dasharray') == '0 100'
    ring.click()
    expect(page.locator('#ctxUsed')).to_have_text('23.3K')
    expect(page.locator('#ctxChartNote')).to_contain_text('不表示窗口占用率')
    expect(page.locator('#ctxCategoryDetail')).to_be_hidden()
    expect(page.locator('#ctxGrid .ctx-slot--1')).to_have_count(0)
    expect(page.locator('#ctxUsage')).to_be_hidden()
    page.keyboard.press('Escape')
    page.evaluate("switchSession('empty')")
    expect(ring).to_have_text('—')
    ring.click()
    expect(page.locator('#ctxUsed')).to_have_text('—')
    expect(page.locator('#ctxTreemap')).to_be_hidden()
    expect(page.locator('#ctxGrid')).to_be_hidden()
    expect(page.locator('#ctxLegend')).to_be_hidden()
    expect(page.locator('#ctxViewGrid')).to_be_disabled()
    assert not errors, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=WEB)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(args=['--no-sandbox'])
                for width, height in ((1360, 860), (390, 760), (320, 570)):
                    page = browser.new_page(viewport={'width': width, 'height': height}, has_touch=width < 600)
                    verify(page, f'http://127.0.0.1:{server.server_port}', width, args.output)
                    print(f'PASS: composer + context live metadata + session transitions, {width}x{height}', flush=True)
                    page.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
