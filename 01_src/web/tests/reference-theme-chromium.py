"""Verify actual reference-theme layout and media with synthetic loopback data."""
import argparse
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


TABLE_ID = '1' * 32
IMAGE_ID = '2' * 32
PDF_ID = '3' * 32
THEMES = ('sage', 'paper', 'navy', 'light', 'cartoon', 'dark')
ROWS = [['样品', '温度', '得率'], ['A02', '28', '94.2'], ['B14', '31', '95.1']]
CSV = '样品,温度,得率\r\nA02,28,94.2\r\nB14,31,95.1\r\n'


def file_record(id_, filename, kind, size, date, origin='uploaded'):
    return {'file_id': id_, 'filename': filename, 'kind': kind, 'size_bytes': size,
            'origin': origin, 'created_at': date, 'last_accessed_at': date,
            'download_url': f'/api/v2/files/{id_}/download', 'preview_url': f'/api/v2/files/{id_}/preview'}


FILES = [file_record(TABLE_ID, '样品温度与实验得率.csv', 'table', 1860, 1720000000),
         file_record(IMAGE_ID, '植物样本照片.webp', 'image', 24398, 1719000000),
         file_record(PDF_ID, '植物生理实验报告.pdf', 'pdf', 153486, 1718000000, 'generated'),
         file_record('4' * 32, '长期实验观察计划.docx', 'docx', 17382, 1717000000)]
