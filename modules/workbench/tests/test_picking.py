# SPDX-License-Identifier: AGPL-3.0-only
"""Native manifest vectors; see Tavotto canvas/pickStrokeLayers.test.ts,
canvas/reviewPickParity.test.ts and canvas/sharedColorbarGroup.test.ts.
"""

import json
from pathlib import Path

import pytest

from wb.picking import (
    describe_group,
    expand_groups,
    group_by_gid,
    group_member_under,
    group_transform_blocked,
    pick,
    structural_parent,
)


def element(gid, role, bbox=(0, 0, 1, 1), **fields):
    return {"gid": gid, "role": role, "bbox": bbox, "editable": [], **fields}


def path(points, closed=False):
    return {"points": points, "closed": closed}


def rect_ring(left, top, right, bottom):
    return [(left, top), (right, top), (right, bottom), (left, bottom)]


def curve(gid, a=(0.1, 0.5), b=(0.9, 0.5), **geometry):
    return element(gid, "line", geometry={
        "kind": "polyline", "fill": False, "stroke": True, "stroke_pt": 1,
        "paths": [path([a, b])], **geometry,
    })


def fill(gid="fill", **geometry):
    return element(gid, "patch", geometry={
        "kind": "path", "fill": True, "stroke": False,
        "paths": [path(rect_ring(0.1, 0.1, 0.9, 0.9), closed=True)], **geometry,
    })


def manifest(*elements, size=(100, 100)):
    return {"size_mm": size, "elements": [element("figure", "figure"), *elements]}


def at(man, x=0.5, y=0.5, locked=(), current=None):
    return pick(man, x, y, list(locked), current)["candidate_gids"]


@pytest.mark.parametrize(("alpha", "expected"), [
    (0.49, ["line", "fill", "axes_0"]),
    (0.5, ["fill", "axes_0", "line"]),
])
def test_opaque_fill_threshold_and_covered_candidates(alpha, expected):
    m = manifest(element("axes_0", "axes"), curve("line", paint_order=2),
                 fill(fill_alpha=alpha, paint_order=5))
    assert at(m) == expected


def test_locked_fill_still_covers_hidden_fill_does_not_and_unknown_order_does_not_demote():
    axes = element("axes_0", "axes")
    line = curve("line", paint_order=2)
    opaque = fill(fill_alpha=0.9, paint_order=5)
    assert at(manifest(axes, line, opaque), locked=("fill",)) == ["axes_0", "line"]
    opaque["editable"] = [{"prop": "visible", "value": False}]
    assert at(manifest(axes, line, opaque)) == ["line", "axes_0"]
    opaque["editable"] = [{"prop": "visible", "value": 0}]
    assert at(manifest(axes, curve("line"), opaque)) == ["line", "fill", "axes_0"]


def test_stroke_bucket_then_ink_area_then_paint_order_then_registration():
    long = curve("long", (0.05, 0.5), (0.95, 0.5), paint_order=9)
    grid = curve("grid", (0.48, 0.514), (0.52, 0.514), paint_order=1)
    assert at(manifest(grid, long)) == ["long", "grid"]
    assert at(manifest(grid, long), y=0.514) == ["grid", "long"]
    small = curve("small", (0.45, 0.5), (0.55, 0.5), paint_order=1)
    a = curve("a", paint_order=3)
    b = curve("b", paint_order=5)
    c = curve("c", paint_order=5)
    assert at(manifest(long, a, c, b, small)) == ["small", "c", "b", "a", "long"]


def test_stroke_width_clip_anisotropy_and_arrow_endpoint_priority():
    diagonal = curve("diagonal", (0.1, 0.1), (0.9, 0.9))
    arrow = element("arrow", "arrow_patch", arrow_endpoints=[(0.1, 0.1), (0.9, 0.9)],
                    geometry=fill()["geometry"], paint_order=1)
    assert at(manifest(diagonal, arrow), 0.12, 0.8) == []
    assert at(manifest(diagonal, arrow)) == ["arrow", "diagonal"]
    horizontal = curve("h", stroke_pt=12, clip=(0.2, 0.2, 0.6, 0.6))
    m = manifest(horizontal, size=(120, 40))
    assert at(m, 0.5, 0.55) == ["h"]  # 2 mm off center, still on the 12pt stroke.
    assert at(m, 0.5, 0.56) == []
    assert at(m, 0.199, 0.5) == []  # On the path, but outside its clip.
    point_arrow = element("p", "arrow_patch", arrow_endpoints=[(0.5, 0.5), (0.5, 0.5)])
    assert at(manifest(point_arrow)) == ["p"]


