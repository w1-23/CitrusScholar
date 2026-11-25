"""Verify workbench addresses for existing, unregistered Matplotlib axes.

These are not semantic identities. They are only meaningful inside a project
whose immutable renderer and typed inputs reproduce the saved replay baseline.
"""
from collections import Counter
from hashlib import sha256
import math
import re
import xml.etree.ElementTree as ET

from citrus_figures.style_replay import valid_baseline

_SVG = "{http://www.w3.org/2000/svg}"
_TOLERANCE_PT = 0.002  # Native manifest bounds round to three decimals.
_AXIS_BOUNDS = ("x0", "y0", "x1", "y1")


def _unregistered(row):
    return (row.get("identity_scope") == "render" and "semantic_key" not in row
            and row.get("identity_reason") == "unregistered")


def _finite(values):
    return all(type(value) in (float, int) and math.isfinite(value) for value in values)


def _close(left, right):
    return all(abs(a - b) <= _TOLERANCE_PT for a, b in zip(left, right))


def _axes(manifest):
    size = manifest.get("figure_size_px")
    if not isinstance(size, list) or len(size) != 2 or not _finite(size) or min(size) <= 0:
        return {}
    width, height = size
    axes = {}
    for row in manifest["objects"]:
        if row["role"] != "axes":
            continue
        locator = row["locator"]
        index = locator.get("axes_index")
        bounds = row.get("bbox")
        if (type(index) is not int or index < 0 or index in axes or row["artist"] != "Axes"
                or locator != {"axes_index": index, "role": "axes"} or row["gid"] != f"axes_{index}"
                or row.get("svg_nodes") != [row["gid"]] or not _unregistered(row)
                or not isinstance(bounds, dict) or set(bounds) != set(_AXIS_BOUNDS) or not _finite(bounds.values())):
            return {}
        x0, y0, x1, y1 = box = tuple(bounds[k] for k in _AXIS_BOUNDS)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            return {}
        # Twins/insets/stacked axes do not have independent plot regions.
        if any(min(x1, r) - max(x0, l) > _TOLERANCE_PT and min(y1, b) - max(y0, t) > _TOLERANCE_PT
               for l, t, r, b in axes.values()):
            return {}
        axes[index] = box
    return axes if 2 <= len(axes) <= 32 and set(axes) == set(range(len(axes))) else {}


def _svg_axes(svg, manifest, axes):
    if b"<!DOCTYPE" in svg.upper() or b"<!ENTITY" in svg.upper():
        return False
    receipt = manifest.get("render_receipt") or {}
    check = (receipt.get("checks") or {}).get("svg") or {}
    if (receipt.get("version") != 2 or check.get("format") != "pass" or check.get("size_bytes") != len(svg)
            or check.get("sha256") != sha256(svg).hexdigest()):
        return False
    root = ET.fromstring(svg)
    view = tuple(float(value) for value in root.get("viewBox", "").split())
    if root.tag != _SVG + "svg" or len(view) != 4 or not _finite(view) or not _close(view, (0, 0, *manifest["figure_size_px"])):
        return False
    ids = Counter(el.get("id") for el in root.iter())
    top = root.find(f"{_SVG}g[@id='figure_1']")
    if top is None or ids["figure_1"] != 1 or root.get("transform") or top.get("transform"):
        return False
    for index, box in axes.items():
        gid = f"axes_{index}"
        node = top.find(f"{_SVG}g[@id='{gid}']")
        patch = next(iter(node), None) if node is not None else None
        path = next(iter(patch), None) if patch is not None else None
        if (ids[gid] != 1 or path is None or patch.tag != _SVG + "g" or path.tag != _SVG + "path"
                or not re.fullmatch(r"patch_[0-9]+", patch.get("id", "")) or len(patch) != 1
                or any(el.get("transform") for el in (node, patch, path))):
            return False
        marks = path.get("d", "").split()
        if len(marks) != 13 or marks[::3] != ["M", "L", "L", "L", "z"]:
            return False
        points = [(float(marks[i + 1]), float(marks[i + 2])) for i in range(0, 12, 3)]
        xs, ys = map(set, zip(*points))
        if (len(xs) != 2 or len(ys) != 2 or len(set(points)) != 4 or not _finite(xs | ys)
                or not all((a[0] == b[0]) != (a[1] == b[1]) for a, b in zip(points, points[1:] + points[:1]))
                or not _close(box, (min(xs), min(ys), max(xs), max(ys)))):
            return False
    return True


class FrozenScriptIdentity:
    def __init__(self, receipt, axes):
        self.receipt = receipt
        self.panels = {index: ("frozen-script-address", index) for index in axes}

    @classmethod
    def create(cls, project, manifest, svg):
        if project.get("source_kind") != "script" or not svg:
            return None
        try:
            source = project["source_json"]
            receipt = source["frozen_render"]["receipt"]
            if (source.get("kind") != "script" or source["output_contract"]["version"] != 2
                    or not valid_baseline(receipt.get("style_replay"))):
                return None
            axes = cls._panels(manifest, receipt)
            return cls(receipt, axes) if axes and _svg_axes(svg, manifest, axes) else None
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, ET.ParseError):
            return None

    @staticmethod
    def _panels(manifest, receipt):
        return (_axes(manifest) if manifest.get("source") == "script" and manifest.get("semantic_identity_version") == 1
                and manifest.get("frozen_render_receipt") == receipt else {})

    def confirms(self, manifest):
        try:
            return self._panels(manifest, self.receipt).keys() == self.panels.keys()
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return False

    def series_key(self, index, row):
        locator = row["locator"]
        series = locator.get("series_index")
        if (index in self.panels and type(series) is int and series >= 0 and _unregistered(row)
                and locator == {"role": "series", "axes_index": index, "series_index": series}
                and row["gid"] == f"axes_{index}.series_{series}"):
            return ("frozen-script-address", row["gid"])
