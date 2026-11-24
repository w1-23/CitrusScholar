"""Closed presentation extensions for source-keyed frozen script artists.

These are stored outside the v1 replay target digest. Existing artists retain
exactly the same replay evidence, including the old role capability list.
"""
from __future__ import annotations

COLORMAPS = ("viridis", "cividis", "plasma", "magma", "inferno", "Blues", "Greys", "RdBu_r")


def options(artist):
    from matplotlib import collections

    kind = type(artist)
    if not isinstance(artist, collections.Collection) or artist.get_animated() or artist.get_agg_filter() is not None:
        return {}
    if any(name == "draw" or name.startswith("set_") for name in vars(artist)):
        return {}
    if kind is collections.QuadMesh and artist.get_array() is not None and not artist.get_rasterized():
        return {"workbench_editable": ["cmap"], "cmap": artist.get_cmap().name}
    if kind in (collections.PolyCollection, getattr(collections, "FillBetweenPolyCollection", None)) and artist.get_array() is None:
        return {"workbench_editable": ["color", "alpha", "linewidth", "visible", "label"]}
    return {}


def properties(row):
    if row.get("role") != "series" or row.get("identity_scope") != "semantic":
        return set()
    advertised = row.get("workbench_editable")
    if not isinstance(advertised, list) or any(not isinstance(prop, str) for prop in advertised):
        return set()
    allowed = {"QuadMesh": {"cmap"},
               "PolyCollection": {"color", "alpha", "linewidth", "visible", "label"},
               "FillBetweenPolyCollection": {"color", "alpha", "linewidth", "visible", "label"}}
    return set(advertised) & allowed.get(row.get("artist"), set())