SNAPSHOT = {'estimate_scope': 'full_request_estimate', 'estimated_tokens': 28400, 'model_window': 65536,
            'model': 'DeepSeek · fixture', 'input_limit': 55000, 'logical_call_id': 'fixture-call',
            'attempt_id': 'fixture-attempt', 'history_load_status': 'loaded',
            'components': {'messages': 18600, 'system': 4200, 'tool_arguments': 700,
                           'tool_schemas': 2900, 'images': 1400, 'protocol': 600},
            'provider_usage': {'input_tokens': 28103, 'output_tokens': 1875,
                               'cache_read': 4000, 'cache_uncached': 24103}}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def verify(page, output, origin, image_bytes):
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def mock(route):
        url = urlparse(route.request.url)
        if not route.request.url.startswith(origin + '/'):
            route.abort()
            return
        p = url.path
        if not (p.startswith('/api/') or p.startswith('/jobs/')):
            route.continue_()
            return
        requests.append((p, parse_qs(url.query)))
        payload = {}
        if p == '/api/v2/auth/me':
            payload = {'user': {'user_id': 'theme-fixture', 'username': '本地验收', 'role': 'user',
                                'avatar_preset': '02', 'onboarding_completed': True}}
        elif p == '/api/v2/sessions':
            payload = {'sessions': [{'session_id': sid, 'title': title, 'msg_count': 2,
                                     'updated_at': '2026-11-24T15:45:00+08:00'}
                                    for sid, title in [('plants', '植物生理与实验数据'), ('audit', '水稻样本数据核对'), ('methods', '论文方法与参考文献')]]}
        elif p.endswith('/messages'):
            payload = {'messages': [], 'has_more': False, 'turns': [{'turn_request_id': 'saved-1',
                'user': {'content': '请整理植物样本的温度与实验得率，并指出下一步需要核对的内容。'},
                'answer': {'content': '## 实验数据整理\n\n已按样品保留温度和得率字段，样本照片与数据表已放在右侧资料库。\n\n- **字段检查**：温度列以 °C 为单位，得率以百分比表示。\n- **下步核对**：比较 A02 与 B14 的重复实验记录，确认测量条件一致。'},
                'display_status': 'completed'}]}
        elif p.endswith('/turn-queue'):
            payload = {'turns': []}
        elif p == '/api/v2/files':
            payload = {'uploaded': FILES, 'generated': [], 'retention_idle_days': 30}
        elif p == f'/api/v2/files/{TABLE_ID}/table-sample':
            payload = {'rows': ROWS}
        elif p == f'/api/v2/files/{IMAGE_ID}/preview':
            route.fulfill(content_type='image/webp', body=image_bytes)
            return
        elif p == f'/api/v2/files/{TABLE_ID}/download':
            route.fulfill(content_type='text/csv', body=CSV)
            return
        route.fulfill(json=payload)

    page.route('**/*', mock)
    page.add_init_script("localStorage.setItem('citrus_session_id_theme-fixture','plants')")
    page.goto(origin + '/', wait_until='domcontentloaded')
    page.locator('.message.bot h2', has_text='实验数据整理').wait_for()
    assert len(requests) and not any(p.endswith('/table-sample') for p, _ in requests)
    page.locator('#sessionFilesRailBtn').click()
    page.locator('.session-file-table-sample').wait_for()
    assert page.locator('.session-file-table-sample td').all_text_contents() == [cell for row in ROWS for cell in row]
    page.locator('.session-file-thumb img.is-ready').wait_for()
    assert any(p.endswith(f'/{IMAGE_ID}/preview') and q.get('library_tile') == ['1'] for p, q in requests)
    assert not any(p.endswith('/download') for p, _ in requests)
    assert page.locator('.session-file-item').count() == len(FILES)
    assert page.locator(f'[data-file-id="{PDF_ID}"].session-file-item [data-file-act="delete"]').count() == 0
    assert page.locator('#send-btn').count() == 1 and page.locator('#stop-btn').count() == 0
    assert page.locator('#themeRailBtn use').get_attribute('href') == '#ui-palette'
    assert page.locator('#memoryRailBtn use').get_attribute('href') == '#ui-sliders'
    assert page.locator('#figureRailBtn use').get_attribute('href') == '#ui-canvas'
    assert page.locator('.input-box').evaluate('(box) => parseFloat(getComputedStyle(box).borderRadius) >= 12')
    distinct = set()
    for theme in THEMES:
        page.locator('#themeRailBtn').click()
        page.locator(f'[data-theme-pick="{theme}"]').click()
        assert page.locator('html').get_attribute('data-theme') == theme
        page.locator('#themeRailBtn').click()
        assert page.locator('#themeMenu').is_hidden()
        bg = page.locator('.chat-panel').evaluate('el=>getComputedStyle(el).backgroundColor')
        rail = page.locator('.left-rail').evaluate('el=>getComputedStyle(el).backgroundColor')
        sidebar = page.locator('.sidebar').evaluate('el=>getComputedStyle(el).backgroundColor')
        assert rail != sidebar and sidebar != bg
        distinct.add(bg)
        if theme in THEMES[:3]:
            page.screenshot(path=output / f'{theme}-workspace.png', animations='disabled')
            page.evaluate('snapshot => renderContextPanel(snapshot)', SNAPSHOT)
            page.locator('#ctxCircleBtn').click()
            assert page.locator('#ctxBar .ctx-cell').count() == 192
            assert page.locator('#ctxBar .ctx-slot--1').count() == 109
            page.locator('#ctxItems [data-key=messages] .ctx-item-value').wait_for()
            assert page.locator('#ctxItems [data-key=messages] .ctx-item-value').inner_text().replace(',', '') == '18600'
            page.locator('#ctxBar').wait_for()
            assert 'low' in page.locator('#ctxBtnFg').get_attribute('class').split()
            page.screenshot(path=output / f'{theme}-context.png', animations='disabled')
            page.locator('#ctxViewDetails').check()
            assert not page.locator('#ctxBar').is_visible() and page.locator('#ctxItems').is_visible()
            page.locator('#ctxViewDetails').focus()
            page.keyboard.press('ArrowRight')
            assert page.locator('#ctxViewMosaic').is_checked() and page.locator('#ctxBar').is_visible()
            page.keyboard.press('Escape')
            assert page.locator('#ctxCircleBtn').get_attribute('aria-expanded') == 'false'
            assert page.locator('#ctxCircleBtn').evaluate('el=>el===document.activeElement')
    assert len(distinct) == len(THEMES), distinct
    page.evaluate("pickTheme('navy')")
    page.locator(f'[data-file-act="preview"][data-file-id="{TABLE_ID}"]').click()
    page.locator('.sf-table td', has_text='94.2').wait_for()
    page.screenshot(path=output / 'navy-file-preview.png', animations='disabled')
    page.locator('#sessionFilePreviewOverlay .overlay-close').click()
    page.locator('.session-files-close').click()
    page.set_viewport_size({'width': 390, 'height': 800})
    page.context.new_cdp_session(page).send('Emulation.setTouchEmulationEnabled', {'enabled': True})
    page.reload(wait_until='domcontentloaded')
    page.locator('.message.bot h2', has_text='实验数据整理').wait_for()
    assert page.locator('html').get_attribute('data-theme') == 'navy'
    page.locator('#sessionFilesRailBtn').click()
    page.locator('.session-file-table-sample').wait_for()
    assert page.locator('.session-files-close').evaluate('el=>el===document.activeElement')
    assert page.locator('[data-file-act=preview]').first.bounding_box()['height'] >= 44
    page.screenshot(path=output / 'navy-mobile-library.png', animations='disabled')
    page.keyboard.press('Escape')
    assert page.locator('#sessionFilesRailBtn').evaluate('el=>el===document.activeElement')
    page.evaluate('snapshot => renderContextPanel(snapshot)', SNAPSHOT)
    page.locator('#ctxCircleBtn').click()
    page.locator('#ctxBar').wait_for()
    geometry = page.locator('#ctxDrawer').bounding_box()
    assert geometry and geometry['x'] >= 0 and geometry['x'] + geometry['width'] <= 390
    page.screenshot(path=output / 'navy-mobile-context.png', animations='disabled')
    assert not errors, errors
    (output / 'report.json').write_text(json.dumps({'themes': THEMES, 'viewport_width': 390,
        'context_cells': 192, 'free_cells': 109, 'table_cells': 9, 'console_errors': errors}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    web = Path(__file__).resolve().parent.parent
    image_bytes = (web / 'assets/avatars/02.webp').read_bytes()
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=web)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1360, 'height': 900})
                page.set_default_timeout(6000)
                verify(page, args.output, origin, image_bytes)
                browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
