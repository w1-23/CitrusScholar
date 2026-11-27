"""Replay evidence and style capabilities for separately rendered figures."""
from __future__ import annotations

from datetime import date
import hashlib
import re

from .frozen_render import typed_payload_sha256


def _coordinates(value):
    import numpy as np

    if value is None or isinstance(value, str):
        return value
    if isinstance(value, date):
        return [type(value).__name__, value.isoformat()]
    if isinstance(value, (list, tuple)):
        return [_coordinates(item) for item in value]
    if type(value) is int:
        return {"int": str(value)}
    values = np.ma.asarray(value)
    raw = np.ma.getdata(values)
    dtype = raw.dtype.newbyteorder("<")
    if dtype.kind == "O":
        return {"shape": list(raw.shape), "objects": [
            [type(item).__name__, _coordinates(item) if item is None or isinstance(
                item, (str, date, int, float, np.generic)) else None] for item in raw.flat]}
    if dtype.kind == "f" and dtype.itemsize > 8:
        data = [np.format_float_scientific(item, unique=True) for item in raw.flat]
    else:
        data = hashlib.sha256(raw.astype(dtype, copy=False).tobytes()).hexdigest()
    return {"dtype": dtype.str, "shape": list(raw.shape), "data": data,
            "mask": hashlib.sha256(np.ma.getmaskarray(values).tobytes()).hexdigest()}


def _path_coordinates(path):
    return [_coordinates(path.vertices), _coordinates(path.codes)]


def _artist_coordinates(artist):
    from matplotlib.collections import Collection, PathCollection, QuadMesh
    from matplotlib.image import AxesImage
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch, Rectangle
    from matplotlib.text import Annotation, Text
    from mpl_toolkits.mplot3d.art3d import Line3D

    if isinstance(artist, Line3D):
        return _coordinates(artist.get_data_3d())
    if isinstance(artist, Line2D):
        return _coordinates(artist.get_data(orig=True))
    if type(artist) in (Text, Annotation):
        return _coordinates([artist.get_position(), artist.xy if type(artist) is Annotation else None])
    if isinstance(artist, Rectangle):
        return _coordinates([artist.get_xy(), artist.get_width(), artist.get_height(), artist.angle])
    if isinstance(artist, Patch):
        return _path_coordinates(artist.get_path().transformed(artist.get_patch_transform()))
    if isinstance(artist, AxesImage):
        return _coordinates([artist.get_extent(), artist.get_array(), artist.get_clim()])
    if isinstance(artist, Collection):
        payload = _coordinates([artist.get_offsets(), artist.get_array(), artist.get_clim()])
        if isinstance(artist, QuadMesh):
            payload.append(_coordinates(artist.get_coordinates()))
        elif not isinstance(artist, PathCollection):
            payload.append([_path_coordinates(path) for path in artist.get_paths()])
        return payload
    return None


def coordinate_sha256(figure):
    def inventory(axes):
        marks = [*axes.lines, *axes.collections, *axes.patches, *axes.images, *axes.texts, *axes.artists]
        return {"x": [axes.get_xscale(), _coordinates(axes.get_xlim())],
                "y": [axes.get_yscale(), _coordinates(axes.get_ylim())],
                "z": _coordinates(axes.get_zlim()) if hasattr(axes, "get_zlim") else None,
                "marks": [[type(mark).__name__, _artist_coordinates(mark)] for mark in marks],
                "children": [inventory(child) for child in axes.child_axes]}

    return typed_payload_sha256({"size": _coordinates(figure.get_size_inches()),
                                "axes": [inventory(axes) for axes in figure.axes],
                                "marks": [[type(mark).__name__, _artist_coordinates(mark)]
                                          for mark in [*figure.lines, *figure.patches, *figure.images,
                                                       *figure.texts, *figure.artists]]})


def replay_baseline(figure, objects=()):
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    canvas = figure.canvas
    if (type(figure) is not Figure or type(canvas) is not FigureCanvasAgg
            or "draw" in vars(figure) or "draw" in vars(canvas)
            or canvas.callbacks.callbacks.get("draw_event")
            or any("draw" in vars(axes) for axes in figure.axes)):
        return {"version": 1, "unavailable": "dynamic_renderer"}
    if figure.bbox.width * figure.bbox.height > 16_000_000:
        return {"version": 1, "unavailable": "replay_canvas_too_large"}
    try:
        canvas.draw()
        rgba = canvas.buffer_rgba()
        pixels = typed_payload_sha256({"shape": list(rgba.shape), "rgba": hashlib.sha256(rgba).hexdigest()})
        coordinates = coordinate_sha256(figure)
        from .svg_projection import MARK_ROLES
        # Preserve the v1 receipt for older frozen projects. Independent marks
        # are additionally bound to the actual SVG and their versioned locator
        # by the workbench; baseline pixels and coordinates still include them.
        targets = [{key: item.get(key) for key in ("gid", "role", "artist", "label", "semantic_key", "locator", "editable")}
                   for item in objects if item.get("role") not in MARK_ROLES]
    except Exception:
        return {"version": 1, "unavailable": "replay_observation_failed"}
    return {"version": 1, "baseline_sha256": pixels, "coordinates_sha256": coordinates,
            "targets_sha256": typed_payload_sha256({"objects": targets})}


