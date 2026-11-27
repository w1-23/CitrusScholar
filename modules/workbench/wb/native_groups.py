"""Atomic motion for native automatic axes groups.

Adapted from Tavotto (AGPL-3.0-only, pinned in TAVOTTO_UPSTREAM.lock.json):
web/src/lib/elementGeom.ts alignEntries and web/src/canvas/interactions.ts
groupMove. This adapter translates the axes-group members, not page objects.
"""
import math

from .figures import FigureError
from .models import NativeEdit, NativePatch
from .picking import describe_group, expand_groups, group_transform_blocked
from .store import Conflict, Missing


def _manifest(doc):
    if doc.native_manifest is None:
        raise FigureError("此图件没有 Tavotto 原生分组。")
    return doc.native_manifest


def group_info(doc, gid):
    found = describe_group(_manifest(doc), gid)
    if found is None:
        raise Missing
    return found


def resolve_groups(doc, settings, selection):
    manifest = _manifest(doc)
    known = {g["gid"] for g in manifest.get("groups", [])}
    return {"revision": doc.revision,
            "expanded_gids": expand_groups(manifest, selection.gids, selection.locked_gids, settings),
            "blocked": {g: reason for g in selection.gids if g in known and
                        (reason := group_transform_blocked(manifest, g, selection.locked_gids, settings))}}


def _round4(value):
    return math.floor(value * 10000 + 0.5) / 10000


def _group_members(doc, settings, gid, operation):
    if doc.revision != operation.expected_revision:
        raise Conflict
    manifest = _manifest(doc)
    group_info(doc, gid)
    reason = group_transform_blocked(manifest, gid, operation.locked_gids, settings)
    if reason:
        raise FigureError(f"原生组不可整体变换（{reason}）。")
    members = expand_groups(manifest, [gid], operation.locked_gids, settings)
    positions = {p["gid"]: p["value"] for p in settings if p["prop"] == "position"}
    elements = {e["gid"]: e for e in manifest["elements"]}
    results = {}
    for member in members:
        target_id = elements[member].get("geom_gid") or member
        element = elements.get(target_id, elements[member])
        target_id = element["gid"]
        value = positions.get(target_id)
        if value is None:
            value = next(p["value"] for p in element["editable"] if p["prop"] == "position")
        results[target_id] = (element.get("identity"), value[:4])
    if not results:
        raise FigureError("原生组没有可变换的成员。")
    return results


def _batch(doc, members, transform):
    patches = [NativePatch(gid=gid, prop="position", identity=identity,
                           value=[_round4(v) for v in transform(*rect)])
               for gid, (identity, rect) in members.items()]
    return NativeEdit(expected_revision=doc.revision, patches=patches)


def move_group(doc, settings, gid, movement):
    members = _group_members(doc, settings, gid, movement)
    width, height = doc.native_manifest["size_mm"]
    dx, dy = movement.dx_mm / width, movement.dy_mm / height
    return _batch(doc, members, lambda x, y, w, h: (x + dx, y - dy, w, h))


def scale_group(doc, settings, gid, operation):
    # Tavotto axesLayout.ts scaleGroupAbout. Working in bottom-origin axes
    # coordinates is affine-equivalent to scaling its top-origin union box.
    members = _group_members(doc, settings, gid, operation)
    rects = [rect for _, rect in members.values()]
    left, bottom = min(r[0] for r in rects), min(r[1] for r in rects)
    right, top = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    cx, cy = (left + right) / 2, (bottom + top) / 2
    factor = max(operation.factor, 0.02 / (right - left), 0.02 / (top - bottom))
    return _batch(doc, members, lambda x, y, w, h: (cx + (x - cx) * factor,
                                                  cy + (y - cy) * factor, w * factor, h * factor))