def test_winding_holes_implicit_closed_edge_fill_tolerance_and_paint_layer():
    outer = rect_ring(0.1, 0.1, 0.9, 0.9)
    inner = rect_ring(0.3, 0.3, 0.7, 0.7)
    solid = fill("solid", paths=[path(outer), path(inner)])
    hole = fill("hole", paths=[path(outer), path(inner[::-1])])
    assert at(manifest(solid, hole)) == ["solid"]
    assert at(manifest(hole), 0.095, 0.5) == ["hole"]  # Implicit closing edge.
    assert at(manifest(hole), 0.08, 0.5) == []
    edge_line = curve("edge", (0.095, 0.2), (0.095, 0.8))
    assert at(manifest(hole, edge_line), 0.095, 0.5) == ["edge", "hole"]
    edged = fill("edged", paths=[path(outer)], stroke=True, stroke_pt=2)
    smaller = fill("smaller", paths=[path(rect_ring(0.05, 0.4, 0.2, 0.6))])
    assert at(manifest(smaller, edged), 0.1, 0.5) == ["edged", "smaller"]


def test_known_order_text_tier_and_bbox_role_penalty():
    axes = element("axes_0", "axes", (0.45, 0.45, 0.1, 0.1))
    tick = element("tick", "ticklabel", (0.48, 0.48, 0.04, 0.04))
    text = element("text", "title", (0.4, 0.45, 0.2, 0.1), paint_order=1)
    assert at(manifest(axes, tick, text, curve("line"))) == ["text", "line", "tick", "axes_0"]
    opaque = fill(fill_alpha=1, paint_order=2)
    arrow = element("arrow", "arrow_patch", arrow_endpoints=[(0.1, 0.5), (0.9, 0.5)], paint_order=1)
    assert at(manifest(axes, opaque, arrow, text)) == ["axes_0", "fill", "text", "arrow"]
    text.pop("paint_order")
    assert at(manifest(text, curve("line"))) == ["line", "text"]


def test_native_bbox_padding_figure_fallback_and_overlap_cycle():
    host = element("axes_0", "axes", (0.2, 0.2, 0.6, 0.6))
    twin = element("axes_1", "axes", host["bbox"])
    m = manifest(host, twin)
    assert at(m, 0.197, 0.197) == ["axes_0", "axes_1"]
    assert at(m, 0.195, 0.195) == []
    assert pick(m, 0.5, 0.5, [])["picked_gid"] == "axes_0"
    assert pick(m, 0.5, 0.5, [], "axes_0")["picked_gid"] == "axes_1"
    assert pick(m, 0.5, 0.5, [], "axes_1")["picked_gid"] == "axes_0"
    assert pick(m, 0.5, 0.5, [], "unrelated")["picked_gid"] == "axes_0"
    assert pick(m, 0.5, 0.5, ["axes_0", "axes_1", "figure"])["picked_gid"] == "figure"
    assert pick({"elements": []}, 0.5, 0.5, [])["picked_gid"] is None


