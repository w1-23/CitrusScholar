"""Import, render and revision policy independent of HTTP and engine internals."""
import ast
import json
import re
import struct
from pathlib import PurePath
from uuid import uuid4

from pydantic import ValidationError

from .models import EngineDocument, FigureDocument, SourceInfo
from .store import Conflict


class FigureError(Exception):
    pass


R_SYNTAX = re.compile(r"(?:<-|\b(?:ggplot|library|plot|source|hist|boxplot|barplot|pairs|curve)\s*\(|::)")
PY_IMPORT = re.compile(r"(?m)^\s*(?:from\s+(?:matplotlib|pandas|seaborn)\b|import\s+(?:matplotlib|pandas|seaborn)\b)")


def identify(name, body, kind, language):
    if kind == "project_bundle":
        raise FigureError("此版本只导入单个脚本或静态图件。")
    if kind == "rendered_asset":
        stripped = body.lstrip(b"\xef\xbb\xbf \n\r\t")
        if body.startswith(b"\x89PNG\r\n\x1a\n") or body.startswith(b"\xff\xd8\xff"):
            return "raster"
        if body.startswith(b"%PDF-") or re.match(br"^(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<svg(?:\s|>)", stripped, re.S):
            return "vector"
        raise FigureError("静态图件支持 PNG、JPEG、SVG 或 PDF。")
    if len(body) > 512 * 1024 or b"\0" in body:
        raise FigureError("脚本须是 UTF-8 文本且不超过 512 KiB。")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError as exc:
        raise FigureError("脚本须是 UTF-8 文本。") from exc
    try:
        python = bool(ast.parse(text).body)
    except (SyntaxError, RecursionError, ValueError):
        python = False
    r = bool(R_SYNTAX.search(text))
    suffix = PurePath(name).suffix.lower()
    if language == "python":
        if python:
            return "python"
        raise FigureError("内容不是可解析的 Python 脚本。")
    if language == "r":
        if text.strip() and not PY_IMPORT.search(text):
            return "r"  # R parser in the isolated renderer is authoritative.
        raise FigureError("内容与所选 R 语言不符。")
    if python and (PY_IMPORT.search(text) or (suffix == ".py" and not r)):
        return "python"
    if r and (not python or suffix == ".r"):
        return "r"
    raise FigureError("无法从脚本内容可靠识别语言，请手动选择。")


def check_artifact(fmt, data):
    valid = False
    if fmt == "png" and len(data) >= 33 and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"):
        width, height = struct.unpack_from(">II", data, 16)
        valid = width > 0 and height > 0 and width * height <= 16_000_000
    elif fmt == "pdf":
        valid = data.startswith(b"%PDF-")
    elif fmt == "svg":
        valid = bool(re.match(br"\s*(?:<\?xml[^>]*>\s*)?(?:<!DOCTYPE\s+svg[^>]*>\s*)?<svg[\s>]", data[:8192], re.S))
    if not valid:
        raise FigureError("渲染器产物与请求格式不符或尺寸超限。")


def saved_edits(row):
    saved = json.loads(row["overrides"])
    return saved.get("normalized", {}), saved.get("native", [])


