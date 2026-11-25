"""Verify the Porcelain spec and media behavior with synthetic loopback data."""
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
SCALES = (125, 150)
RESEARCH = {'id': 'research-question-answering', 'version': 'frozen-rag', 'sha256': 'e' * 64}
SPRITE = '/assets/citrus-icons.svg#ui-'
SHA = 'f' * 64
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
SNAPSHOT = {'estimate_scope': 'full_request_estimate', 'estimated_tokens': 4300, 'model_window': 10000}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def verify(page, output, origin, image_bytes):
    errors, requests, posts = [], [], []
    history = [{'turn_request_id': f'saved-{i}',
        'user': {'message_id': i * 2 - 1, 'content': f'第{i}轮样品核对'},
        'answer': {'message_id': i * 2, 'content': '## 实验数据整理\n\n已按样品保留温度和得率字段。\n\n- **字段检查**：温度使用 °C。\n- **下步核对**：比较重复实验。'},
        'display_status': 'completed'} for i in range(1, 16)]
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
            payload = {'messages': [], 'has_more': False, 'turns': history}
        elif p.endswith('/turns'):
            payload = {'turns': [{'message_id': entry['user']['message_id'],
                'request_id': entry['turn_request_id'], 'text': entry['user']['content']} for entry in history]}
        elif p == '/api/v2/skills':
            payload = {'skills': [{'id': 'data-audit', 'version': 'frozen-fixture', 'sha256': SHA, 'description': '核对表格'},
                                  {**RESEARCH, 'description': '本轮 RAG 科研检索方法'}]}
        elif p.endswith('/turn-queue'):
            if route.request.method == 'POST':
                posted = route.request.post_data_json
                posts.append(posted)
                route.fulfill(status=202, json={'client_request_id': posted['client_request_id'], 'state': 'queued'})
                return
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
    page.locator('.message.bot h2', has_text='实验数据整理').last.wait_for()
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
    for control, icon in [('themeRailBtn', 'palette'), ('memoryRailBtn', 'sliders'), ('figureRailBtn', 'canvas'), ('send-btn', 'send')]:
        assert page.locator(f'#{control} use').get_attribute('href') == SPRITE + icon
    assert page.locator('#send-btn .ui-icon').evaluate('el => getComputedStyle(el).transform').startswith('matrix(0.707107, -0.707107')
    assert page.locator('.input-box').evaluate('(box) => parseFloat(getComputedStyle(box).borderRadius) >= 12')
    assert page.locator('.turn-tick').count() == len(history)
    assert page.locator('.turn-return-latest').is_hidden()
    assert page.locator('[data-theme-pick]').count() == 0
    assert page.locator('html').get_attribute('data-theme') == 'porcelain'
    assert page.evaluate('CitrusTheme.scale.get()') == 125
    assert page.evaluate('CitrusTheme.scale.effective()') == 1.25
    assert page.locator('body').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(245, 243, 239)'
    resizer = page.locator('#sidebarResizer')
    width = int(resizer.get_attribute('aria-valuenow'))
    assert width == 280
    page.locator('.session-files-close').click()
    assert abs(page.locator('.left-rail').bounding_box()['width'] - 60 * 1.25) < 1
    page.wait_for_function('document.querySelector("#appSidebar").offsetWidth === 280')
    resizer.focus()
    page.keyboard.press('ArrowRight')
    assert int(resizer.get_attribute('aria-valuenow')) == width + 16, resizer.get_attribute('aria-valuenow')
    page.keyboard.press('Home')
    assert int(resizer.get_attribute('aria-valuenow')) == width

    for scale in SCALES:
        page.locator('#themeRailBtn').click()
        page.locator(f'[data-ui-scale-pick="{scale}"]').click()
        assert page.locator(f'[data-ui-scale-pick="{scale}"]').get_attribute('aria-pressed') == 'true'
        assert page.evaluate('CitrusTheme.scale.effective()') == scale / 100
        assert abs(page.locator('body').bounding_box()['height'] - 900) < 1
        page.locator('#themeRailBtn').click()
        assert page.locator('#themeMenu').is_hidden()
        page.screenshot(path=output / f'porcelain-{scale}-workspace.png', animations='disabled')
        page.evaluate('snapshot => renderContextPanel(snapshot)', SNAPSHOT)
        collapsed = page.locator('#ctxCircleBtn')
        assert collapsed.inner_text() == '43%'
        assert page.locator('#ctxUsed').is_hidden()
        assert collapsed.locator('#ctxBtnFg').evaluate('el => getComputedStyle(el).stroke') == 'rgb(217, 119, 54)'
        collapsed.click()
        assert page.locator('#ctxUsed').text_content() == '4.3K / 10K'
        assert page.locator('#ctxBackdrop').is_hidden()
        page.locator('#ctxViewGrid').check()
        assert page.locator('#ctxGrid').is_visible() and not page.locator('#ctxBubble').is_visible()
        assert page.locator('#ctxGrid i').count() == 100
        assert page.locator('#ctxGrid i.is-used').count() == 43
        page.screenshot(path=output / f'porcelain-{scale}-context-grid.png', animations='disabled')
        page.locator('#ctxViewBubble').focus()
        page.keyboard.press('Space')
        assert page.locator('#ctxBubble').is_visible() and not page.locator('#ctxGrid').is_visible()
        assert abs(float(page.locator('#ctxBubbleFill').get_attribute('r')) - 54 * .43 ** .5) < .01
        page.screenshot(path=output / f'porcelain-{scale}-context-bubble.png', animations='disabled')
        page.keyboard.press('Escape')
        assert collapsed.get_attribute('aria-expanded') == 'false'
        assert collapsed.evaluate('el => el === document.activeElement')

    chat = page.locator('#chat-container')
    page.locator('.turn-tick').first.click()
    latest = page.locator('.turn-return-latest')
    latest.wait_for()
    page.wait_for_function('document.querySelector("#chat-container").scrollTop < 50')
    page.evaluate('followChatTail(false)')
    assert chat.evaluate('el => el.scrollTop') < 50
    latest.click()
    assert latest.is_hidden()
    assert chat.evaluate('el => el.scrollHeight - el.clientHeight - el.scrollTop <= 1')
    chat.hover()
    page.mouse.wheel(0, -300)
    latest.wait_for()
    latest.click()
    assert latest.is_hidden()
    page.locator('#sessionFilesRailBtn').click()
    page.locator('#ctxCircleBtn').click()
    page.wait_for_function('''() => {
      const rect = selector => document.querySelector(selector).getBoundingClientRect();
      const composer = rect('.input-box'), context = rect('#ctxDrawer'), library = rect('#sessionFilesPanel');
      return composer.right <= Math.min(context.left, library.left) && context.bottom <= library.top;
    }''')
    page.locator('#ctxClose').click()
    page.locator(f'[data-file-act="preview"][data-file-id="{TABLE_ID}"]').click()
    page.locator('.sf-table td', has_text='94.2').wait_for()
    page.screenshot(path=output / 'porcelain-file-preview.png', animations='disabled')
    page.locator('#sessionFilePreviewOverlay .overlay-close').click()
    page.locator('.session-files-close').click()
    attachments, scenes = page.locator('#input-tools-menu'), page.locator('#skill-picker-menu')
    attachments.locator('summary').click()
    assert [label.strip() for label in attachments.locator('button').all_text_contents()] == ['上传文件', '从资料库选择', '上传图片']
    assert not any(path == '/api/v2/skills' for path, _ in requests)
    assert '.pdf' in page.locator('#attachment-input').get_attribute('accept')
    assert 'image/' not in page.locator('#attachment-input').get_attribute('accept')
    assert page.locator('#image-attachment-input').get_attribute('accept') == 'image/jpeg,image/png,image/webp,image/gif'
    with page.expect_file_chooser() as opened:
        page.locator('#attachment-btn').click()
    opened.value.set_files({'name': 'fixture.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF-fixture'})
    assert page.locator('#attachment-list').get_by_text('fixture.pdf').is_visible()
    attachments.locator('summary').click()
    with page.expect_file_chooser() as opened:
        page.locator('#attachment-image-btn').click()
    opened.value.set_files({'name': 'fixture.webp', 'mimeType': 'image/webp', 'buffer': image_bytes})
    assert page.locator('#attachment-list img').is_visible()
    attachments.locator('summary').click()
    page.screenshot(path=output / 'porcelain-attachment-menu.png', animations='disabled')
    scenes.locator('summary').click()
    assert not attachments.evaluate('node => node.open')
    page.locator('[data-skill-id=data-audit]').check()
    page.screenshot(path=output / 'porcelain-scene-menu.png', animations='disabled')
    assert page.evaluate('skillPicker.capture().items') == [{'id': 'data-audit', 'version': 'frozen-fixture', 'sha256': SHA}]
    assert scenes.locator('#browse-requested').is_visible()
    page.keyboard.press('Escape')
    assert scenes.locator('summary').evaluate('node => node === document.activeElement && !node.parentNode.open')
    method = page.locator('#research-method')
    method.check()
    assert page.evaluate('skillPicker.capture().items')[-1] == RESEARCH
    assert not page.locator('#browse-requested').is_checked()
    method.uncheck()
    assert page.evaluate('skillPicker.capture().items') == [{'id': 'data-audit', 'version': 'frozen-fixture', 'sha256': SHA}]
    attachments.locator('summary').click()
    page.locator('#attachment-library-btn').click()
    page.locator(f'[data-file-id="{TABLE_ID}"].session-file-item [data-file-act="reference"]').click()
    assert page.locator('#attachment-list .library-reference-chip').count() == 1
    page.locator('.session-files-close').click()
    page.set_viewport_size({'width': 390, 'height': 800})
    page.context.new_cdp_session(page).send('Emulation.setTouchEmulationEnabled', {'enabled': True})
    page.reload(wait_until='domcontentloaded')
    page.locator('.message.bot h2', has_text='实验数据整理').last.wait_for()
    assert page.locator('html').get_attribute('data-theme') == 'porcelain'
    assert page.evaluate('CitrusTheme.scale.effective()') == 1
    assert page.locator('#appSidebar').is_hidden()
    page.locator('#sidebarToggleBtn').click()
    assert page.locator('.session-drawer-backdrop').is_visible()
    page.screenshot(path=output / 'porcelain-mobile-sessions.png', animations='disabled')
    page.keyboard.press('Escape')
    assert page.locator('#appSidebar').is_hidden()
    page.locator('#sessionFilesRailBtn').click()
    page.locator('.session-file-table-sample').wait_for()
    assert page.locator('.session-files-close').evaluate('el => el === document.activeElement')
    assert page.locator('[data-file-act=preview]').first.bounding_box()['height'] >= 44
    page.screenshot(path=output / 'porcelain-mobile-library.png', animations='disabled')
    page.keyboard.press('Escape')
    assert page.locator('#sessionFilesRailBtn').evaluate('el => el === document.activeElement')
    page.evaluate('snapshot => renderContextPanel(snapshot)', SNAPSHOT)
    page.locator('#ctxCircleBtn').click()
    page.locator('#ctxUsed').wait_for()
    assert page.locator('#ctxBackdrop').is_visible()
    geometry = page.locator('#ctxDrawer').bounding_box()
    assert geometry and geometry['x'] >= 0 and geometry['x'] + geometry['width'] <= 390
    page.screenshot(path=output / 'porcelain-mobile-context.png', animations='disabled')
    page.keyboard.press('Escape')
    attachments.locator('summary').click()
    geometry = page.locator('#attachment-options').bounding_box()
    assert 0 <= geometry['x'] < geometry['x'] + geometry['width'] <= 390
    page.screenshot(path=output / 'porcelain-mobile-attachments.png', animations='disabled')
    assert page.locator('#chat-container').evaluate('el => el.scrollWidth <= el.clientWidth')
    assert abs(page.locator('.message.user .avatar').last.bounding_box()['y'] - page.locator('.message.user .content').last.bounding_box()['y']) < 3
    page.keyboard.press('Escape')
    page.locator('.composer-research').click()
    page.wait_for_function('document.getElementById("research-method").checked')
    page.locator('#user-input').fill('验收真实场景信封')
    page.locator('#send-btn').click()
    page.wait_for_function('!document.getElementById("research-method").checked')
    assert len(posts) == 1 and posts[0]['selected_skills'] == [RESEARCH], posts
    assert 'browse_requested' not in posts[0]
    assert not errors, errors

    # A page opened earlier receives cross-tab scale changes without a reload.
    other = page.context.new_page()
    other.set_viewport_size({'width': 1360, 'height': 900})
    other.route('**/*', mock)
    other.goto(origin + '/assets/guide.html', wait_until='domcontentloaded')
    assert other.locator('html').get_attribute('data-theme') == 'porcelain'
    assert other.evaluate('CitrusTheme.scale.effective()') == 1.5
    page.evaluate('CitrusTheme.scale.set(125)')
    other.wait_for_function("document.documentElement.dataset.uiScale === '125'")
    for path in ['figures.html', 'figure-workbench.html', 'integrity-review.html', 'admin.html', 'assets/guide.html']:
        other.goto(origin + '/' + path, wait_until='domcontentloaded')
        assert other.locator('html').get_attribute('data-theme') == 'porcelain', path
        assert other.evaluate('CitrusTheme.scale.effective()') == 1.25, path
    (output / 'report.json').write_text(json.dumps({'scales': SCALES, 'narrow_width': 390,
        'context_cells': 100, 'used_cells': 43, 'turn_ticks': len(history), 'console_errors': errors}, indent=2))



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
                context = browser.new_context(viewport={'width': 1360, 'height': 900})
                page = context.new_page()
                page.set_default_timeout(6000)
                verify(page, args.output, origin, image_bytes)
                browser.close()
        finally:
            server.shutdown()
            thread.join()


if __name__ == '__main__':
    main()
