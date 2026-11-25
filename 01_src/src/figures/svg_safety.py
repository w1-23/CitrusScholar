"""Shared allowlist and canonical SVG normalization for figure projects."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_NS = "http://www.w3.org/2000/svg"
_SVG_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "title", "desc", "defs", "linearGradient",
    "radialGradient", "stop", "clipPath", "marker",
}
_PAINTABLE_TAGS = frozenset({
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan",
})
_SVG_ATTRS = {
    "viewBox", "preserveAspectRatio", "stroke-width", "stroke-linecap",
    "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset", "fill-opacity",
    "stroke-opacity", "font-family", "font-size", "font-weight", "font-style",
    "text-anchor", "dominant-baseline", "gradientUnits", "gradientTransform",
    "stop-color", "stop-opacity", "clipPathUnits", "markerWidth",
    "markerHeight", "markerUnits", "marker-start", "marker-mid", "marker-end",
    "aria-label", "clip-path", "role", "id", "href", "x", "y", "x1", "y1", "x2", "y2",
    "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "fill",
    "stroke", "opacity", "transform", "offset",
}
_FORBIDDEN_TAGS = frozenset({
    "script", "foreignObject", "iframe", "object", "embed", "handler",
    "listener", "audio", "video", "canvas",
})
_FORBIDDEN_ATTRS = frozenset({
    "src", "action", "formaction", "srcdoc", "target", "download",
    "ping", "poster", "data", "srcset",
})
_ID_RE = re.compile(r"[A-Za-z][\w:.-]{0,100}")
_LOCAL_URL_RE = re.compile(r"url\(\s*#[A-Za-z][\w:.-]{0,100}\s*\)", re.I)
_LOCAL_HREF_RE = re.compile(r"#[A-Za-z][\w:.-]{0,100}")
_ACTIVE_VALUE_RE = re.compile(r"javascript:|data:|https?:|file:|//", re.I)
_MAX_SOURCE_BYTES = 100_000
_MAX_NODES = 5000
_PLOT_ATTRS = {"stroke-miterlimit", "textLength", "lengthAdjust",
               "{http://www.w3.org/XML/1998/namespace}space"}

# ── The shared sanitizer ──────────────────────────────────────────────────

def sanitize_svg(source: str | bytes, *, plot: bool = False) -> str:
    """Return canonical, bounded SVG markup or raise ``ValueError``.

    The allowlist is unchanged in spirit from ``draw_svg``'s original
    sanitizer. Documents that use scripts, ``foreignObject`` or other active
    containers, event handlers, navigation attributes, external ``href``-like
    attributes or non-local ``url()`` references are rejected rather than
    silently rewritten. Unknown benign attributes are still dropped so legacy
    chat drawings keep working.
    """
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("SVG must be UTF-8 text") from exc
    source = str(source or "").strip()
    if not source or len(source.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("SVG is empty or too large")
    if re.search(r"<!DOCTYPE|<!ENTITY", source, re.I):
        raise ValueError("SVG contains a forbidden declaration")
    root = ET.fromstring(source)
    if root.tag != f"{{{_NS}}}svg":
        raise ValueError("Root element must be an SVG document")
    allowed_attributes = _SVG_ATTRS | _PLOT_ATTRS if plot else _SVG_ATTRS
    nodes = 0

    def clean(node: ET.Element) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES:
            raise ValueError("Unsupported SVG complexity")
        if node.tag.startswith("{"):
            namespace = node.tag.split("}", 1)[0][1:]
            if namespace != _NS:
                raise ValueError("Unsupported SVG namespace")
        tag = node.tag.split("}", 1)[-1]
        if tag in _FORBIDDEN_TAGS:
            raise ValueError(f"Forbidden SVG element: {tag}")
        if tag not in _SVG_TAGS:
            raise ValueError(f"Unsupported SVG element: {tag}")
        node.tag = tag
        for key in list(node.attrib):
            value = node.attrib[key]
            local = key.split("}", 1)[-1]
            if len(local) > 2 and local.casefold().startswith("on"):
                raise ValueError(f"Forbidden active SVG attribute: {local}")
            if local.casefold() in _FORBIDDEN_ATTRS:
                raise ValueError(f"Forbidden SVG attribute: {local}")
            if local.casefold() == "href":
                if not _LOCAL_HREF_RE.fullmatch(value):
                    raise ValueError("External SVG href reference")
                if key not in allowed_attributes:
                    del node.attrib[key]
                continue
            if key not in allowed_attributes:
                del node.attrib[key]
                continue
            if len(value) > 4000 or re.search(r"[<>\x00-\x1f]", value):
                del node.attrib[key]
                continue
            if key == "id" and not _ID_RE.fullmatch(value):
                del node.attrib[key]
                continue
            if re.search(r"url\s*\(", value, re.I) and not _LOCAL_URL_RE.fullmatch(value):
                raise ValueError("External or non-local SVG url() reference")
            if _ACTIVE_VALUE_RE.search(value):
                raise ValueError("External or active SVG attribute value")
        for child in list(node):
            clean(child)

    clean(root)
    if not root.get("viewBox") and not root.get("width"):
        raise ValueError("SVG must provide viewBox or width")
    root.set("xmlns", _NS)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)
