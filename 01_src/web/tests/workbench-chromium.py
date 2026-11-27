"""A-series browser smoke with the isolated FastAPI workbench fixture."""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

from playwright.sync_api import expect, sync_playwright


def choose(page, label):
    page.get_by_role("button", name="打开图件", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name=label, exact=True).click()


def expand(page, name):
    branch = group(page, name)
    expect(branch).to_be_visible()
    if branch.get_attribute("aria-expanded") == "false":
        branch.locator(":scope > .tree-row > .tree-arrow").click()


def group(page, name):
    return page.get_by_role("treeitem", name=name, exact=True)


def selected(page, name):
    expect(group(page, name)).to_have_attribute("aria-selected", "true")


def apply(page, button="应用到此组"):
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/preview"), timeout=30000) as response:
        page.get_by_role("button", name=button).click()
    expect(page.locator("#status")).to_have_text("草稿预览已更新。", timeout=30000)
    return response.value.json()


def pick_geometry(page, target):
    # Read the actual rendered SVG primitives in the detached document. A
    # sampled interior point must be resolved through the page's click path.
    return page.evaluate("""async (target) => {
      const image = document.querySelector('#preview'), viewport = document.querySelector('#canvas');
      const xml = new DOMParser().parseFromString(await (await fetch(window.fixtureDetail.replace('?revision=', '/preview/svg?revision='))).text(), 'image/svg+xml');
      const project = await (await fetch(window.fixtureDetail)).json();
      const wanted = project.groups.find(g => g.label === target);
      const geometry = new window.FigureHitScene(xml, project.hit_manifest, true);
      let box = image.getBoundingClientRect();
      const [vx,vy,vw,vh] = geometry.viewBox;
      const available = viewport.getBoundingClientRect();
      const rows = project.hit_manifest.objects.filter(x => wanted.pick_ids.includes(x.gid) && x.bbox);
      for (const row of rows) {
        const {x0,x1,y0,y1} = row.bbox;
        const aimX = box.left + ((x0+x1)/2-vx)*box.width/vw;
        const aimY = box.top + ((y0+y1)/2-vy)*box.height/vh;
        viewport.scrollLeft += aimX - (available.left+available.right)/2;
        viewport.scrollTop += aimY - (available.top+available.bottom)/2;
        box = image.getBoundingClientRect();
        for (let py=y0;py<=y1;py+=0.7) for (let px=x0;px<=x1;px+=0.7) {
          const x=Math.round(box.left+(px-vx)*box.width/vw), y=Math.round(box.top+(py-vy)*box.height/vh);
          const hit = geometry.hits(geometry.point(x, y, box), false)[0];
          if (!wanted.pick_ids.includes(hit) || document.elementFromPoint(x, y) !== image) continue;
          if (x>available.left && x<available.right && y>available.top && y<available.bottom) return {x,y};
        }
      }
      throw new Error(`No visible geometry for ${target}`);
    }""", target)