class Figures:
    def __init__(self, store, run):
        self.store = store
        self.run = run

    def render(self, language, body, settings):
        result, output = self.run(language if language in {"python", "r"} else "static", body, settings)
        try:
            engine = EngineDocument.model_validate(result)
            keys = [o.id for o in engine.objects]
            panels = [p.id for p in engine.panels]
            if len(set(keys)) != len(keys) or len(set(panels)) != len(panels):
                raise FigureError("渲染器返回重复对象标识。")
            if any(o.panel_id is not None and o.panel_id not in panels for o in engine.objects):
                raise FigureError("渲染器返回不存在的面板标识。")
        except ValidationError as exc:
            raise FigureError("渲染器返回了无效的图件文档。") from exc
        check_artifact(settings["format"], output)
        return engine, output

    def import_figure(self, owner, asset, request, *, library_ref=None, library_version=None):
        if request.source.assets:
            raise FigureError("当前版本不支持脚本依赖资源的多文件导入。")
        lang = identify(asset["name"], asset["body"], request.source.kind, request.source.language)
        engine, preview = self.render(lang, asset["body"], {"overrides": {}, "format": "png", "dpi": 120})
        mode = request.options.parse_mode
        if mode == "editable" and not (any(o.editable for o in engine.objects)
                                      or engine.native_editability in {"semantic", "partial"}):
            raise FigureError("来源没有可验证的语义编辑对象。可改用自动或仅预览导入。")
        if mode == "preview_only":
            engine.editability = "preview_only"
            for obj in engine.objects:
                obj.editable = []
        layout = {}
        if engine.native_manifest and len(engine.native_manifest.get("size_mm", [])) == 2:
            layout = dict(zip(("width_mm", "height_mm"), engine.native_manifest["size_mm"]))
        doc = FigureDocument(**engine.model_dump(), figure_id=f"figure_{uuid4().hex}", name=asset["name"],
                             revision=1, layout=layout, source=SourceInfo(language=lang, source_ref=asset["id"],
                             source_hash=asset["sha"], library_asset_ref=library_ref, library_version=library_version))
        self.store.create(owner, doc, preview)
        return doc

    def get(self, owner, ref, revision=None):
        row = self.store.revision(owner, ref, revision)
        return FigureDocument.model_validate_json(row["doc"]), row

    def edit(self, owner, ref, change):
        doc, row = self.get(owner, ref)
        if doc.revision != change.expected_revision:
            raise Conflict
        editable = {obj.id for obj in doc.objects if "text" in obj.editable}
        if not set(change.overrides) <= editable:
            raise FigureError("含有不存在或不可编辑的对象。")
        normalized, native = saved_edits(row)
        if native:
            raise FigureError("此图件已有原生编辑，请继续使用原生补丁接口。")
        normalized.update({k: v.model_dump() for k, v in change.overrides.items()})
        return self._save(owner, doc, row, normalized, native)

    def edit_native(self, owner, ref, change):
        doc, row = self.get(owner, ref)
        if doc.revision != change.expected_revision:
            raise Conflict
        if not doc.native_manifest or doc.editability == "preview_only":
            raise FigureError("此图件没有可编辑的 Tavotto 原生对象。")
        normalized, native = saved_edits(row)
        merged = {(patch["gid"], patch["prop"]): patch for patch in native}
        for patch in change.patches:
            key = (patch.gid, patch.prop)
            merged.pop(key, None)
            merged[key] = patch.model_dump(exclude_none=True)
        return self._save(owner, doc, row, normalized, list(merged.values()))

    def _save(self, owner, doc, row, normalized, native):
        asset = self.store.asset(owner, row["asset"])
        engine, preview = self.render(doc.source.language, asset["body"], {
            "overrides": normalized, **({"native_patches": native} if native else {}), "format": "png", "dpi": 120,
        })
        self._verify_normalized(engine, normalized)
        revised = doc.model_copy(update={"revision": doc.revision + 1, **engine.__dict__})
        self.store.save(owner, revised, {"normalized": normalized, "native": native}, preview, doc.revision)
        return revised

    @staticmethod
    def _verify_normalized(engine, normalized):
        rendered = {o.id: o.properties.get("text") for o in engine.objects}
        if any(rendered.get(k) != v["text"] for k, v in normalized.items()):
            raise FigureError("重绘无法确认所有编辑，未写入新版本或导出记录。")

    def restore(self, owner, ref, change):
        doc, _ = self.get(owner, ref)
        if doc.revision != change.expected_revision:
            raise Conflict
        old, row = self.get(owner, ref, change.revision)
        revised = old.model_copy(update={"revision": doc.revision + 1})
        self.store.save(owner, revised, json.loads(row["overrides"]), row["preview"], doc.revision)
        return revised

    def export(self, owner, ref, options):
        doc, row = self.get(owner, ref)
        asset = self.store.asset(owner, row["asset"])
        params = options.model_dump(exclude_none=True)
        normalized, native = saved_edits(row)
        settings = dict(params, overrides=normalized)
        if native:
            settings["native_patches"] = native
        engine, output = self.render(doc.source.language, asset["body"], settings)
        self._verify_normalized(engine, normalized)
        export_id, sha = self.store.export_record(ref, doc.revision, options.format, params, output)
        return output, export_id, sha, doc.revision
