"""Verify renderer addresses against the exact owned or replayed SVG bytes."""
from collections import defaultdict
from hashlib import sha256
import xml.etree.ElementTree as ET

_SVG = "{http://www.w3.org/2000/svg}"


class SvgTargets:
    """Index actual nodes, retaining duplicate IDs and structural ownership."""

    def __init__(self, svg, manifest):
        self.axes = {}
        self.svg = svg
        self.nodes = defaultdict(list)
        self.parents = {}
        self.figure = None
        if not isinstance(svg, bytes) or not svg or b"<!DOCTYPE" in svg.upper() or b"<!ENTITY" in svg.upper():
            return
        receipt = manifest.get("render_receipt") or {}
        checks = receipt.get("checks") if isinstance(receipt, dict) else None
        check = checks.get("svg") if isinstance(checks, dict) else None
        if not isinstance(check, dict):
            return
        if (receipt.get("version") != 2 or check.get("format") != "pass"
                or check.get("size_bytes") != len(svg) or check.get("sha256") != sha256(svg).hexdigest()):
            return
        try:
            root = ET.fromstring(svg)
        except (ET.ParseError, ValueError):
            return
        if root.tag != _SVG + "svg":
            return
        for node in root.iter():
            if node.get("id"):
                self.nodes[node.get("id")].append(node)
            for child in node:
                self.parents[child] = node
        self.figure = self._group("figure_1", root)
        for row in manifest.get("objects", []):
            if not isinstance(row, dict) or row.get("role") != "axes" or not isinstance(row.get("locator"), dict):
                continue
            index, gid = row.get("locator", {}).get("axes_index"), row.get("gid")
            if type(index) is not int or index < 0:
                continue
            owner = self._group(gid, self.figure) if row.get("svg_nodes") == [gid] else None
            self.axes[index] = owner if index not in self.axes else None

    def _group(self, gid, parent):
        items = self.nodes.get(gid, ())
        if parent is not None and len(items) == 1 and items[0].tag == _SVG + "g" and self.parents.get(items[0]) is parent:
            return items[0]
        return None

    def axes_gid(self, row):
        index = row.get("locator", {}).get("axes_index")
        axis = self.axes.get(index) if type(index) is int else None
        return axis.get("id") if axis is not None else None

    def verifies(self, row):
        index = row.get("locator", {}).get("axes_index")
        if type(index) is not int:
            return False
        owner = self.axes.get(index)
        if owner is None:
            return False
        gid, ids = row.get("gid"), row.get("svg_nodes")
        if row.get("role") == "axes":
            return owner.get("id") == gid
        return self._member_ids(row) and all(self._group(node_id, owner) is not None for node_id in ids)

    @staticmethod
    def _member_ids(row):
        gid, ids = row.get("gid"), row.get("svg_nodes")
        # Members of bar and errorbar series have renderer-assigned, ordered
        # addresses. A lookup elsewhere (including a legend or defs) is invalid.
        return bool(isinstance(gid, str) and isinstance(ids, list) and ids
                    and all(node_id == (gid if pos == 0 else f"{gid}.member_{pos}")
                            for pos, node_id in enumerate(ids)))

    def verifies_absence(self, row):
        """A deliberate hide is a verified absence, never a canvas hit."""
        return bool(self.axes_gid(row) and row.get("role") == "series" and row.get("visible") is False
                    and self._member_ids(row) and not any(node_id in self.nodes for node_id in row["svg_nodes"]))