def valid_baseline(value):
    hashes = ("baseline_sha256", "coordinates_sha256", "targets_sha256")
    return (isinstance(value, dict) and set(value) == {"version", *hashes} and type(value["version"]) is int
            and value["version"] == 1
            and all(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{64}", value[key]) for key in hashes))


REPLAY_UNAVAILABLE = frozenset({"dynamic_renderer", "replay_canvas_too_large", "replay_observation_failed"})
REPLAY_ERRORS = {
    "renderer_changed": "独立渲染脚本的摘要与冻结记录不一致。",
    "frozen_data_changed": "冻结表的摘要与渲染回执不一致。",
    "renderer_output_changed": "相同渲染脚本和冻结表未重现原图。",
    "renderer_targets_changed": "独立渲染未重现原图件的样式编辑对象。",
    "render_data_changed": "独立渲染未重现冻结图件的数值坐标。",
    "override_data_changed": "样式修改或导出改变了数值坐标，修改未保存。",
    "dynamic_renderer": "绘图使用自定义绘制过程或回调，无法验证样式重放。",
    "replay_canvas_too_large": "画布超出样式重放校验的尺寸上限。",
    "replay_observation_failed": "工作器无法读取绘图输出或数值坐标以验证重放。",
    "replay_evidence_unavailable": "当前工作器未提供可验证的样式重放证据。",
    "legacy_replay_evidence_unavailable": "旧图件缺少可验证的重放证据，请用独立渲染脚本和冻结表重新生成。",
}


def replay_mismatch(expected, actual):
    if not isinstance(actual, dict):
        return "replay_evidence_unavailable"
    if expected.get("renderer_sha256") != actual.get("renderer_sha256"):
        return "renderer_changed"
    if expected.get("data_sha256") != actual.get("data_sha256"):
        return "frozen_data_changed"
    baseline = actual.get("style_replay")
    if not valid_baseline(baseline):
        reason = baseline.get("unavailable") if isinstance(baseline, dict) else None
        return reason if isinstance(reason, str) and reason in REPLAY_UNAVAILABLE else "replay_evidence_unavailable"
    prior = expected.get("style_replay")
    if prior is None:
        if expected.get("geometry_sha256") != actual.get("geometry_sha256"):
            return "render_data_changed"
    elif prior["coordinates_sha256"] != baseline["coordinates_sha256"]:
        return "render_data_changed"
    elif prior["baseline_sha256"] != baseline["baseline_sha256"]:
        return "renderer_output_changed"
    elif prior["targets_sha256"] != baseline["targets_sha256"]:
        return "renderer_targets_changed"
    return None


def style_capabilities(artist, role, members, properties):
    from matplotlib.axes import Axes
    from matplotlib.collections import LineCollection, PathCollection, PolyCollection
    from matplotlib.legend import Legend
    from matplotlib.lines import Line2D
    from matplotlib.patches import Arrow, Circle, Ellipse, FancyArrow, FancyArrowPatch, PathPatch, Polygon, Rectangle, Wedge
    from matplotlib.spines import Spine
    from matplotlib.text import Annotation, Text
    from .core import ScatterPoint

    if not properties:
        return properties, None
    edited = [member.collection if type(member) is ScatterPoint else member for member in members]
    allowed = {
        "axes": (Axes,), "colorbar": (Axes,), "legend": (Legend,), "spine": (Spine,),
        "annot": (Text, Annotation), "title": (Text,), "xlabel": (Text,), "ylabel": (Text,),
        "ticklabel": (Text,), "grid": (Line2D,), "errorbar": (Line2D, LineCollection),
        "series": (Line2D, LineCollection, PathCollection, PolyCollection, Rectangle),
        "point": (PathCollection,), "bar": (Rectangle,),
        "patch": (Circle, Ellipse, PathPatch, Polygon, Rectangle, Wedge),
        "arrow": (Arrow, FancyArrow, FancyArrowPatch),
    }
    if any(type(member) not in allowed.get(role, ()) for member in edited):
        return [], "unsupported_artist_type"
    if any(member.get_animated() or member.get_agg_filter() is not None
           or any(name == "draw" or name.startswith("set_") for name in vars(member))
           for member in edited):
        return [], "dynamic_artist"
    excluded = set()
    reason = None
    if role == "annot" and type(artist) is Annotation:
        excluded = {"offset_px"}
        reason = "annotation_anchor"
    if role in {"axes", "colorbar"} and artist.get_axes_locator() is not None:
        excluded = {"offset_px"}
        reason = "managed_axes_position"
    if role in {"series", "point"} and any(isinstance(member, (PathCollection, PolyCollection, LineCollection))
                                              and member.get_array() is not None for member in edited):
        excluded = {"color", "alpha", "marker", "markerfacecolor", "markeredgecolor"}
        if role == "point":
            excluded.add("visible")
        reason = "scalar_mapped_colors"
    filtered = [prop for prop in properties if prop not in excluded]
    return filtered, reason if filtered != properties else None