def run(browser, origin, output):
    context = browser.new_context(viewport={"width": 1360, "height": 900}, accept_downloads=True)
    context.add_cookies([{"name": "citrus_auth", "value": "fixture", "url": origin}])
    page = context.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url)))
    page.goto(origin + "/figure-workbench")
    expect(page.get_by_role("heading", name="未打开图件")).to_be_visible()
    assert page.get_by_text("从资料库导入").count() == 0
    library_tab = page.get_by_role("tab", name="图件库")
    objects_tab = page.get_by_role("tab", name="对象", exact=True)
    assets = page.locator("#assetList")
    expect(library_tab).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("link", name="返回会话")).to_have_attribute("href", "/")
    library_tab.focus()
    page.keyboard.press("ArrowRight")
    expect(objects_tab).to_be_focused()
    expect(assets).not_to_be_visible()
    page.keyboard.press("ArrowLeft")
    expect(library_tab).to_be_focused()
    expect(assets.get_by_role("button", name="Synthetic overlap")).to_be_visible()
    assert page.get_by_label("类型", exact=True).count() == 0
    assert page.get_by_label("编辑权限", exact=True).count() == 0
    page.get_by_label("按名称筛选", exact=True).fill("overlap")
    expect(assets.get_by_role("button", name="Synthetic overlap")).to_be_visible()
    expect(assets.get_by_role("button", name="Static only")).to_have_count(0)
    assert all("kind=" not in url and "access=" not in url for _, url in requests)
    page.get_by_label("按名称筛选", exact=True).clear()
    expect(assets.get_by_role("button", name="Static only")).to_be_visible()
    page.get_by_role("button", name="打开图件", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_visible()
    assert page.get_by_role("dialog").get_by_role("button", name="Foreign").count() == 0
    assert page.get_by_role("dialog").get_by_role("button", name="data.csv").count() == 0
    page.get_by_role("button", name="关闭图件库").click()
    expect(page.get_by_role("heading", name="未打开图件")).to_be_visible()
    assert not any(method == "POST" for method, _ in requests)
    assets.get_by_role("button", name="Synthetic overlap").click()
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible(timeout=15000)
    expect(page.get_by_role("dialog")).not_to_be_visible()
    expect(page.get_by_role("tree", name="对象组")).to_be_visible()
    expect(objects_tab).to_have_attribute("aria-selected", "true")
    expect(assets).not_to_be_visible()
    expect(page.locator("#preview")).to_be_visible()
    detail = next(url for method, url in reversed(requests) if method == "GET" and "/projects/" in url and "/preview/" not in url)
    page.evaluate("url => {window.fixtureDetail = url}", detail)

    first_url = page.url
    panel = group(page, "子图 1")
    expect(panel).to_have_attribute("aria-expanded", "false")
    expect(group(page, "WL")).not_to_be_visible()
    expand(page, "子图 1")
    expect(group(page, "数据系列")).to_have_attribute("aria-expanded", "false")
    group(page, "数据系列").locator(":scope > .tree-row > .tree-name").click()
    expect(page.get_by_text("批量属性 · 3 个对象")).to_be_visible()
    page.get_by_role("radio", name="菱形", exact=True).press("Space")
    batch = apply(page, "应用到类别")
    assert {item["label"] for item in batch["groups"] if item["kind"] == "series" and item["values"].get("marker") == "D"} == {"CK", "BL", "WL"}
    page.get_by_role("button", name="撤销", exact=True).click()
    expect(page.locator("#status")).to_have_text("已恢复保存的图件。", timeout=20000)
    expand(page, "数据系列")
    page.get_by_role("button", name="画布选取").click()
    page.locator('[role=treeitem][tabindex="0"]').focus()
    for _ in range(60):
        if group(page, "WL").evaluate("node => node === document.activeElement"):
            break
        page.keyboard.press("ArrowDown")
    expect(group(page, "WL")).to_be_focused()
    page.keyboard.press("Enter")
    selected(page, "WL")
    expect(page.get_by_role("heading", name="WL", exact=True)).to_be_visible()
    original = page.locator("#preview").get_attribute("src")
    page.get_by_label("系列颜色", exact=True).fill("#7a3c96")
    page.get_by_role("radio", name="虚线", exact=True).press("Space")
    page.get_by_role("radio", name="向上三角", exact=True).press("Space")
    page.get_by_label("填充方式", exact=True).select_option("none")
    draft = apply(page)
    assert page.locator("#preview").get_attribute("src") != original
    svg = draft["svg"]
    assert svg.count('stroke="#7a3c96"') >= 2 and 'stroke-dasharray=' in svg
    page.get_by_role("button", name="撤销", exact=True).click()
    expect(page.locator("#status")).to_have_text("已恢复保存的图件。", timeout=20000)
    expect(page.get_by_role("radio", name="实线", exact=True)).to_be_checked()
    expect(page.get_by_label("填充方式", exact=True)).to_have_value("auto")
    page.get_by_role("button", name="重做", exact=True).click()
    expect(page.locator("#status")).to_have_text("草稿预览已更新。", timeout=20000)
    expect(page.get_by_label("填充方式", exact=True)).to_have_value("none")

    page.once("dialog", lambda dialog: dialog.accept())
    choose(page, "Unreadable asset")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#libraryNotice")).to_contain_text("无法")
    page.get_by_role("button", name="关闭图件库").click()
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible()
    selected(page, "WL")
    expect(page.locator("#revision")).to_have_text("版本 1 · 草稿")
    page.get_by_role("button", name="保存图件", exact=True).click()
    expect(page.locator("#revision")).to_have_text("版本 2", timeout=30000)
    page.evaluate("() => {window.fixtureDetail = window.fixtureDetail.replace('revision=1', 'revision=2')}")

    page.get_by_role("button", name="画布选取").click()
    zoom = page.get_by_label("自定义画布缩放百分比")
    zoom.fill("201")
    page.get_by_role("button", name="应用画布缩放").click()
    expect(page.get_by_role("button", name="适合窗口")).to_have_attribute("aria-pressed", "true")
    zoom.fill("150")
    page.get_by_role("button", name="应用画布缩放").click()
    expect(page.locator("#zoomLevel")).to_have_text("150%")
    expect(page.locator('[data-ui-scale-pick]')).to_have_count(0)
    page.evaluate("window.CitrusTheme.scale.set(150)")
    expect(page.locator("html")).to_have_attribute("data-ui-scale", "150")
    expect(page.locator("html")).to_have_css("zoom", "1")
    expect(page.locator("#zoomLevel")).to_have_text("150%")
    expect(page.get_by_role("button", name="画布选取")).to_have_attribute("aria-pressed", "true")
    point = pick_geometry(page, "X 轴刻度文字")
    page.mouse.click(**point)
    selected(page, "X 轴刻度文字")
    point = pick_geometry(page, "WL")
    page.mouse.click(**point)
    selected(page, "WL")
    page.get_by_role("button", name="适合窗口").click()
    point = pick_geometry(page, "标题")
    page.mouse.move(**point)
    page.mouse.down()
    page.mouse.move(point["x"] + 16, point["y"] + 10, steps=6)
    page.mouse.up()
    expect(page.locator("#status")).to_have_text("草稿预览已更新。", timeout=20000)
    selected(page, "标题")
    assert float(page.get_by_label("水平", exact=True).input_value()) > 0
    page.get_by_role("button", name="撤销", exact=True).click()
    expect(page.locator("#status")).to_have_text("已恢复保存的图件。", timeout=20000)

    # A space inside the diagonal bounding box but away from its line keeps
    # the previous selection. Pixel coordinates are computed from SVG bounds.
    blank = page.evaluate("""async () => {
      const image = document.querySelector('#preview'), box = image.getBoundingClientRect();
      const p = await (await fetch(window.fixtureDetail)).json();
      const r = p.hit_manifest.objects.find(r => r.label === 'WL' && r.role === 'series').bbox;
      return {x:box.left+(r.x1*.9+r.x0*.1)*box.width/p.figure_size[0],
              y:box.top+(r.y1*.9+r.y0*.1)*box.height/p.figure_size[1]};
    }""")
    group(page, "CK").focus()
    page.keyboard.press("Enter")
    page.mouse.click(**blank)
    selected(page, "CK")

    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / "workbench-desktop.png"))
    page.locator("#exports summary").click()
    for format, caption, prefix in [("svg", "SVG", b"<svg"), ("png", "生成 PNG", b"\x89PNG"), ("pdf", "生成 PDF", b"%PDF-")]:
        endpoint = f"/export/{format}?revision=2"
        with page.expect_response(lambda response: endpoint in response.url) as received, page.expect_download(timeout=35000) as download:
            page.get_by_role("button", name=caption, exact=True).click()
        artifact = download.value.path().read_bytes()
        if format == "svg":
            assert ET.fromstring(artifact).tag == "{http://www.w3.org/2000/svg}svg"
        else:
            assert artifact.startswith(prefix)
        assert received.value.headers["x-figure-sha256"] == sha256(artifact).hexdigest()
        assert received.value.headers["x-figure-revision"] == "2"
    page.locator("#exports summary").click()

    # A pinned URL opens directly, and a same-library drag uses the identical loader.
    page.goto(first_url.split("&revision=")[0] + "&revision=2")
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible(timeout=15000)
    expect(page.get_by_role("dialog")).not_to_be_visible()
    page.locator("#canvas").evaluate("""canvas => {
      const transfer = new DataTransfer();
      transfer.setData('text/html', '<svg onload=alert(1)>');
      const drop = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: transfer });
      if (canvas.dispatchEvent(drop)) throw new Error('Untrusted drop default was not cancelled');
    }""")
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible()
    library_tab.click()
    assets.get_by_role("button", name="Five independent groups").drag_to(page.locator("#canvas"))
    expect(page.get_by_role("heading", name="Five independent groups")).to_be_visible(timeout=15000)
    expect(group(page, "子图 1")).to_have_attribute("aria-expanded", "false")
    expand(page, "子图 1")
    expand(page, "数据系列")
    for name in ["CK", "WL", "BL", "Control A", "Control B"]:
        group(page, name).locator(":scope > .tree-row > .tree-name").click()
        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
        expect(page.get_by_label("系列颜色", exact=True)).to_be_visible()

    expect(page.locator("html")).to_have_attribute("data-theme", "porcelain")
    page.reload()
    expect(page.get_by_role("heading", name="Five independent groups")).to_be_visible(timeout=15000)
    expect(page.locator("html")).to_have_attribute("data-ui-scale", "150")
    expect(page.get_by_role("button", name="适合窗口")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("html")).to_have_css("zoom", "1")
    assert page.viewport_size["width"] == 1360
    expect(zoom).to_have_value("")
    expect(zoom).to_have_attribute("placeholder", "缩放 %")
    expect(zoom).to_have_accessible_description("输入 50 至 200 之间的整数。")
    zoom_layout = """input => {
      const panel = input.closest('.canvas-toolbar').getBoundingClientRect();
      const controls = [...input.closest('.view-controls').querySelectorAll('button, input, output')];
      const boxes = controls.map(node => node.getBoundingClientRect());
      const overlap = boxes.flatMap((box, index) => boxes.slice(index + 1).flatMap((other, offset) =>
        Math.min(box.right, other.right) - Math.max(box.left, other.left) > .5 &&
        Math.min(box.bottom, other.bottom) - Math.max(box.top, other.top) > .5 ?
          [[controls[index].id, controls[index + 1 + offset].id]] : []));
      const outside = boxes.flatMap((box, index) =>
        box.left < panel.left - .5 || box.right > panel.right + .5 ? [controls[index].id] : []);
      const style = getComputedStyle(input);
      const ctx = document.createElement('canvas').getContext('2d');
      ctx.font = `${style.fontSize} ${style.fontFamily}`;
      const room = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      const text = ctx.measureText(input.value || input.placeholder).width;
      return {appearance: style.appearance, text, room, overlap, outside};
    }"""
    def check_zoom_layout():
        metrics = zoom.evaluate(zoom_layout)
        assert metrics["appearance"] == "textfield" and metrics["text"] + 2 < metrics["room"], metrics
        assert not metrics["overlap"] and not metrics["outside"], metrics

    check_zoom_layout()
    page.screenshot(path=str(output / "workbench-canvas-fit.png"))
    zoom.fill("200")
    page.get_by_role("button", name="应用画布缩放").click()
    expect(zoom).to_have_value("200")
    expect(page.locator("#zoomLevel")).to_have_text("200%")
    check_zoom_layout()
    page.screenshot(path=str(output / "workbench-canvas-200.png"))

    choose(page, "Native two-panel figure")
    expect(page.get_by_role("heading", name="Native two-panel figure")).to_be_visible(timeout=15000)
    expect(page.get_by_role("button", name="画布选取")).to_have_attribute("aria-pressed", "true")
    detail = next(url for method, url in reversed(requests) if method == "GET" and "/projects/" in url and "/preview/" not in url)
    page.evaluate("url => {window.fixtureDetail = url}", detail)
    baseline = page.request.get(detail.replace("?revision=", "/preview/svg?revision=")).body()
    for label, control in [("Points B", "系列颜色"), ("Trend A", "系列颜色")]:
        page.mouse.click(**pick_geometry(page, label))
        selected(page, label)
        expect(page.get_by_label(control, exact=True)).to_be_visible()
    page.get_by_label("系列颜色", exact=True).fill("#7a3c96")
    native_draft = apply(page)
    assert 'stroke="#7a3c96"' in native_draft["svg"]
    assert page.request.get(detail.replace("?revision=", "/preview/svg?revision=")).body() == baseline
    page.get_by_role("button", name="保存图件", exact=True).click()
    expect(page.locator("#revision")).to_have_text("版本 2", timeout=45000)
    selected(page, "Trend A")
    expect(page.get_by_label("系列颜色", exact=True)).to_have_value("#7a3c96")

    page.set_viewport_size({"width": 390, "height": 760})
    choose(page, "Separate bar groups")
    expect(page.get_by_role("heading", name="Separate bar groups")).to_be_visible(timeout=15000)
    fit = page.get_by_role("button", name="适合窗口")
    actual = page.get_by_role("button", name="实际大小 100%")
    assert page.locator("#preview").evaluate("img => img.getBoundingClientRect().width < img.naturalWidth")
    actual.click()
    expect(actual).to_have_attribute("aria-pressed", "true")
    assert page.locator("#preview").evaluate("img => img.getBoundingClientRect().width === img.naturalWidth")
    fit.click()
    expect(fit).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="对象组", exact=True).click()
    expand(page, "子图 1")
    expand(page, "数据系列")
    group(page, "CK").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_label("填充颜色", exact=True)).to_be_visible()
    page.get_by_label("填充颜色", exact=True).fill("#7a3c96")
    apply(page)
    page.get_by_role("button", name="对象组", exact=True).click()
    group(page, "BL").focus()
    page.keyboard.press("Enter")
    assert page.get_by_label("填充颜色", exact=True).input_value() != "#7a3c96"
    page.screenshot(path=str(output / "workbench-narrow.png"))

    page.once("dialog", lambda dialog: dialog.accept())
    choose(page, "Static only")
    expect(page.get_by_role("heading", name="Static only")).to_be_visible(timeout=15000)
    page.locator("#exports summary").click()
    expect(page.get_by_role("button", name="SVG", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="PNG", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="PDF", exact=True)).to_be_disabled()
    project_id = parse_qs(urlparse(page.url).query)["project"][0]
    example_legacy = f"/figures/legacy?project={project_id}"
    current_editor_url = example_legacy

    def legacy_fixture(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "editor_url": current_editor_url})

    page.route(f"**/api/v2/figure-workbench/projects/{project_id}?revision=1", legacy_fixture)
    page.reload()
    expect(page.get_by_role("link", name="旧版 SVG 编辑器")).to_have_attribute("href", example_legacy)
    current_editor_url = f"/figures/legacy?project={'e' * 32}"
    page.reload()
    expect(page.get_by_role("heading", name="Static only")).to_be_visible(timeout=15000)
    expect(page.get_by_role("link", name="旧版 SVG 编辑器")).not_to_be_visible()
    assert not errors, errors
    context.close()
    print("Chromium A01/A02/A03/A04/A07 passed")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--origin", required=True)
    args.add_argument("--chromium")
    args.add_argument("--output", required=True, type=Path)
    options = args.parse_args()
    assert urlparse(options.origin).hostname == "127.0.0.1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=options.chromium, args=["--no-sandbox"])
        run(browser, options.origin, options.output)
        browser.close()