def test_native_group_is_structural_not_a_canvas_candidate():
    group_gid = "group:axes_2"
    m = manifest(
        element("axes_0", "axes", parent_gid=group_gid),
        element("axes_0.xticks", "ticks"),
        element("axes_0.xticklabels_3", "ticklabel", parent_gid="unavailable"),
        element("axes_1", "axes", parent_gid=group_gid),
        element("axes_2", "axes", parent_gid=group_gid, is_colorbar=True, colorbar_gid="axes_2.colorbar"),
        element("axes_2.colorbar", "colorbar", geom_gid="axes_2"),
        element("axes_3.image", "image", geom_gid="axes_1"),
        element("axes_3", "axes"),
        curve("axes_0.nested.line_0"),
    )
    group = {"gid": group_gid, "kind": "shared_colorbar", "members": ["axes_0", "axes_1", "axes_2"],
             "subplot_gids": ["axes_0", "axes_1"], "colorbar_gid": "axes_2.colorbar",
             "mappable_gid": "axes_3.image", "bbox": [0, 0, 1, 1], "resizable": False}
    m["groups"] = [group]
    result = pick(m, 0.5, 0.5, [], group_gid)
    assert result["picked_gid"] == "axes_0.nested.line_0"
    assert group_gid not in result["candidate_gids"]
    assert result["parent_gid"] == "axes_0"
    assert result["ancestor_gids"] == ["figure", group_gid, "axes_0"]
    assert result["group_gids"] == [group_gid]
    assert group_by_gid(m, group_gid) == group
    assert describe_group(m, group_gid) == {
        **group, "parent_gid": "figure", "selectable_member_gids": ["axes_0", "axes_1", "axes_2.colorbar"],
    }
    assert describe_group(m, "group:missing") is None
    assert structural_parent(m, "axes_0.xticklabels_3") == "axes_0.xticks"
    assert structural_parent(m, group_gid) == "figure"
    assert structural_parent(m, "figure") is None
    assert group_member_under(m, group_gid, "axes_0.nested.line_0") == "axes_0"
    assert group_member_under(m, group_gid, "axes_2.colorbar") == "axes_2"
    assert group_member_under(m, group_gid, "axes_3.image") == "axes_1"
    assert group_member_under(m, group_gid, "axes_3") is None


def test_explicit_parents_override_gid_path_and_cycle_terminates():
    left = element("axes_0", "axes", parent_gid="other")
    right = element("other", "text", parent_gid="axes_0")
    m = manifest(left, right, curve("axes_0.line_0"))
    assert pick(m, 0.5, 0.5, [])["ancestor_gids"] == ["other", "axes_0"]
    right["parent_gid"] = "other"
    assert structural_parent(m, "other") == "figure"


def test_group_transform_eligibility_and_layout_targets_are_all_or_nothing():
    gid = "group:cax"
    group = {
        "gid": gid, "kind": "shared_colorbar", "members": ["axes_0", "axes_1", "cax"],
        "colorbar_gid": "cax.colorbar", "resizable": True,
    }
    position = {"prop": "position", "value": [0.1, 0.2, 0.3, 0.4]}
    axes_0 = element("axes_0", "axes", resizable=True)
    axes_0["editable"] = [position]
    axes_1 = element("axes_1", "axes", geom_gid="proxy", resizable=True)
    cax = element("cax", "axes", resizable=True)
    cax["editable"] = [position, {"prop": "visible", "value": False}]
    m = manifest(
        axes_0, axes_1, cax, element("proxy", "axes"),
        element("axes_0.image", "image", geom_gid="axes_0"),
        element("axes_0.title", "title"), element("cax.colorbar", "colorbar", geom_gid="cax"),
    )
    m["groups"] = [group]
    selection = ["axes_1", "axes_0.image", "axes_0.title", gid, "cax.colorbar", "axes_0.title"]
    assert group_transform_blocked(m, gid, []) == "incomplete"
    override = {"gid": "proxy", **position}
    overrides = [{**override, "value": "invalid"}, override]
    assert group_transform_blocked(m, gid, [], overrides) is None
    assert expand_groups(m, selection, [], overrides) == ["axes_0.title", "axes_0", "axes_1", "cax"]
    assert group_transform_blocked(m, gid, [], overrides[::-1]) == "incomplete"
    for member in ["axes_0", "cax.colorbar"]:
        assert group_transform_blocked(m, gid, [member], overrides) == "locked"
        assert expand_groups(m, selection, [member], overrides) == ["axes_0.title"]
    group["resizable"] = False
    assert group_transform_blocked(m, gid, ["axes_0"], overrides) == "not_resizable"
    assert expand_groups(m, selection, [], overrides) == ["axes_0.title"]


def test_pinned_upstream_real_matplotlib_fixture():
    fixture = (
        Path(__file__).resolve().parents[1] / ".workbench-dev/tavotto/web/src/canvas"
        / "__fixtures__/review-pick-fixture.json"
    )
    if not fixture.is_file():
        pytest.skip("Optional real-engine fixture from the pinned Tavotto source tree")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for probe in data["probes"]:
        stack = at(data["manifest"], *probe["pt"])
        assert (stack[0] if stack else None) == probe["expect"], probe["name"]
