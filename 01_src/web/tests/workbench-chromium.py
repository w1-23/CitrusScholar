"""A-series browser smoke with the isolated FastAPI workbench fixture."""
from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def choose(page, label, *, confirm=True):
    page.get_by_role("button", name="打开图件", exact=True).click()
    asset = page.get_by_role("radio", name=label)
    asset.focus()
    page.keyboard.press("Space")
    expect(asset).to_be_checked()
    if confirm:
        page.get_by_role("button", name="打开所选图件").click()


def group(page, name):
    return page.get_by_role("treeitem", name=name, exact=True)


def selected(page, name):
    expect(group(page, name)).to_have_attribute("aria-selected", "true")


def apply(page):
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/preview"), timeout=30000) as response:
        page.get_by_role("button", name="应用到此组").click()
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
      const box = image.getBoundingClientRect(), [vx,vy,vw,vh] = geometry.viewBox;
      const available = viewport.getBoundingClientRect();
      const rows = project.hit_manifest.objects.filter(x => wanted.pick_ids.includes(x.gid) && x.bbox);
      for (const row of rows) {
        const {x0,x1,y0,y1} = row.bbox;
        for (let py=y0;py<=y1;py+=0.7) for (let px=x0;px<=x1;px+=0.7) {
          const hit = geometry.hits({x:px,y:py}, false)[0];
          if (!wanted.pick_ids.includes(hit)) continue;
          const x=box.left+(px-vx)*box.width/vw, y=box.top+(py-vy)*box.height/vh;
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
    assets = page.locator("#assetList")
    expect(assets.get_by_role("button", name="Synthetic overlap")).to_be_visible()
    page.get_by_label("类型", exact=True).select_option("svg")
    expect(assets.get_by_role("button", name="Static only")).to_be_visible()
    expect(assets.get_by_role("button", name="Synthetic overlap")).to_have_count(0)
    page.get_by_label("类型", exact=True).select_option("")
    page.get_by_label("编辑权限", exact=True).select_option("editable")
    page.get_by_label("按名称筛选", exact=True).fill("overlap")
    expect(assets.get_by_role("button", name="Synthetic overlap")).to_be_visible()
    expect(assets.get_by_role("button", name="Static only")).to_have_count(0)
    assets.get_by_role("button", name="Synthetic overlap").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.get_by_role("button", name="取消").click()
    choose(page, "Synthetic overlap", confirm=False)
    expect(page.get_by_role("radio", name="Foreign")).to_have_count(0)
    expect(page.get_by_role("radio", name="data.csv")).to_have_count(0)
    page.get_by_role("button", name="取消").click()
    expect(page.get_by_role("heading", name="未打开图件")).to_be_visible()
    assert not any(method == "POST" for method, _ in requests)
    choose(page, "Synthetic overlap")
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible(timeout=15000)
    expect(page.get_by_role("tree", name="对象组")).to_be_visible()
    expect(page.locator("#preview")).to_be_visible()
    detail = next(url for method, url in reversed(requests) if method == "GET" and "/projects/" in url and "/preview/" not in url)
    page.evaluate("url => {window.fixtureDetail = url}", detail)

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
    page.get_by_label("线型", exact=True).select_option("--")
    page.get_by_label("标记形状", exact=True).select_option("^")
    page.get_by_label("填充方式", exact=True).select_option("none")
    draft = apply(page)
    assert page.locator("#preview").get_attribute("src") != original
    svg = draft["svg"]
    assert svg.count('stroke="#7a3c96"') >= 2 and 'stroke-dasharray=' in svg
    page.get_by_role("button", name="撤销", exact=True).click()
    expect(page.locator("#status")).to_have_text("已恢复保存的图件。", timeout=20000)
    expect(page.get_by_label("线型", exact=True)).to_have_value("-")
    expect(page.get_by_label("填充方式", exact=True)).to_have_value("auto")
    page.get_by_role("button", name="重做", exact=True).click()
    expect(page.locator("#status")).to_have_text("草稿预览已更新。", timeout=20000)
    expect(page.get_by_label("填充方式", exact=True)).to_have_value("none")

    choose(page, "Unreadable asset")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#libraryNotice")).to_contain_text("无法")
    page.get_by_role("button", name="取消").click()
    expect(page.get_by_role("heading", name="Synthetic overlap")).to_be_visible()
    selected(page, "WL")
    expect(page.locator("#revision")).to_have_text("版本 1 · 草稿")
    page.get_by_role("button", name="保存图件", exact=True).click()
    expect(page.locator("#revision")).to_have_text("版本 2", timeout=30000)
    page.evaluate("() => {window.fixtureDetail = window.fixtureDetail.replace('revision=1', 'revision=2')}")

    page.get_by_role("button", name="画布选取").click()
    page.get_by_role("button", name="放大").click()
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
        assert artifact.startswith(prefix)
        assert received.value.headers["x-figure-sha256"] == sha256(artifact).hexdigest()
        assert received.value.headers["x-figure-revision"] == "2"
    page.locator("#exports summary").click()

    page.set_viewport_size({"width": 390, "height": 760})
    choose(page, "Separate bar groups")
    expect(page.get_by_role("heading", name="Separate bar groups")).to_be_visible(timeout=15000)
    group(page, "CK").focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="样式", exact=True).click()
    expect(page.get_by_label("填充颜色", exact=True)).to_be_visible()
    page.get_by_label("填充颜色", exact=True).fill("#7a3c96")
    apply(page)
    page.get_by_role("button", name="对象组", exact=True).click()
    group(page, "BL").focus()
    page.keyboard.press("Enter")
    page.get_by_role("button", name="样式", exact=True).click()
    assert page.get_by_label("填充颜色", exact=True).input_value() != "#7a3c96"
    page.screenshot(path=str(output / "workbench-narrow.png"))

    choose(page, "Static only")
    expect(page.get_by_role("heading", name="Static only")).to_be_visible(timeout=15000)
    page.locator("#exports summary").click()
    expect(page.get_by_role("button", name="SVG", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="PNG", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="PDF", exact=True)).to_be_disabled()
    assert not errors, errors
    context.close()
    print("Chromium A01/A02/A03/A04/A07 passed")


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--origin", required=True)
    args.add_argument("--output", required=True, type=Path)
    options = args.parse_args()
    assert urlparse(options.origin).hostname == "127.0.0.1"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        run(browser, options.origin, options.output)
        browser.close()
