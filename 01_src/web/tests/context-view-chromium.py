"""Test the real chat shell with browser-local context fixtures."""
import argparse
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

WEB = Path(__file__).resolve().parent.parent
ORIGIN = 'http://context-fixture.test'
SNAPSHOT = {
    'snapshot_version': 1, 'scope': 'context_snapshot', 'status': 'complete', 'seq': 11,
    'session_id': 'observed', 'request_id': 'synthetic-request', 'logical_call_id': 'synthetic-call',
    'attempt_id': 'synthetic-attempt', 'estimate_scope': 'full_request_estimate',
    'estimate_method': 'character_heuristic', 'estimated_input_tokens': 23332,
    'input_limit': 128000, 'model_window': None, 'model_window_status': 'unknown', 'output_tokens': 4096,
    'model': 'hidden-fixture-model', 'view_scope': 'active', 'history_load_status': 'loaded',
    'actual_output_tokens': 4096, 'attempt_elapsed_ms': 5000,
    'task_budget': {'calls': 2, 'max_calls': 8, 'tokens': 4000, 'max_tokens': 9000},
    'components': {'system': 2962, 'messages': 914, 'tool_arguments': 0, 'tool_schemas': 19432, 'images': 0, 'protocol': 24},
    'provider_usage': {'scope': 'single_call', 'logical_call_id': 'synthetic-call', 'attempt_id': 'synthetic-attempt',
                       'input_tokens': 24354, 'output_tokens': 1858, 'cache_read': 0},
}


def assert_fully_expanded(drawer):
    geometry = drawer.evaluate("""el => {
      const body = el.querySelector('.ctx-drawer-body'), rect = el.getBoundingClientRect();
      const checks = [
        ['.ctx-drawer-header', ['.ctx-drawer-title', '.ctx-view-picker', '#ctxClose']],
        ['.ctx-drawer-body', ['#ctxTreemap', '#ctxGrid', '#ctxLegend', '#ctxCategoryDetail', '#ctxUsage']],
        ['#ctxLegend', ['.context-legend-row']],
        ['.context-legend-row', ['.ctx-item-name', '.ctx-item-value', '.ctx-item-percent']],
        ['.ctx-item-name', ['span']]
      ];
      const clipped = checks.flatMap(([parent, children]) => [...el.querySelectorAll(parent)].flatMap(container => {
        if (!container.getClientRects().length) return [];
        const box = container.getBoundingClientRect();
        const css = getComputedStyle(container), zoom = container.currentCSSZoom || 1;
        const left = box.left + parseFloat(css.paddingLeft) * zoom;
        const right = box.right - parseFloat(css.paddingRight) * zoom;
        return children.flatMap(selector => [...container.querySelectorAll(selector)].flatMap(node => {
          if (!node.getClientRects().length) return [];
          const rect = node.getBoundingClientRect();
          return rect.left < left - 1 || rect.right > right + 1 || node.scrollWidth > node.clientWidth + 1
            ? [[selector, rect.left, rect.right, left, right, node.scrollWidth, node.clientWidth]] : [];
        }));
      }));
      return {left: rect.left, right: rect.right, bottom: rect.bottom, clipped,
              width: innerWidth, height: innerHeight,
              overflowX: Math.max(el.scrollWidth - el.clientWidth, body.scrollWidth - body.clientWidth),
              overflowY: Math.max(el.scrollHeight - el.clientHeight, body.scrollHeight - body.clientHeight)};
    }""")
    assert 0 <= geometry['left'] < geometry['right'] <= geometry['width'] + 1, geometry
    assert geometry['bottom'] <= geometry['height'] + 1, geometry
    assert geometry['overflowX'] <= 1 and geometry['overflowY'] <= 1 and not geometry['clipped'], geometry


