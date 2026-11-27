"""Isolated acceptance for the real gallery DOM with pinned synthetic files."""
from __future__ import annotations

import argparse
import base64
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import struct
from pathlib import Path
import re
from threading import Thread
from urllib.parse import urlparse
import zlib

from playwright.sync_api import expect, sync_playwright


def digest(body):
    return hashlib.sha256(body).hexdigest()


def png(width, height):
    def chunk(tag, body):
        return struct.pack('>I', len(body)) + tag + body + struct.pack('>I', zlib.crc32(tag + body))
    image = width * bytes((40, 100, 69))
    bits = (b'\x00' + image) * height
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bits)) + chunk(b'IEND', b''))


def pdf():
    stream = b'0.10 0.45 0.25 rg 15 20 140 75 re f\n'
    body = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate((b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 120] /Contents 4 0 R >>',
                             b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'endstream'), 1):
        offsets.append(len(body))
        body += b'%d 0 obj\n' % i + obj + b'\nendobj\n'
    xref = len(body)
    return (body + b'xref\n0 5\n0000000000 65535 f \n' + b''.join(b'%010d 00000 n \n' % n for n in offsets[1:])
            + b'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % xref)


def run(browser, output):
    context = browser.new_context(viewport={'width': 1360, 'height': 900}, accept_downloads=True)
    page = context.new_page()
    page_errors, api_posts = [], []
    page.on('pageerror', lambda err: page_errors.append(str(err)))
    sid = 'gallery-fixture-session'
    svg_id = 'a' * 32
    source_turn = 'gallery-svg-turn'
    data = {
        'a' * 32: ('Curves.svg', 'svg', 'image/svg+xml', b'<svg xmlns="http://www.w3.org/2000/svg" width="580" height="360"><rect width="580" height="360" fill="#286445"/><circle cx="290" cy="180" r="75" fill="#7a3c96"/></svg>'),
        'b' * 32: ('Samples.png', 'image', 'image/png', png(580, 360)),
        'c' * 32: ('Report.pdf', 'pdf', 'application/pdf', pdf()),
        'd' * 32: ('Measurements.xlsx', 'table', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', b''),
    }

    web = Path(__file__).resolve().parents[1]

    class FileHost(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=web, **kwargs)

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith('/api/v2/files/') and path.endswith('/download'):
                fid = path.split('/')[-2]
                if fid not in data:
                    self.send_error(404)
                    return
                name, _, mime, body = data[fid]
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Disposition', f'attachment; filename={name}')
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), FileHost)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f'http://127.0.0.1:{server.server_port}'

    def serve(route):
        request = route.request
        url = urlparse(request.url)
        path = url.path
        if url.hostname != '127.0.0.1':
            route.abort()
        elif path == '/api/v2/auth/me':
            route.fulfill(json={'user': {'user_id': 17, 'username': 'Gallery Fixture', 'role': 'user'}})
        elif path == '/api/v2/files':
            files = [{'file_id': fid, 'filename': name, 'kind': kind, 'mime_type': mime,
                      'origin': 'generated' if fid == svg_id else 'uploaded',
                      **({'source_session_id': sid, 'turn_request_id': source_turn} if fid == svg_id else {}),
                      'size_bytes': len(body), 'sha256': digest(body),
                      'download_url': f'/api/v2/files/{fid}/download', 'preview_url': f'/api/v2/files/{fid}/preview'}
                     for fid, (name, kind, mime, body) in data.items()]
            route.fulfill(json={kind: [file for file in files if file['origin'] == kind]
                                for kind in ('uploaded', 'generated')})
        elif path.startswith('/api/v2/files/') and path.endswith('/download'):
            route.continue_()
        elif path.startswith('/api/v2/files/'):
            _, _, _, _, file_id, action = path.split('/')
            name, _, mime, body = data[file_id]
            if action == 'table-sample':
                route.fulfill(json={'rows': [['Series', 'Value'], ['CK', '42']]})
            else:
                route.fulfill(body=body, content_type=mime)
        elif path == '/api/v1/session/new':
            route.fulfill(json={'session_id': sid})
        elif path.startswith(('/api/', '/jobs/')):
            if request.method == 'POST':
                api_posts.append(path)
            route.fulfill(json={'sessions': [{'session_id': sid, 'title': 'Fixture'}], 'turns': [], 'messages': []})
        else:
            route.continue_()

    page.route('**/*', serve)
    page.goto(origin + '/?files=1')
    panel = page.locator('#sessionFilesPanel')
    expect(panel).to_be_visible()
    workbook = page.evaluate('''() => {
      const book = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([['Series','Value'],['CK',42]]), 'Raw');
      XLSX.utils.book_append_sheet(book, XLSX.utils.aoa_to_sheet([['Series','Value'],['WL',84]]), 'Second');
      return XLSX.write(book, {bookType:'xlsx', type:'base64'});
    }''')
    workbook_id = 'd' * 32
    data[workbook_id] = (*data[workbook_id][:3], base64.b64decode(workbook))
    page.evaluate('window.loadSessionFiles()')
    expect(panel.locator('.session-file-item')).to_have_count(4)
    expect(panel.locator('.session-file-thumb img.is-ready')).to_have_count(2)
    assert panel.evaluate('''panel => {
      const rect = node => node.getBoundingClientRect();
      const filters = panel.querySelector('.library-filters');
      const body = panel.querySelector('#sessionFilesBody');
      const card = panel.querySelector('.session-file-item');
      return filters.parentElement === panel && rect(filters).bottom <= rect(body).top + 1 &&
        rect(card.querySelector('.session-file-thumb')).bottom <= rect(card.querySelector('.session-file-name')).top &&
        rect(card.querySelector('.session-file-actions')).top > rect(card.querySelector('.session-file-name')).bottom;
    }''')
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / 'gallery-desktop.png'))
    experience = page.locator(f'.session-file-item[data-file-id="{svg_id}"]')
    action_names = lambda row: row.locator('[data-file-act]').evaluate_all('(items) => items.map(i => i.dataset.fileAct)')
    assert action_names(experience) == ['preview', 'download', 'reference', 'jump']
    assert action_names(panel.locator('.session-file-item', has_text='Samples.png')) == ['preview', 'download', 'reference']
    assert experience.get_by_role('button', name='下载原件 Curves.svg').inner_text() in {'下载原件', '原件'}
    with page.expect_download() as direct_original:
        experience.get_by_role('button', name='下载原件 Curves.svg').click()
    assert digest(direct_original.value.path().read_bytes()) == digest(data[svg_id][3])
    page.evaluate("""() => {
        window.fixtureJumps = [];
        window.jumpToFileTurn = (...pair) => { window.fixtureJumps.push(pair); return true; };
    }""")
    experience.get_by_role('button', name='定位到来源会话 Curves.svg').click()
    page.wait_for_function('fixtureJumps.length > 0', timeout=3000)
    assert page.evaluate('fixtureJumps.length') == 1
    assert page.evaluate('fixtureJumps[0]') == [sid, source_turn]

    overlay = page.locator('#sessionFilePreviewOverlay')
    for fid, (name, kind, mime, source) in data.items():
        panel.get_by_role('button', name=f'预览 {name}').click()
        expect(overlay).to_be_visible()
        if kind in {'image', 'svg'}:
            image = overlay.locator('.sf-preview-image')
            expect(image).to_be_visible()
            expect(overlay.get_by_role('button', name='原始大小 100%')).to_be_enabled()
            assert image.get_attribute('src') == f'/api/v2/files/{fid}/preview'
            assert image.evaluate("node => { const c = document.createElement('canvas'); c.width=c.height=1; const ctx=c.getContext('2d'); ctx.drawImage(node,0,0); return [...ctx.getImageData(0,0,1,1).data].join(','); }") == '40,100,69,255'
            overlay.get_by_role('button', name='原始大小 100%').click()
            assert image.evaluate('node => node.offsetWidth === node.naturalWidth && node.offsetHeight === node.naturalHeight')
            overlay.get_by_role('button', name='放大图片').click()
            assert image.evaluate('node => node.offsetWidth > node.naturalWidth && node.offsetHeight > node.naturalHeight')
            overlay.get_by_role('button', name='缩小图片').click()
            overlay.get_by_role('button', name='适应窗口').click()
            expect(overlay.get_by_role('button', name='适应窗口')).to_have_attribute('aria-pressed', 'true')
            assert overlay.locator('iframe,object,svg').count() == 0
            expect(overlay.get_by_role('button', name=re.compile('可编辑副本|SVG 源码|导出|文件信息'))).to_have_count(0)
        elif kind == 'pdf':
            if page.evaluate('navigator.pdfViewerEnabled') is False:
                expect(overlay.locator('.sf-pdf-fallback')).to_contain_text('没有内嵌 PDF 阅读器')
                print('PDF checked bytes and truthful fallback (headless Chromium has no PDF renderer)')
            else:
                expect(overlay.locator('.sf-pdf-frame')).to_have_attribute('src', re.compile('^blob:'))
                expect(overlay.locator('.sf-pdf-frame')).to_be_visible()
                print('PDF checked bytes and native viewer readiness')
        else:
            expect(overlay.get_by_text('CK', exact=True)).to_be_visible()
            overlay.get_by_role('button', name='Second').click()
            expect(overlay.get_by_text('84', exact=True)).to_be_visible()
        with page.expect_download() as download:
            overlay.get_by_role('link', name='下载原件', exact=True).first.click()
        assert digest(download.value.path().read_bytes()) == digest(source), name
        page.screenshot(path=str(output / f'preview-{kind}.png'))
        page.keyboard.press('Escape')
        expect(overlay).not_to_be_visible()
        expect(panel.get_by_role('button', name=f'预览 {name}')).to_be_focused()

    page.evaluate("""({fid, sid, turn}) => {
        toggleSessionFiles(false);
        const content = document.createElement('div');
        content.dataset.sessionId = sid;
        content.dataset.requestId = turn;
        document.getElementById('chat-container').appendChild(content);
        appendTurnGeneratedFiles(content, [{file_id: fid, filename: 'Curves.svg', kind: 'svg',
          origin: 'generated', source_session_id: sid, turn_request_id: turn,
          download_url: `/api/v2/files/${fid}/download`}]);
    }""", {'fid': svg_id, 'sid': sid, 'turn': source_turn})
    chat_file = page.locator('.turn-file-card')
    expect(chat_file.locator('.file-primary-action')).to_have_count(3)
    expect(chat_file.get_by_role('button', name='预览 Curves.svg')).to_be_visible()
    expect(chat_file.get_by_role('button', name='引用 Curves.svg')).to_be_visible()
    expect(chat_file.get_by_role('link', name='下载原件 Curves.svg')).to_have_attribute('href', f'/api/v2/files/{svg_id}/download')
    assert action_names(chat_file) == ['reference']
    chat_file.get_by_role('button', name='引用 Curves.svg').click()
    assert page.evaluate('window._selectedAccountFileRefs.map(x => x.file_id)') == [svg_id]
    assert page.evaluate('fixtureJumps.length') == 1
    page.evaluate('toggleSessionFiles(true)')
    page.set_viewport_size({'width': 390, 'height': 760})
    panel.get_by_role('button', name='预览 Samples.png').click()
    expect(overlay.locator('img')).to_be_visible()
    assert overlay.locator('.session-file-preview-card').evaluate('node => node.getBoundingClientRect().right <= innerWidth')
    page.screenshot(path=str(output / 'preview-narrow.png'))
    assert not api_posts and not page_errors, (api_posts, page_errors)
    context.close()
    server.shutdown()
    server.server_close()
    print('Chromium library/chat file actions and SVG, PNG, PDF, XLSX unmodified download fixtures passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--chromium')
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.chromium, args=['--no-sandbox'])
        run(browser, args.output)
        browser.close()
