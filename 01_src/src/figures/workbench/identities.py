"""Bind saved render addresses to independently verified source identities."""
from collections import Counter

from citrus_figures.presentation_ids import axes_address, presentation_key, series_address


class DeclarativeIdentity:
    def __init__(self, frozen):
        self.panels = {}
        self.series = {}
        if frozen is None:
            return
        for index, leaf in enumerate(frozen.panels or [frozen]):
            if frozen.panels and not leaf.panel_id:
                continue
            gid = axes_address(index, leaf.panel_id, multipanel=bool(frozen.panels))
            self.panels[index] = (gid, leaf.panel_id)
            if leaf.chart == "histogram":
                series = (leaf.series[0] if leaf.series else None,)
            elif leaf.chart in {"bar", "line", "scatter", "errorbar"}:
                series = leaf.series or (None,)
            else:
                continue
            for series_index, item in enumerate(series):
                key, name = (item.key, item.name) if item is not None else (None, "")
                address = series_address(index, leaf.panel_id, gid, series_index, key=key, name=name)
                self.series.setdefault((index, address), []).append((series_index, key, name))

    @staticmethod
    def _matches(locator, **expected):
        return all(k not in locator or (type(locator[k]) is type(v) and locator[k] == v) for k, v in expected.items())

    def panel(self, index, row):
        panel = self.panels.get(index)
        if panel is None:
            return None
        gid, panel_id = panel
        if (row["gid"] == gid and self._matches(row["locator"], panel_id=panel_id)
                and self._matches(row, presentation_key=presentation_key(panel_id, "axes"))):
            return panel_id or "single-spec-panel"
        return None

    def series_key(self, index, row):
        known = self.series.get((index, row["gid"]), [])
        if len(known) != 1:
            return None
        series_index, key, name = known[0]
        _, panel_id = self.panels[index]
        if not self._matches(row["locator"], panel_id=panel_id, series_id=row["gid"], series_index=series_index):
            return None
        if not self._matches(row, presentation_key=presentation_key(panel_id, "series", series_key=key, source_group=name)):
            return None
        if "semantic_key" in row and row["semantic_key"] != key:
            return None
        if key is not None and row.get("identity_scope") == "semantic" and row.get("semantic_key") != key:
            return None
        # Preserve prior workbench IDs for targets whose source binding is verified.
        return key if key is not None else row["gid"]

    def confirmation(self, before, after):
        """Allow only source-confirmed old omissions to acquire new metadata."""
        role = before["role"]
        index = before["locator"].get("axes_index")
        if (role not in {"series", "axes"} or role != after.get("role")
                or before["artist"] != after.get("artist") or before["gid"] != after.get("gid")):
            return False
        lookup = self.series_key if role == "series" else self.panel
        prior, current = lookup(index, before), lookup(index, after)
        if (prior is None or prior != current
                or "presentation_key" in before and before["presentation_key"] != after.get("presentation_key")):
            return False
        # This is an addition-only migration. A contradictory existing value,
        # changed coordinate, scope or artist cannot be normalized away.
        newer = after["locator"]
        return (all(k in newer and type(newer[k]) is type(v) and newer[k] == v for k, v in before["locator"].items())
                and set(newer) - set(before["locator"]) <= {"panel_id", "series_id", "series_index"})


def verified_panels(axes, declarative):
    resolved = {}
    for index, row in axes.items():
        if declarative is not None:
            key = declarative.panel(index, row)
        else:
            key = row.get("semantic_key") if row.get("identity_scope") == "semantic" else None
            # In a frozen script, a sole axes with no competing axes has a
            # unique container. A multi-axes ordinal is never a panel identity.
            if not key and len(axes) == 1:
                key = "single-rendered-panel"
        if isinstance(key, str) and key:
            resolved[index] = key
    multiplicity = Counter(resolved.values())
    return {index: key for index, key in resolved.items() if multiplicity[key] == 1}
