"""Lossless presentation adapter for known Matplotlib and svglite SVG dialects.

Only compiled-in exporter rules are converted. There is no general CSS parsing,
rendering, geometry reconstruction, or rendering of unsupported originals.
"""
from __future__ import annotations

from copy import deepcopy
import re
import xml.etree.ElementTree as ET

from src.figures import svg_safety

NS = "{http://www.w3.org/2000/svg}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_MPL_DOCTYPE = ('<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" '
                '"http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">')
_MPL_CSS = "*{stroke-linejoin:round;stroke-linecap:butt}"
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CC = "{http://creativecommons.org/ns#}"
_MPL_METADATA = {_RDF + "RDF", _CC + "Work", _CC + "Agent",
                 *(_DC + key for key in ("type", "date", "format", "creator", "title", "description", "rights"))}
_R_PAINTED = {"line", "polyline", "polygon", "path", "rect", "circle"}
_R_CSS = (".svglite line,.svglite polyline,.svglite polygon,.svglite path,.svglite rect,.svglite circle"
          "{fill:none;stroke:#000000;stroke-linecap:round;stroke-linejoin:round;stroke-miterlimit:10.00;}"
          ".svglite text{white-space:pre;}.svglite g.glyphgroup path{fill:inherit;stroke:none;}")
_R_CLIP_ID = re.compile(r"cp[A-Za-z0-9_-]{1,80}={1,2}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[Ee][+-]?\d+)?")
_SIZE = re.compile(r"(?:\d+\.?\d*|\.\d+)(?:px|pt)?")
_INLINE_VALUES = {
    "fill": r"none|#[0-9a-fA-F]{6}", "stroke": r"none|#[0-9a-fA-F]{6}",
    "stroke-width": _SIZE.pattern, "stroke-dashoffset": _NUMBER.pattern,
    "stroke-dasharray": r"none|[0-9., ]+", "stroke-linecap": r"butt|round|square",
    "stroke-linejoin": r"miter|round|bevel", "stroke-miterlimit": _SIZE.pattern,
    "opacity": r"[0-9.]+", "fill-opacity": r"[0-9.]+", "stroke-opacity": r"[0-9.]+",
    "font-size": _SIZE.pattern, "font-family": r"[\w ,'\".-]{1,200}",
    "font-weight": r"normal|bold|[1-9]00", "font-style": r"normal|italic|oblique",
    "text-anchor": r"start|middle|end",
}


def _unsupported():
    return ValueError("Unsupported Nature plot SVG dialect")


def _inline(style):
    declared = {}
    for token in style.split(";"):
        if not token.strip():
            continue
        name, sep, value = token.partition(":")
        name, value = name.strip(), value.strip()
        regex = _INLINE_VALUES.get(name)
        if not sep or not regex or name in declared or not re.fullmatch(regex, value):
            raise _unsupported()
        declared[name] = value
    return declared


def _parse(source, backend):
    if len(source) > svg_safety._MAX_SOURCE_BYTES or backend not in {"python", "r"}:
        raise _unsupported()
    text = source.decode("utf-8").strip()
    text = re.sub(r"^<\?xml\s+[^<>?]+\?>", "", text, count=1)
    if backend == "python":
        doctype = re.match(r"\s*(<!DOCTYPE\s+[^<>]+>)", text)
        if doctype and " ".join(doctype[1].split()) == _MPL_DOCTYPE:
            text = text[doctype.end():]
    if re.search(r"<!DOCTYPE|<!ENTITY|<\?", text, re.I):
        raise _unsupported()
    root = ET.fromstring(text.lstrip())
    if root.tag != NS + "svg" or not root.get("viewBox"):
        raise _unsupported()
    nodes = list(root.iter())
    if len(nodes) > svg_safety._MAX_NODES:
        raise _unsupported()
    styles = [node for node in nodes if node.tag == NS + "style"]
    expected = _MPL_CSS if backend == "python" else _R_CSS
    if len(styles) != 1 or styles[0].attrib != {"type": "text/css"} or len(styles[0]):
        raise _unsupported()
    style = styles[0]
    css = " ".join((style.text or "").split())
    css = re.sub(r"\s*([{},:;])\s*", r"\1", css)
    if css != expected:
        raise _unsupported()
    parents = {child: parent for parent in nodes for child in parent}
    if parents[style].tag != NS + "defs":
        raise _unsupported()
    parents[style].remove(style)
    if backend == "python":
        root.attrib.pop("version", None)
        # Matplotlib RDF describes the original artifact, kept separately. It
        # is not needed on the display derivative or parsed as SVG content.
        for node in root.findall(NS + "metadata"):
            if node.attrib or any(child.tag not in _MPL_METADATA or
                (child.attrib and not (child.tag == _DC + "type" and
                    child.attrib == {_RDF + "resource": "http://purl.org/dc/dcmitype/StillImage"}))
                for child in list(node.iter())[1:]):
                raise _unsupported()
            root.remove(node)
    elif len(root) != 1 or root[0].tag != NS + "g" or root[0].get("class") != "svglite":
        raise _unsupported()
    return root


