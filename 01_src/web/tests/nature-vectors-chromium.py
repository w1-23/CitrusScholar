"""Browser acceptance for three real Nature plotting artifacts via a fixture app."""
from __future__ import annotations

import argparse
from base64 import b64encode
from hashlib import sha256
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


def image_delta(page, expected, seen):
    return page.evaluate('''async ([expected, seen]) => {
      async function read(base) {
        const image = new Image();
        const data = Uint8Array.from(atob(base), (letter) => letter.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([data], {type: 'image/svg+xml'}));
        try {
          image.src = url;
          await image.decode();
          const canvas = document.createElement('canvas');
          canvas.width = image.naturalWidth * 2;
          canvas.height = image.naturalHeight * 2;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
          return [canvas.width, canvas.height, ctx.getImageData(0, 0, canvas.width, canvas.height).data];
        } finally { URL.revokeObjectURL(url); }
      }
      const [w, h, a] = await read(expected);
      const [u, v, b] = await read(seen);
      if (w !== u || h !== v) throw new Error(`Image dimensions differ: ${w}x${h} ${u}x${v}`);
      let pixels = 0, maxDifference = 0;
      for (let i = 0; i < a.length; i += 4) {
        const max = Math.max(...[0,1,2,3].map(n => Math.abs(a[i+n]-b[i+n])));
        if (max > 0) pixels++;
        if (max > maxDifference) maxDifference = max;
      }
      return {w, h, pixels, maxDifference};
    }''', [b64encode(blob).decode() for blob in (expected, seen)])


def run(browser, origin, fixtures):
    context = browser.new_context(viewport={"width": 1320, "height": 900}, accept_downloads=True)
    context.add_cookies([{"name": "citrus_auth", "value": "fixture", "url": origin}])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for item in fixtures:
        if "uploaded" in item:
            response = context.request.post(origin + "/api/v2/figure-workbench/import-svg", data={"file_id": item["uploaded"]})
            assert response.ok, response.text()
            project = response.json()
            assert project["origin"] == "uploaded" and project["vector_dialect"] == "svglite"
            assert "backend" not in project
            title = Path(item["filename"]).stem
        else:
            project = item
            name = next(name for name in project["original_exports"] if name.endswith(".svg"))
            title = f"Actual {project['backend']} {name}"
        origin_path = f"{origin}/api/v2/figure-workbench/projects/{project['figure_id']}"
        base_url = origin + project["workspace_url"]
        primary = next(name for name in project["original_exports"] if name.endswith(".svg"))
        page.goto(base_url)
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(page.locator("#preview")).to_be_visible()
        before = context.request.get(origin + project["preview_url"]).body()
        original = context.request.get(origin + project["original_exports"][primary]["download_url"]).body()
        delta = image_delta(page, original, before)
        print(f"{primary} original-versus-derivative pixels {json.dumps(delta)}", flush=True)
        assert delta["pixels"] == 0, (primary, delta)
        detail = context.request.get(origin_path + "?revision=1").json()
        native = next(group for group in detail["groups"] if group["native_tag"] == "text")
        vectors = page.get_by_role("treeitem", name="原生矢量对象", exact=True)
        expect(vectors).to_have_attribute("aria-expanded", "false")
        vectors.locator(":scope > .tree-row > .tree-arrow").click()
        page.get_by_role("treeitem", name=native["label"], exact=True).click()
        page.get_by_label("颜色", exact=True).fill("#a11566")
        page.get_by_role("button", name="应用到此组").click()
        expect(page.locator("#status")).to_have_text("草稿预览已更新。")
        page.get_by_role("button", name="保存图件", exact=True).click()
        expect(page.locator("#revision")).to_have_text("版本 2")
        page.reload()
        expect(page.locator("#revision")).to_have_text("版本 2")
        vectors = page.get_by_role("treeitem", name="原生矢量对象", exact=True)
        expect(vectors).to_have_attribute("aria-expanded", "false")
        vectors.locator(":scope > .tree-row > .tree-arrow").click()
        page.get_by_role("treeitem", name=native["label"], exact=True).click()
        expect(page.get_by_label("颜色", exact=True)).to_have_value("#a11566")
        changed = context.request.get(origin_path + "/preview/svg?revision=2")
        assert changed.ok and b'fill="#a11566"' in changed.body()
        assert changed.headers["x-figure-sha256"] == sha256(changed.body()).hexdigest()
        page.locator("#exports summary").click()
        for name, entry in project["original_exports"].items():
            with page.expect_download() as downloaded:
                page.get_by_role("link", name=name, exact=True).click()
            assert sha256(downloaded.value.path().read_bytes()).hexdigest() == entry["sha256"]
        print(f"{project['origin']} {primary} edited, saved revision 2, reopened, downloaded originals", flush=True)
    assert not errors, errors
    context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--chromium")
    options = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=options.chromium, args=["--no-sandbox"])
        run(browser, options.origin, json.loads(options.fixtures.read_text()))
        browser.close()