def verify(page, width, output):
    page.set_default_timeout(8000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def respond(route):
        if not route.request.url.startswith(ORIGIN + '/'):
            route.abort()
            return
        path = urlparse(route.request.url).path
        if not path.startswith('/api/'):
            asset = (WEB / ('index.html' if path == '/' else path.lstrip('/'))).resolve()
            if asset.is_relative_to(WEB) and asset.is_file():
                route.fulfill(path=asset)
            else:
                route.fulfill(status=404)
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
    page.goto(ORIGIN + '/', wait_until='domcontentloaded')
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
    ring.focus()
    expect(ring).to_be_focused()
    page.keyboard.press('Enter')
    expect(ring).to_have_attribute('aria-expanded', 'true')
    expect(page.locator('#ctxClose')).to_be_focused()
    expect(drawer.locator('#ctxUsed, #ctxValueNote, #ctxStatus, #ctxMetadata')).to_have_count(0)
    for hidden in ('23.3K / 128K', '估算输入 ·', 'hidden-fixture-model', '当前调用', '历史已加载',
                   '应用单次输入上限', '输出预留', '实际发包输出上限', '本次调用用时', '本轮预算'):
        expect(drawer).not_to_contain_text(hidden)
    expect(page.locator('#ctxTreemap')).to_be_visible()
    assert_fully_expanded(drawer)
    free = page.locator('#ctxTreemap [data-category="free"]')
    assert abs(free.evaluate('el => parseFloat(el.style.width) * parseFloat(el.style.height) / 10000') - 104668 / 128000) < 1e-6
    page.screenshot(path=output / f'treemap-{width}.png', animations='disabled')
    row = page.locator('#ctxLegend [data-category="tool_schemas"]')
    row.focus()
    page.keyboard.press('Enter')
    expect(row).to_have_attribute('aria-expanded', 'true')
    expect(page.locator('#ctxCategoryDetail')).to_contain_text('19,432 令牌')
    expect(page.locator('#ctxCategoryDetail')).to_contain_text('工具名称、说明和参数定义')
    page.locator('#ctxViewTreemap').focus()
    page.keyboard.press('ArrowRight')
    expect(page.locator('#ctxViewGrid')).to_be_checked()
    expect(page.locator('#ctxGrid')).to_be_visible()
    expect(page.locator('#ctxGrid i')).to_have_count(100)
    expect(page.locator('#ctxGrid i.is-used')).to_have_count(18)
    expect(page.locator('#ctxGrid .ctx-slot--1')).to_have_count(82)
    assert_fully_expanded(drawer)
    page.screenshot(path=output / f'grid-{width}.png', animations='disabled')
    provider = page.locator('#ctxUsage summary')
    provider.focus()
    page.keyboard.press('Space')
    expect(page.locator('#ctxUsageRows')).to_contain_text('24,354')
    expect(page.locator('#ctxUsageRows')).to_contain_text('缓存读取')
    expect(page.locator('#ctxUsageRows')).not_to_contain_text('缓存覆盖率')
    assert_fully_expanded(drawer)
    page.screenshot(path=output / f'expanded-{width}.png', animations='disabled')
    if width >= 1025:
        page.evaluate("const files = document.getElementById('sessionFilesPanel'); files.style.visibility = 'hidden'; files.hidden = false; document.body.classList.add('session-files-open')")
        dock_geometry = "[document.querySelector('#ctxDrawer').getBoundingClientRect().bottom, document.querySelector('#sessionFilesPanel').getBoundingClientRect().top, document.body.style.getPropertyValue('--context-dock-height')]"
        drawer_bottom, files_top, observed_height = page.evaluate(dock_geometry)
        assert drawer_bottom <= files_top, (drawer_bottom, files_top, observed_height)
        assert_fully_expanded(drawer)
        page.evaluate("document.getElementById('sessionFilesPanel').hidden = true; document.body.classList.remove('session-files-open')")
    page.keyboard.press('Escape')
    expect(ring).to_be_focused()
    page.evaluate("switchSession('unknown')")
    expect(ring).to_have_text('23.3K')
    assert page.locator('#ctxBtnFg').get_attribute('stroke-dasharray') == '0 100'
    ring.click()
    expect(page.locator('#ctxChartNote')).to_contain_text('不表示窗口占用率')
    expect(page.locator('#ctxCategoryDetail')).to_be_hidden()
    expect(page.locator('#ctxGrid .ctx-slot--1')).to_have_count(0)
    expect(page.locator('#ctxUsage')).to_be_hidden()
    page.keyboard.press('Escape')
    page.evaluate("switchSession('empty')")
    expect(ring).to_have_text('—')
    ring.click()
    expect(page.locator('#ctxChartNote')).to_have_text('暂无可显示的上下文构成。')
    expect(page.locator('#ctxTreemap')).to_be_hidden()
    expect(page.locator('#ctxGrid')).to_be_hidden()
    expect(page.locator('#ctxLegend')).to_be_hidden()
    expect(page.locator('#ctxViewGrid')).to_be_disabled()
    assert not errors, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium', type=Path, help='Use an already installed browser')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, args=['--no-sandbox', '--disable-gpu'])
        for width, height in ((1360, 860), (390, 760), (320, 570)):
            page = browser.new_page(viewport={'width': width, 'height': height}, has_touch=width < 600)
            verify(page, width, args.output)
            print(f'PASS: context layout, keyboard, provider data, session transitions, {width}x{height}', flush=True)
            page.close()
        browser.close()


if __name__ == '__main__':
    main()
