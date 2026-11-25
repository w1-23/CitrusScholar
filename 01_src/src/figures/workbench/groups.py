"""Project-owned presentation groups derived from the saved renderer inventory."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math

from citrus_figures.manifest import EDITABLE_BY_ROLE
from citrus_figures.workbench_styles import properties as workbench_properties
from src.figures.projects import ProjectError
from src.figures.workbench.identities import DeclarativeIdentity, verified_panels

_TEXT = {"color", "fontsize", "fontfamily", "fontweight", "fontstyle", "horizontalalignment", "visible"}
_SERIES = {"color", "linewidth", "linestyle", "marker", "markersize", "markerfacecolor", "markeredgecolor",
           "markeredgewidth", "facecolor", "edgecolor", "alpha", "visible", "label", "cmap"}
_ALLOWED = {
    "figure": {"transparent"}, "series": _SERIES, "legend": _TEXT | {"frameon", "ncols", "loc", "offset_px"},
    "title": _TEXT | {"text", "offset_px"}, "xlabel": _TEXT | {"text", "offset_px"},
    "ylabel": _TEXT | {"text", "offset_px"}, "annot": _TEXT | {"text", "offset_px"},
    "axes": {"offset_px"}, "ticklabel": _TEXT - {"horizontalalignment"},
    "grid": set(EDITABLE_BY_ROLE["grid"]), "spine": set(EDITABLE_BY_ROLE["spine"]),
    "errorbar": set(EDITABLE_BY_ROLE["errorbar"]), "colorbar": {"label", "offset_px", "visible"},
}
_LABELS = {"title": "标题", "xlabel": "X 轴标题", "ylabel": "Y 轴标题", "legend": "图例",
           "annot": "注释", "grid": "网格线", "spine": "坐标轴线", "errorbar": "误差棒", "colorbar": "色标"}
_ARTISTS = {"Line2D": "曲线", "Rectangle": "柱形", "PathCollection": "散点", "PolyCollection": "填充带",
            "FillBetweenPolyCollection": "填充带", "QuadMesh": "热图", "AxesImage": "热图"}
_MISSING = object()
_OBSERVED_STYLES = {"alpha", "cmap", "color", "facecolor", "edgecolor", "fontfamily", "fontsize", "fontstyle",
                    "fontweight", "frameon", "label", "linestyle", "loc", "marker", "markerfacecolor", "markeredgecolor",
                    "markeredgewidth", "markersize", "ncols", "text", "transparent", "visible"}


def invalid(code, message, status=400):
    return ProjectError(code, message, status=status)


def _group_id(figure_id, key):
    return "grp_" + sha256(json.dumps([figure_id, key], ensure_ascii=True).encode()).hexdigest()[:32]


def _identity(item):
    return (item["role"], item.get("artist"), item.get("locator"), item.get("semantic_key"))


def _value(item, prop):
    if prop == "markerfacecolor":
        mode = item.get("marker_fill_mode")
        if mode is None and item.get("markerface_auto") is True:
            mode = "auto"
        if mode in {"auto", "none"}:
            return mode
    return item.get(prop, _MISSING)


@dataclass
class Group:
    id: str
    kind: str
    label: str
    parent: str | None
    rows: list[dict] = field(default_factory=list)
    hits: list[str] = field(default_factory=list)
    props: set[str] = field(default_factory=set)
    reason: str = ""
    overrides: dict = field(default_factory=dict)

    def public(self):
        values = {}
        for prop in self.props:
            options = [self.overrides.get((row["gid"], prop), _value(row, prop)) for row in self.rows]
            if options and options[0] is not _MISSING and all(x == options[0] for x in options):
                values[prop] = options[0]
        return {"id": self.id, "kind": self.kind, "label": self.label, "parent": self.parent,
                "pick_ids": list(dict.fromkeys(self.hits)), "properties": sorted(self.props),
                "values": values, "reason": self.reason,
                "line_marker_fill": bool(self.rows and all(row.get("artist") == "Line2D" for row in self.rows))}


class Inventory:
    """One revision's groups and the closed translation into renderer overrides."""

    def __init__(self, project, manifest, *, disabled="", frozen=None):
        self.figure_id = project["figure_id"]
        self.source_kind = project.get("source_kind")
        self.groups = {}
        self.disabled = disabled
        self.rows = {}
        self.saved = {}
        self.declarative = DeclarativeIdentity(frozen) if self.source_kind == "spec" else None
        self._build(manifest, project.get("overrides_json") or [])

    def _add(self, key, kind, label, parent=None, rows=()):
        gid = _group_id(self.figure_id, key)
        group = Group(gid, kind, label, parent.id if parent else None, list(rows),
                      [row["gid"] for row in rows])
        if rows:
            group.props = set.intersection(*((set(row.get("editable") or ()) | workbench_properties(row)) & _ALLOWED.get(row["role"], set())
                                             for row in rows))
        group.overrides = {(row["gid"], "offset_px"): self.saved[(row["gid"], "offset_px")]
                           for row in rows if (row["gid"], "offset_px") in self.saved}
        self.groups[gid] = group
        return group

    def _build(self, manifest, saved):
        rows = manifest.get("objects", [])
        if (not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("gid"), str)
                or not row["gid"] or not isinstance(row.get("role"), str)
                or not isinstance(row.get("locator"), dict)
                or not isinstance(row.get("editable"), list)
                or any(not isinstance(prop, str) for prop in row["editable"]) for row in rows)):
            raise invalid("invalid_inventory", "已保存的图件对象清单不完整。", 409)
        self.rows = {row["gid"]: row for row in rows}
        if len(self.rows) != len(rows):
            raise invalid("invalid_inventory", "已保存的图件对象地址存在歧义。", 409)
        if not isinstance(saved, list) or any(not isinstance(edit, dict) or set(edit) != {"gid", "prop", "value"}
                or not isinstance(edit["gid"], str) or not isinstance(edit["prop"], str) for edit in saved):
            raise invalid("invalid_inventory", "已保存的图件样式记录不完整。", 409)
        self.saved = {(edit["gid"], edit["prop"]): edit["value"] for edit in saved}
        axes = {row["locator"].get("axes_index"): row for row in rows if row["role"] == "axes"
                and type(row["locator"].get("axes_index")) is int and row["locator"]["axes_index"] >= 0}
        if len(axes) != sum(row["role"] == "axes" for row in rows):
            raise invalid("invalid_inventory", "已保存的子图标识存在歧义。", 409)
        figure_rows = [row for row in rows if row["role"] == "figure"]
        if self.source_kind == "spec" and not figure_rows:
            # Earlier declarative manifests stored a root with a blank role.
            # Only upgrade that renderer's exact known figure locator.
            root_artist = self.rows.get("figure_1", {})
            if (root_artist.get("artist") == "Figure" and root_artist.get("role") == ""
                    and root_artist.get("locator") == {"scope": "figure"}):
                figure_rows = [{**root_artist, "role": "figure", "locator": {"scope": "figure", "role": "figure"}}]
        if len(figure_rows) > 1:
            raise invalid("invalid_inventory", "已保存的画布标识存在歧义。", 409)
        # A prior declarative renderer may predate the transparent background
        # control. Its known figure artist and verified source are enough.
        if figure_rows and self.source_kind == "spec" and figure_rows[0]["locator"].get("scope") == "figure":
            row = figure_rows[0]
            figure_rows = [{**row, "editable": [*row["editable"], "transparent"],
                            "transparent": row.get("transparent", False)}]
        root = self._add(("figure",), "figure", "整张图", rows=figure_rows)
        root.hits.clear()
        panel_ids = verified_panels(axes, self.declarative)
        panel_keys = {index: ("verified", panel_ids[index]) if index in panel_ids else ("unverified", index)
                      for index in axes}
        panels = {}
        for index, row in sorted(axes.items()):
            panel = self._add(("panel", panel_keys[index]), "panel", f"子图 {index + 1}", root, [row])
            if index not in panel_ids:
                panel.props.clear()
                panel.reason = "此子图没有唯一的已保存标识。"
            panel.hits.clear()
            panels[index] = panel

        targets, ticks, series_by_gid = {}, defaultdict(list), {}
        for row in rows:
            role, locator = row["role"], row["locator"]
            index = locator.get("axes_index")
            panel = panels.get(index) if type(index) is int else None
            if role in {"axes", "figure", "point", "bar"}:
                continue
            if role == "ticklabel":
                if panel and locator.get("axis") in ("x", "y"):
                    if row.get("bbox") and row.get("visible", True):
                        ticks[index, locator["axis"]].append(row)
                continue
            if panel is None and locator.get("scope") != "figure":
                continue
            if role == "series":
                source_key = self.declarative.series_key(index, row) if self.declarative is not None else (
                    row.get("semantic_key") if row.get("identity_scope") == "semantic" else None)
                key = (panel_keys.get(index), "series", source_key if source_key else ("read-only", row["gid"]))
                label = row.get("label") or _ARTISTS.get(row.get("artist"), "未命名系列")
            elif role in _ALLOWED:
                key = (panel_keys.get(index), role, row["gid"])
                label = _LABELS.get(role, role)
                if role == "grid" and locator.get("axis") in ("x", "y"):
                    label = locator["axis"].upper() + " " + label + ("（次刻度）" if locator.get("which") == "minor" else "")
                if role == "spine" and locator.get("spine") in ("left", "right", "top", "bottom"):
                    label += "（" + {"left": "左", "right": "右", "top": "上", "bottom": "下"}[locator["spine"]] + "）"
            else:
                continue
            if key in targets:
                raise invalid("invalid_inventory", "已保存的图件对象组不唯一。", 409)
            targets[key] = row
            parent = series_by_gid.get(row.get("parent_gid"), panel or root) if role == "errorbar" else panel or root
            group = self._add(key, role, label, parent, [row])
            if panel and index not in panel_ids:
                group.props.clear()
                group.reason = "此子图没有唯一的已保存标识。"
            if role == "series":
                series_by_gid[row["gid"]] = group
                if not source_key:
                    group.props.clear()
                    group.reason = group.reason or "此系列没有可验证的来源标识。"
                if not group.props and not group.reason:
                    group.reason = "渲染器未声明此系列可用的样式属性。"
            elif role in {"title", "xlabel", "ylabel"} and not row.get("text") and not row.get("bbox"):
                group.hits.clear()
        for row in rows:
            parent = series_by_gid.get(row.get("parent_gid"))
            if row["role"] in {"bar", "point"} and parent:
                parent.hits.append(row["gid"])
                blocked = {prop for gid, prop in self.saved if gid == row["gid"]}
                if blocked:
                    parent.props -= blocked | ({"color", "markerfacecolor", "markeredgecolor"}
                                                if blocked & _SERIES else set())
                    parent.reason = "此系列已有单点或单柱样式，相关组属性不可覆盖。"
        for (index, axis), members in sorted(ticks.items()):
            group = self._add((panel_keys[index], "ticks", axis), "ticks", f"{axis.upper()} 轴刻度文字", panels[index], members)
            if index not in panel_ids:
                group.props.clear()
                group.reason = "此子图没有唯一的已保存标识。"
            elif len(members) > 200:
                group.props.clear()
                group.reason = "刻度成员数超出一次渲染的样式限额。"
        self._add_shared(root, series_by_gid, manifest.get("shared_legend_groups", {}))
        for group in self.groups.values():
            if self.disabled:
                group.props.clear()

    def _add_shared(self, root, series_by_gid, relations):
        if not isinstance(relations, dict):
            raise invalid("invalid_inventory", "共享图例关系无法验证。", 409)
        for members in relations.values():
            if not isinstance(members, list) or any(not isinstance(gid, str) for gid in members):
                raise invalid("invalid_inventory", "共享图例关系无法验证。", 409)
            if len(set(members)) < 2:
                continue
            linked = [series_by_gid[gid] for gid in members if gid in series_by_gid]
            if len(linked) != len(members) or len(set(members)) != len(members):
                raise invalid("invalid_inventory", "共享图例关系无法验证。", 409)
            common = set.intersection(*(group.props for group in linked))
            if common:
                label = linked[0].label
                key = ("shared_legend", *sorted(group.id for group in linked))
                sync = self._add(key, "sync", f"同步共享图例 · {label}", root,
                                 [group.rows[0] for group in linked])
                sync.props &= common
                sync.hits.clear()
            for group in linked:
                group.props.clear()
                group.reason = "此系列与其他子图共用一项图例。可以选择对应的同步组。" if common else "共享图例系列缺少可验证的共同样式。"
            for group in self.groups.values():
                if group.kind == "errorbar" and group.parent in {item.id for item in linked}:
                    group.props.clear()
                    group.reason = "此误差棒由共享图例关联。"

    def public(self):
        return [group.public() for group in self.groups.values()]

    def translate(self, commands):
        if self.disabled:
            raise invalid("workbench_read_only", self.disabled, 409)
        raw, expected = [], {}
        for command in commands:
            group = self.groups.get(command["group_id"])
            prop = command["property"]
            if not group or prop not in group.props:
                raise invalid("group_property_unavailable", "此版本的所选对象组不支持该属性。", 422)
            for row in group.rows:
                key = (row["gid"], prop)
                if key in expected:
                    raise invalid("duplicate_group_property", "同一组成员的属性只能提交一次。", 422)
                expected[key] = row
                raw.append({"gid": row["gid"], "prop": prop, "value": command["value"]})
        if len(raw) > 200:
            raise invalid("group_batch_too_large", "这批修改超出 200 项成员样式上限。", 422)
        return raw, expected

    def confirm(self, expected, manifest, accepted):
        actual = {row.get("gid"): row for row in manifest.get("objects", []) if isinstance(row, dict)}
        if len(actual) != len(manifest.get("objects", [])):
            raise invalid("group_members_changed", "重绘返回的对象地址不唯一。", 409)
        values = {(entry["gid"], entry["prop"]): entry["value"] for entry in accepted}
        if not expected.keys() <= values.keys():
            raise invalid("group_edit_unconfirmed", "渲染器未确认全部组样式，修改未保存。", 409)
        for gid, prop in expected:
            before, after = expected[gid, prop], actual.get(gid)
            if after is None or (_identity(before) != _identity(after) and not (
                    self.declarative and self.declarative.confirmation(before, after))):
                raise invalid("group_members_changed", "重绘时所选组成员发生变化。", 409)
            if prop not in (set(actual[gid].get("editable", [])) | workbench_properties(actual[gid])):
                raise invalid("group_property_unavailable", "重绘未确认所选组的样式。", 409)
            if prop not in _OBSERVED_STYLES:
                continue
            observed = _value(actual[gid], prop)
            wanted = values[gid, prop]
            if observed is _MISSING:
                continue  # Some artists cannot report a single effective value.
            if type(observed) in (int, float) and type(wanted) in (int, float):
                matches = math.isclose(observed, wanted, abs_tol=1e-6)
            else:
                matches = observed == wanted
            if not matches:
                raise invalid("group_edit_unconfirmed", "重绘结果与提交的组样式不一致，修改未保存。", 409)
