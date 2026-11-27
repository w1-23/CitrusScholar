"""Preview/export static PNG/JPEG/SVG/PDF inside the isolated worker."""
import io
import json
import sys
from pathlib import Path

import fitz
from defusedxml.ElementTree import fromstring
from PIL import Image, ImageOps

PIXEL_MAX = 16_000_000
Image.MAX_IMAGE_PIXELS = PIXEL_MAX


def to_pdf(body):
    if body.startswith(b"%PDF-"):
        return "pdf", fitz.open(stream=body, filetype="pdf")
    if body.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        with Image.open(io.BytesIO(body), formats=["PNG", "JPEG"]) as original:
            if original.width * original.height > PIXEL_MAX or original.is_animated:
                raise ValueError("Image exceeds static asset limits")
            image = ImageOps.exif_transpose(original).convert("RGBA")
            kind = original.format.lower()
        with io.BytesIO() as data:
            image.save(data, format="PNG")
            document = fitz.open()
            page = document.new_page(width=image.width * 0.6, height=image.height * 0.6)
            page.insert_image(page.rect, stream=data.getvalue())
        return kind, document
    if fromstring(body).tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
        raise ValueError("Unrecognized SVG root")
    with fitz.open(stream=body, filetype="svg") as svg:
        return "svg", fitz.open(stream=svg.convert_to_pdf(), filetype="pdf")


def physical_size(rect, config):
    w, h = rect.width, rect.height
    if w <= 0 or h <= 0:
        raise ValueError("Invalid figure size")
    width, height = config.get("width_mm"), config.get("height_mm")
    if width is not None or height is not None:
        width_pt = width * 72 / 25.4 if width is not None else (height * 72 / 25.4) * w / h
        height_pt = height * 72 / 25.4 if height is not None else width_pt * h / w
        w, h = width_pt, height_pt
    fit = min(1, 1120 / max(w, h)) if width is None and height is None else 1
    if w * fit < 0.5 or h * fit < 0.5 or max(w, h) * fit > 1200:
        raise ValueError("Output dimensions exceed limits")
    return w * fit, h * fit


def main(source_name, request_name, output_dir):
    request = json.loads(Path(request_name).read_text(encoding="utf-8"))
    directory = Path(output_dir)
    fmt = request["format"]
    out = directory / ("figure." + fmt)
    kind, source = to_pdf(Path(source_name).read_bytes())
    limitations = ["静态图件不会恢复原始数据或绘图对象。"]
    with source, fitz.open() as target:
        if source.needs_pass or len(source) < 1:
            raise ValueError("Encrypted or empty asset")
        if len(source) > 1:
            limitations.append("仅导入 PDF 首页。")
        page = target.new_page(width=1, height=1)
        w, h = physical_size(source[0].rect, request)
        page.set_mediabox(fitz.Rect(0, 0, w, h))
        page.show_pdf_page(page.rect, source, 0)
        if fmt == "pdf":
            target.save(out)
        elif fmt == "svg":
            out.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
        elif fmt == "png":
            dpi = request.get("dpi", 150)
            if (int(w * dpi / 72) + 1) * (int(h * dpi / 72) + 1) > PIXEL_MAX:
                raise ValueError("Raster export exceeds pixel limit")
            page.get_pixmap(dpi=dpi, alpha=False).save(out)
        else:
            raise ValueError("Unsupported format")
    result = {"renderer": {"id": "static-" + kind, "version": "1"}, "editability": "layout_only",
              "panels": [{"id": "asset", "label": "静态图件"}], "objects": [], "limitations": limitations}
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:])