def _convert_styles(root, backend):
    nodes = list(root.iter())
    parents = {child: parent for parent in nodes for child in parent}
    clip_renames = {
        node.get("id"): node.get("id").rstrip("=")
        for node in nodes if backend == "r" and node.tag == NS + "clipPath"
        and _R_CLIP_ID.fullmatch(node.get("id", ""))
    }
    ids = {}
    for node in nodes:
        tag = node.tag.removeprefix(NS)
        if node.tag != NS + tag or tag not in svg_safety._SVG_TAGS | {"use"}:
            raise _unsupported()
        if tag == "use" and backend != "python":
            raise _unsupported()
        if "class" in node.attrib:
            cls = node.attrib.pop("class")
            if not (backend == "r" and node is root[0] and cls == "svglite"):
                # The pinned fixtures have no embedded glyph groups. Reject
                # future patterns instead of guessing their CSS semantics.
                raise _unsupported()
        if tag == "use":
            if not set(node.attrib) <= {XLINK_HREF, "x", "y", "style", "id"}:
                raise _unsupported()
        else:
            allowed = svg_safety._SVG_ATTRS | svg_safety._PLOT_ATTRS | {"style"}
            if not set(node.attrib) <= allowed or "href" in node.attrib:
                raise _unsupported()
        if "id" in node.attrib:
            ident = node.attrib["id"] = clip_renames.get(node.attrib["id"], node.attrib["id"])
            if not svg_safety._ID_RE.fullmatch(ident) or ident in ids or ident.startswith("nature-"):
                raise _unsupported()
            ids[ident] = node
        if "clip-path" in node.attrib:
            node.attrib["clip-path"] = re.sub(r"^url\(#([^)]*)\)$",
                lambda m: f"url(#{clip_renames.get(m[1], m[1])})", node.attrib["clip-path"])
        inline = _inline(node.attrib.pop("style", ""))
        if backend == "r" and tag in _R_PAINTED:
            node.attrib.update({"fill": "none", "stroke": "#000000", "stroke-linecap": "round",
                                "stroke-linejoin": "round", "stroke-miterlimit": "10.00"})
        elif backend == "r" and tag == "text":
            node.set(XML_SPACE, "preserve")
        elif backend == "python":
            node.attrib.update({"stroke-linejoin": "round", "stroke-linecap": "butt"})
        node.attrib.update(inline)
    for node in nodes:
        if node.get("clip-path"):
            value = node.get("clip-path")
            if not svg_safety._LOCAL_URL_RE.fullmatch(value) or ids.get(value[5:-1]) is None or ids[value[5:-1]].tag != NS + "clipPath":
                raise _unsupported()
        if node.tag != NS + "use":
            continue
        href = node.get(XLINK_HREF, "")
        target = ids.get(href[1:]) if svg_safety._LOCAL_HREF_RE.fullmatch(href) else None
        if (target is None or target.tag != NS + "path" or parents[target].tag != NS + "defs"
                or not target.get("d") or target.get("transform") or target.get("clip-path") or len(target)):
            raise _unsupported()
        x, y = node.get("x", "0"), node.get("y", "0")
        if not _NUMBER.fullmatch(x) or not _NUMBER.fullmatch(y) or len(node):
            raise _unsupported()
        # A referenced simple path retains its own attributes over inherited
        # use styles, and its coordinates are translated exactly once.
        path = deepcopy(target)
        use_style = {key: value for key, value in node.attrib.items() if key in _INLINE_VALUES}
        path.attrib = {**use_style, **path.attrib}
        path.attrib.pop("id", None)
        if node.get("id"):
            path.set("id", node.attrib["id"])
        path.set("transform", f"translate({x} {y})")
        path.tail = node.tail
        parent = parents[node]
        index = list(parent).index(node)
        parent.remove(node)
        parent.insert(index, path)
    return ids


