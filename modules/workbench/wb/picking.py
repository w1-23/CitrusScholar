# SPDX-License-Identifier: AGPL-3.0-only
"""Tavotto's figure-space point picking and manifest group selection.

Adapted from Tavotto b6de338c3b8d104d64930f18828b9675acb261a5
(https://github.com/Tavotto/Tavotto), licensed AGPL-3.0-only. In web/src:
canvas/interactions.ts (arrowDistMm, pickElementStack, pickElement,
cycleElementAt), lib/pathGeom.ts (manifest geometry scoring),
lib/elementGeom.ts (isElementHidden, entryUnder, groupTransformBlocked,
expandGroups, geomTarget, positionOf), lib/effectiveOverride.ts (last-wins),
components/inspector/roles/hierarchy.ts (structuralParent, ancestorsOf,
groupByGid) and components/inspector/GroupPage.tsx (member selection).

Coordinates are full-figure fractions (Y down), as in the native manifest.
No panel transform, viewport, render state, or grouped layout mutation is
performed by these functions.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from math import ceil, hypot, inf

PATH_HIT_MM = 1.5
OPAQUE_FILL_ALPHA = 0.5
STROKE_DIST_BUCKET_MM = 0.5
_BBOX_PAD = 0.004
_HIT_PENALTY = {"axes": 10, "axes3d": 10, "ticks": 20, "ticklabel": 20}
_INK_BBOX_ROLES = {"text", "title", "axis_label", "legend", "legend_text"}
_TICK_LABEL = re.compile(r"^(.*)\.([xyz])ticklabels_[0-9]+$")


def is_element_hidden(element: dict) -> bool:
    return any(
        field.get("prop") == "visible" and field.get("value") is False
        for field in element.get("editable", ())
    )


def _in_clip(geometry: dict, x: float, y: float) -> bool:
    clip = geometry.get("clip")
    if clip is None:
        return True
    left, top, width, height = clip
    return left <= x <= left + width and top <= y <= top + height


def _segments(geometry: dict) -> Iterator[tuple[Sequence[float], Sequence[float]]]:
    for path in geometry["paths"]:
        points = path["points"]
        yield from zip(points, points[1:])
        # Matplotlib fills open paths as if closed, including the closing edge.
        if (path.get("closed") or geometry.get("fill")) and len(points) > 2:
            yield points[-1], points[0]


def _segment_distance_mm(size, px, py, a, b) -> float:
    sw, sh = size
    ax, ay = a[0] * sw, a[1] * sh
    dx, dy = b[0] * sw - ax, b[1] * sh - ay
    length_sq = dx * dx + dy * dy
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq if length_sq else 0
    t = min(max(t, 0), 1)
    return hypot(px - (ax + dx * t), py - (ay + dy * t))


def _geom_distance_mm(geometry: dict, size, x: float, y: float) -> float:
    if not _in_clip(geometry, x, y):
        return inf
    px, py = x * size[0], y * size[1]
    return min(
        (_segment_distance_mm(size, px, py, a, b) for a, b in _segments(geometry)),
        default=inf,
    )


def _geom_contains(geometry: dict, x: float, y: float) -> bool:
    if not geometry.get("fill") or not _in_clip(geometry, x, y):
        return False
    # SVG's nonzero winding rule. Same-direction rings do not form holes.
    winding = 0
    for path in geometry["paths"]:
        points = path["points"]
        if len(points) < 3:
            continue
        ax, ay = points[-1]
        for bx, by in points:
            side = (bx - ax) * (y - ay) - (x - ax) * (by - ay)
            if ay <= y < by and side > 0:
                winding += 1
            elif by <= y < ay and side < 0:
                winding -= 1
            ax, ay = bx, by
    return winding != 0


def _geom_area_frac(geometry: dict) -> float:
    total = 0.0
    for path in geometry["paths"]:
        points = path["points"]
        if not (path.get("closed") or geometry.get("fill")) or len(points) < 3:
            continue
        area = 0.0
        ax, ay = points[-1]
        for bx, by in points:
            area += ax * by - bx * ay
            ax, ay = bx, by
        total += abs(area) / 2
    return total


def _geom_ink_area_frac(geometry: dict, size, tolerance: float) -> float:
    sw, sh = size
    if sw * sh <= 0:
        return 0.0
    length = 0.0
    for a, b in _segments(geometry):
        length += hypot((b[0] - a[0]) * sw, (b[1] - a[1]) * sh)
    return (length * 2 * tolerance) / (sw * sh)


def _half_stroke_mm(geometry: dict) -> float:
    pt = (geometry.get("stroke_pt") or 0) if geometry.get("stroke") else 0
    return (pt * 25.4) / 72 / 2


def _stroke_bucket(half_stroke: float, distance: float) -> int:
    edge = distance - half_stroke
    return 0 if edge <= 1e-9 else ceil(edge / STROKE_DIST_BUCKET_MM - 1e-9)


def _paint_order(obj: dict) -> int:
    order = obj.get("paint_order")
    return -1 if order is None else order


@dataclass(slots=True)
class _Hit:
    gid: str
    index: int
    tier: int
    score: float
    order: int
    bucket: int = 0

    def sort_key(self) -> tuple:
        if self.tier == 0:
            return self.tier, self.bucket, self.score, -self.order, self.index
        return self.tier, self.score, self.index


def _arrow_hit(element: dict, index: int, size, x: float, y: float) -> _Hit | None:
    sw, sh = size
    a, b = element["arrow_endpoints"][:2]
    distance = _segment_distance_mm(size, x * sw, y * sh, a, b)
    if distance > PATH_HIT_MM:
        return None
    length = hypot((b[0] - a[0]) * sw, (b[1] - a[1]) * sh)
    score = (length * 2 * PATH_HIT_MM) / (sw * sh)
    return _Hit(element["gid"], index, 0, score, _paint_order(element), _stroke_bucket(0, distance))


def _geometry_hit(element: dict, index: int, size, x: float, y: float, inside: bool) -> _Hit | None:
    geometry = element["geometry"]
    half = _half_stroke_mm(geometry)
    tolerance = max(PATH_HIT_MM, half)
    fill, stroke = geometry.get("fill"), geometry.get("stroke")
    distance = inf if inside and fill and not stroke else _geom_distance_mm(geometry, size, x, y)
    near = distance <= tolerance
    if not inside and not near:
        return None
    order = _paint_order(geometry)
    if near and (not fill or stroke):
        score = _geom_ink_area_frac(geometry, size, tolerance)
        return _Hit(element["gid"], index, 0, score, order, _stroke_bucket(half, distance))
    if inside:
        score = _geom_area_frac(geometry) * _HIT_PENALTY.get(element.get("role"), 1)
    else:
        score = _geom_ink_area_frac(geometry, size, tolerance)
    return _Hit(element["gid"], index, 1, score, order)


def _bbox_hit(element: dict, index: int, x: float, y: float) -> _Hit | None:
    left, top, width, height = element["bbox"]
    if not (left - _BBOX_PAD <= x <= left + width + _BBOX_PAD):
        return None
    if not (top - _BBOX_PAD <= y <= top + height + _BBOX_PAD):
        return None
    role, order = element.get("role"), _paint_order(element)
    tier = 0 if role in _INK_BBOX_ROLES and order >= 0 else 1
    return _Hit(element["gid"], index, tier, width * height * _HIT_PENALTY.get(role, 1), order)


def _pick_element_stack(manifest: dict, x: float, y: float, locked_gids: Sequence[str]) -> list[str]:
    locked = set(locked_gids)
    hits: list[_Hit] = []
    covers: list[tuple[str, int]] = []
    for index, element in enumerate(manifest["elements"]):
        gid = element["gid"]
        if gid == "figure" or is_element_hidden(element):
            continue
        geometry = element.get("geometry")
        if len(element.get("arrow_endpoints") or ()) >= 2:
            hit = None if gid in locked else _arrow_hit(element, index, manifest["size_mm"], x, y)
        elif geometry is not None:
            inside = _geom_contains(geometry, x, y)
            order = _paint_order(geometry)
            if inside and order >= 0 and (geometry.get("fill_alpha") or 0) >= OPAQUE_FILL_ALPHA:
                covers.append((gid, order))  # Even if locked, visible fill can occlude.
            hit = None if gid in locked else _geometry_hit(element, index, manifest["size_mm"], x, y, inside)
        else:
            hit = None if gid in locked else _bbox_hit(element, index, x, y)
        if hit is not None:
            hits.append(hit)

    for hit in hits:
        if hit.order >= 0 and any(gid != hit.gid and order > hit.order for gid, order in covers):
            hit.tier = 2
    return [hit.gid for hit in sorted(hits, key=_Hit.sort_key)]


class _Hierarchy:
    def __init__(self, manifest: dict):
        self.elements = {element["gid"]: element for element in manifest["elements"]}
        self.groups = {group["gid"]: group for group in manifest.get("groups") or ()}

    def has(self, gid: str) -> bool:
        return gid in self.elements or gid in self.groups

    def parent(self, gid: str) -> str | None:
        if gid == "figure":
            return None
        if gid in self.groups:
            return "figure"
        explicit = self.elements.get(gid, {}).get("parent_gid")
        if explicit and explicit != gid and self.has(explicit):
            return explicit
        tick = _TICK_LABEL.fullmatch(gid)
        if tick:
            ticks_gid = f"{tick[1]}.{tick[2]}ticks"
            if self.has(ticks_gid):
                return ticks_gid
        while "." in gid:
            gid = gid.rsplit(".", 1)[0]
            if self.has(gid):
                return gid
        return "figure"

    def walk(self, gid: str) -> Iterator[str]:
        seen: set[str] = set()
        node: str | None = gid
        while node and node not in seen:
            seen.add(node)
            yield node
            node = self.parent(node)


def structural_parent(manifest: dict, gid: str) -> str | None:
    """Resolve native explicit parent, tick container, or nearest gid ancestor."""
    return _Hierarchy(manifest).parent(gid)


def group_by_gid(manifest: dict, gid: str) -> dict | None:
    """Look up an explicitly selectable manifest group, not a fabricated element."""
    return next((g for g in manifest.get("groups") or () if g["gid"] == gid), None)


def describe_group(manifest: dict, gid: str) -> dict | None:
    """Read a group by explicit gid as the native single-selected group page would.

    ``members`` and ``resizable`` are unchanged engine metadata, not a promise
    of layout writes. ``selectable_member_gids`` is the native member list's
    selection target, with a colorbar axis routed to its colorbar when present.
    """
    group = group_by_gid(manifest, gid)
    if group is None:
        return None
    hierarchy = _Hierarchy(manifest)
    selectable = []
    for member_gid in group["members"]:
        element = hierarchy.elements.get(member_gid)
        if element is None:
            continue
        colorbar_gid = element.get("colorbar_gid") if element.get("is_colorbar") else None
        selectable.append(colorbar_gid if colorbar_gid in hierarchy.elements else member_gid)
    return {**group, "parent_gid": hierarchy.parent(gid), "selectable_member_gids": selectable}


def _geom_gid(element: dict) -> str:
    geom_gid = element.get("geom_gid")
    return element["gid"] if geom_gid is None else geom_gid


def _position_of(element: dict, overrides: Sequence[dict]) -> list | None:
    current = next(
        (entry.get("value") for entry in reversed(overrides)
         if entry.get("gid") == element["gid"] and entry.get("prop") == "position"),
        None,
    )
    if isinstance(current, list):
        return current[:4]
    default = next(
        (field.get("value") for field in element.get("editable", ()) if field.get("prop") == "position"),
        None,
    )
    return default[:4] if isinstance(default, list) else None


def _group_transform_blocked(
    group: dict, hierarchy: _Hierarchy, locked: set[str], overrides: Sequence[dict]
) -> str | None:
    if not group.get("resizable"):
        return "not_resizable"
    if locked.intersection((*group["members"], group["colorbar_gid"])):
        return "locked"
    for gid in group["members"]:
        member = hierarchy.elements.get(gid)
        # For group layout, upstream requires both isAlignable and resizable.
        # A resizable non-figure element is already alignable.
        if not member or gid == "figure" or not member.get("resizable"):
            return "incomplete"
        target = hierarchy.elements.get(member.get("geom_gid")) or member
        if _position_of(target, overrides) is None:
            return "incomplete"
    return None


def group_transform_blocked(
    manifest: dict, group_gid: str, locked_gids: Sequence[str], overrides: Sequence[dict] = ()
) -> str | None:
    """Native all-or-nothing layout eligibility (unknown group is incomplete).

    Uses the write target's last effective position override, falling back to
    the rendered editable position. Locks on the colorbar itself also block.
    A hidden member still participates. This function performs no layout writes.
    """
    group = group_by_gid(manifest, group_gid)
    if group is None:
        return "incomplete"
    return _group_transform_blocked(group, _Hierarchy(manifest), set(locked_gids), overrides)


def expand_groups(
    manifest: dict, gids: Sequence[str], locked_gids: Sequence[str], overrides: Sequence[dict] = ()
) -> list[str]:
    """Expand selected groups for layout, leaving candidate/visible selection unchanged.

    A selected group claims its members and their geometry aliases even when
    blocked, so they cannot be transformed separately. Unrelated selected
    elements are left alone for the caller's alignment/transform logic.
    """
    hierarchy = _Hierarchy(manifest)
    if not hierarchy.groups:
        return list(gids)
    selected_gids = set(gids)
    claimed = {
        member for gid, group in hierarchy.groups.items() if gid in selected_gids
        for member in group["members"]
    }
    locked = set(locked_gids)
    out: dict[str, None] = {}
    for gid in gids:
        group = hierarchy.groups.get(gid)
        if group is not None:
            if _group_transform_blocked(group, hierarchy, locked, overrides) is None:
                out.update(dict.fromkeys(group["members"]))
        else:
            element = hierarchy.elements.get(gid)
            if gid not in claimed and (element is None or _geom_gid(element) not in claimed):
                out[gid] = None
    return list(out)


def group_member_under(manifest: dict, group_gid: str, hit_gid: str) -> str | None:
    """Return the selected group's member geometry underneath a hit (native entryUnder)."""
    group = group_by_gid(manifest, group_gid)
    if group is None:
        return None
    hierarchy = _Hierarchy(manifest)
    if hit_gid not in hierarchy.elements:
        return None
    members = set(group["members"])
    for gid in hierarchy.walk(hit_gid):
        element = hierarchy.elements.get(gid)
        if element is not None:
            key = _geom_gid(element)
            if key in members:
                return key
    return None


def pick(
    manifest: dict, x: float, y: float, locked_gids: list[str], current_gid: str | None = None
) -> dict:
    """Select at a figure fraction and return the unchanged, ordered candidate stack.

    A supplied ``current_gid`` advances the pointer cycle or starts at its first
    hit when current isn't in the stack. With no hits, use native plain-click
    fallback (figure, if present). Groups can be selected by explicit gid via
    ``describe_group``. A member click still picks the element, never its group.
    """
    candidates = _pick_element_stack(manifest, x, y, locked_gids)
    hierarchy = _Hierarchy(manifest)
    if candidates:
        at = candidates.index(current_gid) if current_gid in candidates else -1
        selected = candidates[(at + 1) % len(candidates)]
    else:
        selected = "figure" if "figure" in hierarchy.elements else None
    ancestry = list(hierarchy.walk(selected))[1:][::-1] if selected is not None else []
    return {
        "picked_gid": selected,
        "candidate_gids": candidates,
        "parent_gid": ancestry[-1] if ancestry else None,
        "ancestor_gids": ancestry,
        "group_gids": [gid for gid in ancestry if gid in hierarchy.groups],
    }
