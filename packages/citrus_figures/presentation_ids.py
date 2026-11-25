"""Pure declarative renderer addresses shared with revision verification."""
from hashlib import sha256


def axes_address(index, panel_id, *, multipanel):
    return f"panel_{panel_id}" if multipanel else f"axes_{index}"


def series_address(index, panel_id, axes_gid, series_index, *, key=None, name=""):
    """Mirror the renderer's versioned GID, never a mutable legend label."""
    if not panel_id:
        return f"axes_{index}.series_{series_index}"
    if key is not None:
        return f"{axes_gid}.series_explicit_{series_index}"
    token = sha256((name or "ungrouped").encode("utf-8")).hexdigest()[:16]
    return f"{axes_gid}.series_{token}"