def _mark_objects(root, ids):
    def walk(node, locked=False):
        tag = node.tag.removeprefix(NS)
        locked = locked or tag in {"defs", "clipPath", "marker", "linearGradient", "radialGradient"}
        if not locked and tag in svg_safety._PAINTABLE_TAGS:
            yield node
        for child in node:
            yield from walk(child, locked)

    objects = list(walk(root))
    if len(objects) > 600 or not any(node.tag == NS + "text" and node.text for node in objects):
        raise _unsupported()
    if not any(node.tag not in {NS + "text", NS + "tspan"} for node in objects):
        raise _unsupported()
    for i, node in enumerate(objects, 1):
        # Generate document-local immutable IDs so a saved edit addresses one
        # native mark. No inferred series, tick mapping, or scientific meaning.
        ident = f"nature-{node.tag.removeprefix(NS)}-{i}"
        if ident in ids:
            raise _unsupported()
        node.set("id", ident)


def normalize_nature_svg(source: bytes, backend: str) -> str:
    """Flatten one pinned exporter dialect to the shared inert SVG subset."""
    try:
        root = _parse(source, backend)
        ids = _convert_styles(root, backend)
        _mark_objects(root, ids)
        text = ET.tostring(root, encoding="unicode")
        safe = svg_safety.sanitize_svg(text, plot=True)
        # The base sanitizer drops some unknown or overlong attributes for its
        # legacy consumers. A provenance derivative instead fails on any loss.
        before = list(root.iter())
        after = list(ET.fromstring(safe).iter())
        if len(before) != len(after) or any(a.attrib != b.attrib for a, b in zip(before, after)):
            raise _unsupported()
        return safe
    except (ET.ParseError, UnicodeError, RecursionError) as exc:
        raise _unsupported() from exc


def plot_objects(svg: str):
    """List presentation controls without inventing target geometry or changing text."""
    for node in ET.fromstring(svg).iter():
        ident = node.get("id", "")
        if not ident.startswith("nature-"):
            continue
        tag = node.tag.removeprefix(NS)
        text = tag in {"text", "tspan"}
        values = {}
        fill = node.get("fill", "#000000" if text else "")
        stroke = node.get("stroke", "")
        if _COLOR.fullmatch(fill):
            values["color" if text else "facecolor"] = fill.lower()
        if _COLOR.fullmatch(stroke):
            values["edgecolor"] = stroke.lower()
            width = node.get("stroke-width", "1")
            if _NUMBER.fullmatch(width) and 0.2 <= float(width) <= 8:
                values["linewidth"] = float(width)
        if values:
            yield ident, tag, (node.text or "").strip()[:80] if text else "", values


PLOT_PROPERTIES = {"color": "fill", "facecolor": "fill", "edgecolor": "stroke", "linewidth": "stroke-width"}


def validate_plot_edits(svg, edits):
    """Reject edits outside existing paint on individual, native plot objects."""
    plots = {ident: {PLOT_PROPERTIES[key] for key in values} for ident, _, _, values in plot_objects(svg)}
    for edit in edits:
        attribute = edit.get("attribute")
        value = edit["value"]
        if attribute == "stroke-width":
            valid = _NUMBER.fullmatch(value) and 0.2 <= float(value) <= 8
        else:
            valid = _COLOR.fullmatch(value)
        if attribute not in plots.get(edit["target"], set()) or not valid:
            raise ValueError("Unsupported Nature plot presentation edit")
