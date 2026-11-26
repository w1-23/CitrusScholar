"""Versioned manuscript typography for every figure worker (audit F-08).

Why this module exists
----------------------
``nature_runtime.configure_style()`` only exposed the upstream helper as a
callable adapter. Nothing forced a general ``run_research_figure_script`` job
to use it, so one project could mix Arial, DejaVu Sans and whatever the
container happened to install, and the workbench font menu advertised every
font on the host rather than the fonts the worker actually verified.

The rule
--------
* One versioned style, applied by default at the worker entry, with an explicit
  opt-out for a user who sets typography themselves.
* Latin and CJK families are configured separately; the CJK stack is only used
  when a script or a request asks for Chinese text.
* A family that is not installed produces a **warning naming the family and the
  resolved fallback**, never a silent substitution.
* Before publication the worker records the family it asked for, the family
  Matplotlib resolved, the font file behind it, and whether the exported format
  carries an embedded font program.
* The workbench offers the families the worker verified, not the host's list.

The injected prelude is self-contained Python: it imports only Matplotlib, so
it needs no package import inside the sandbox image.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

# The style is data, so a receipt can name exactly what was applied.
STYLE_VERSION = "citrus-manuscript/1"

LATIN_FAMILIES: tuple[str, ...] = ("Arial", "Helvetica", "DejaVu Sans", "Liberation Sans")
CJK_FAMILIES: tuple[str, ...] = (
    "Noto Sans CJK SC", "Source Han Sans SC", "Noto Sans SC", "SimHei", "Microsoft YaHei", "Noto Sans CJK JP",
)

# Families the worker is allowed to offer as verified choices. The workbench
# list is the intersection of this curated set with what is installed in the
# image; a host font that is not here is not offered.
VERIFIED_FAMILIES: tuple[str, ...] = LATIN_FAMILIES + CJK_FAMILIES

# Manuscript rcParams. Kept small and explicit: sizes in points, line widths in
# points, and a TrueType PDF so the exported PDF embeds a real font program.
BASE_RCPARAMS: dict[str, Any] = {
    "font.size": 7.0,
    "axes.titlesize": 7.0,
    "axes.labelsize": 7.0,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.75,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.major.width": 0.75,
    "ytick.major.width": 0.75,
    "xtick.major.size": 3.0,
    "ytick.major.size": 3.0,
    "legend.frameon": False,
    "figure.dpi": 150,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
    "mathtext.fontset": "dejavusans",
}

STYLE_MARKER = "# CITRUS_STYLE"

# A user who sets typography explicitly keeps it. These are the only two ways
# to opt out: calling the style hook by name, or assigning a font rcParam.
_OVERRIDE_TOKENS = (
    "configure_style(",
    "CITRUS_STYLE_OVERRIDE",
    "font.family",
    "font.sans-serif",
    "font.serif",
)


def _python(value: Any) -> str:
    """A literal safe to paste into generated source (names are user data)."""
    text = repr(value)
    # ``repr`` keeps non-ASCII readable; the worker writes UTF-8 source, so a
    # readable stack is preferable to escapes and stays a valid literal.
    return text


def font_stack(chinese: bool = False) -> tuple[str, ...]:
    return tuple(LATIN_FAMILIES) + tuple(CJK_FAMILIES) if chinese else tuple(LATIN_FAMILIES)


def style_preamble(*, chinese: bool = False, version: str = STYLE_VERSION) -> str:
    """Self-contained source that applies this style before the user's code.

    It prints one machine-readable line when a requested family is missing, so
    a container without Arial reports that fact instead of quietly drawing in
    whatever Matplotlib chose. The dict it leaves in ``CITRUS_STYLE`` is the
    in-process record; the exported-artifact record is built by
    :func:`font_evidence` from the real figure.
    """
    stack = font_stack(chinese)
    params = dict(BASE_RCPARAMS)
    return (
        f"{STYLE_MARKER}_BEGIN version={version} chinese={int(bool(chinese))}\n"
        f"CITRUS_STYLE_VERSION = {_python(version)}\n"
        "def _citrus_configure_style():\n"
        f"    _stack = {_python(list(stack))}\n"
        f"    _params = {_python(params)}\n"
        "    import json as _json\n"
        "    import sys as _sys\n"
        "    import warnings as _warnings\n"
        "    from matplotlib import rcParams as _rc\n"
        "    try:\n"
        "        from matplotlib import font_manager as _fm\n"
        "        _installed = {getattr(_entry, 'name', '') for _entry in _fm.fontManager.ttflist}\n"
        "    except Exception:\n"
        "        _installed = set()\n"
        "    _missing = [name for name in _stack if _installed and name not in _installed]\n"
        "    _resolved = next((name for name in _stack if not _installed or name in _installed), _stack[-1])\n"
        "    _primary_missing = bool(_installed) and _stack[0] not in _installed\n"
        "    for _key, _value in _params.items():\n"
        "        _rc[_key] = _value\n"
        "    _rc['font.family'] = ['sans-serif']\n"
        "    _rc['font.sans-serif'] = _stack + ['DejaVu Sans']\n"
        "    _report = {'style_version': " + _python(version) + ", 'requested_stack': _stack,\n"
        "               'resolved_family': _resolved, 'missing_families': _missing,\n"
        "               'primary_missing': _primary_missing}\n"
        "    if _primary_missing:\n"
        "        _message = '[CITRUS_FONT_MISSING] ' + _json.dumps(_report, ensure_ascii=True)\n"
        "        _warnings.warn(_message)\n"
        "        print(_message, file=_sys.stderr)\n"
        "    return _report\n"
        "CITRUS_STYLE = _citrus_configure_style()\n"
        f"{STYLE_MARKER}_END\n"
    )


def declares_style(source: str) -> bool:
    """Whether the script set typography itself (explicit override wins)."""
    text = str(source or "")
    return any(token in text for token in _OVERRIDE_TOKENS)


def inject(source: str, *, chinese: bool = False) -> tuple[str, dict[str, Any]]:
    """Return (executed source, style record) for one user script."""
    text = str(source or "")
    if not text.strip():
        return text, {"policy": "empty", "style_version": STYLE_VERSION}
    if STYLE_MARKER + "_BEGIN" in text:
        return text, {"policy": "already_styled", "style_version": STYLE_VERSION}
    if declares_style(text):
        return text, {"policy": "user_override", "style_version": STYLE_VERSION,
                      "requested_family": _requested_family()}
    return style_preamble(chinese=chinese) + text, {
        "policy": "injected", "style_version": STYLE_VERSION,
        "chinese": bool(chinese), "requested_family": list(font_stack(chinese)),
    }


def _requested_family() -> list[str]:
    try:
        from matplotlib import rcParams
    except Exception:
        return list(LATIN_FAMILIES)
    return [str(name) for name in rcParams.get("font.sans-serif", ())][:8]


def apply_style(*, chinese: bool = False) -> dict[str, Any]:
    """Apply the versioned style to this interpreter (host or worker side)."""
    from matplotlib import rcParams

    stack = font_stack(chinese)
    for key, value in BASE_RCPARAMS.items():
        rcParams[key] = value
    rcParams["font.family"] = ["sans-serif"]
    rcParams["font.sans-serif"] = list(stack) + ["DejaVu Sans"]
    available = set(available_families())
    missing = [name for name in stack if available and name not in available]
    resolved = next((name for name in stack if not available or name in available), stack[-1])
    return {"style_version": STYLE_VERSION, "requested_stack": list(stack),
            "resolved_family": resolved, "missing_families": missing,
            "warnings": [f"[CITRUS_FONT_MISSING] {name} 未安装，实际使用 {resolved}" for name in missing]}


def available_families() -> list[str]:
    """Family names backed by an existing font file in this interpreter."""
    try:
        from pathlib import Path
        from matplotlib import font_manager
    except Exception:
        return []
    return sorted({entry.name for entry in font_manager.fontManager.ttflist
                   if Path(entry.fname).is_file()}, key=str.casefold)


def verified_families(available: Iterable[str] | None = None) -> list[str]:
    """The workbench menu: curated scientific families the worker verified.

    An image that installed none of the curated families keeps its installed
    list, so the workbench never offers an empty menu; that case is a build
    problem, and :func:`resolve_font` reports it as a missing family.
    """
    installed = {str(name).casefold(): str(name) for name in
                 (available if available is not None else available_families())}
    curated = [installed[name.casefold()] for name in VERIFIED_FAMILIES
               if name.casefold() in installed]
    if curated:
        return curated
    return sorted(installed.values(), key=str.casefold)


def resolve_font(family: str, *, available: Iterable[str] | None = None) -> dict[str, Any]:
    """Resolve one requested family to a real font file, or name the fallback."""
    installed = list(available if available is not None else available_families())
    folded = {name.casefold() for name in installed}
    if str(family).casefold() in folded:
        resolved = str(family)
        missing = False
    else:
        stack = list(font_stack()) + list(available_families())
        resolved = next((name for name in stack if name.casefold() in folded), "")
        missing = True
    path = ""
    try:
        from matplotlib import font_manager
        path = str(font_manager.findfont(
            font_manager.FontProperties(family=[resolved or "sans-serif"]),
            fallback_to_default=True))
    except Exception:
        path = ""
    return {"requested": str(family), "resolved": resolved, "font_file": path,
            "missing": missing}


def _embedding(pdf_bytes: bytes | None, formats: Iterable[str]) -> dict[str, str]:
    """Whether each exported format carries an embedded font program."""
    result: dict[str, str] = {}
    formats = [str(fmt).lower().lstrip(".") for fmt in formats]
    if "pdf" in formats:
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes:
            result["pdf"] = "not_provided"
        elif (b"/FontFile2" in pdf_bytes or b"/FontFile3" in pdf_bytes
              or b"/FontFile " in pdf_bytes or b"/Type3" in pdf_bytes):
            result["pdf"] = "font_program_embedded"
        else:
            result["pdf"] = "no_embedded_font_program"
    if "svg" in formats:
        # SVG keeps real text and references a family name; it does not embed
        # a font program. Saying so is the honest record.
        result["svg"] = "family_reference_only"
    if "png" in formats or "tiff" in formats:
        result["raster"] = "embedded_at_render_time"
    return result


def embedding_report(*, formats: Iterable[str] = ("svg",),
                     pdf_bytes: bytes | None = None) -> dict[str, str]:
    """Public per-format embedding record, for a publication receipt."""
    return _embedding(pdf_bytes, formats)


def font_evidence(figure: Any = None, *, pdf_bytes: bytes | None = None,
                  formats: Iterable[str] = ("svg",), requested: Iterable[str] | None = None,
                  available: Iterable[str] | None = None) -> dict[str, Any]:
    """Record what a rendered figure actually used, before publication."""
    used: list[str] = []
    declared: list[str] = []
    text_count = 0
    font_file = ""
    if figure is not None:
        try:
            from matplotlib import font_manager
            from matplotlib.text import Text
        except Exception:
            font_manager = None
            Text = ()
        if font_manager is not None:
            for artist in figure.findobj():
                if not isinstance(artist, Text):
                    continue
                label = str(artist.get_text() or "")
                if not label.strip():
                    continue
                text_count += 1
                try:
                    for name in artist.get_fontfamily() or ():
                        if name and name not in declared:
                            declared.append(str(name))
                except Exception:
                    pass
                try:
                    # Resolve the artist's own FontProperties, then read back the
                    # concrete family name behind the selected font file. This is
                    # the family the exporter will actually reference.
                    path = font_manager.findfont(artist.get_fontproperties(),
                                                 fallback_to_default=False)
                except Exception:
                    continue
                try:
                    name = font_manager.FontProperties(fname=path).get_name()
                except Exception:
                    name = ""
                if name and name not in used:
                    used.append(name)
                if path and not font_file:
                    font_file = str(path)
    requested_stack = [str(name) for name in (requested or declared or _requested_family())][:8]
    installed = list(available if available is not None else available_families())
    folded = {name.casefold() for name in installed}
    generic = {"sans-serif", "serif", "sans", "cursive", "monospace", "fantasy", "system-ui"}
    concrete = [name for name in requested_stack
                if name.casefold() in folded and name.casefold() not in generic]
    missing = [name for name in requested_stack
               if name.casefold() not in folded and name.casefold() not in generic]
    resolved = used[0] if used else concrete[0] if concrete else ""
    if not font_file:
        try:
            from matplotlib import font_manager
            font_file = str(font_manager.findfont(
                font_manager.FontProperties(family=[resolved or "sans-serif"]),
                fallback_to_default=True))
        except Exception:
            font_file = ""
    cjk_present = [name for name in used if name in CJK_FAMILIES]
    warnings = [f"{name} 未在 Worker 中安装；实际解析为 {resolved or 'Matplotlib 默认'}"
                for name in missing]
    return {
        "style_version": STYLE_VERSION,
        "requested_family": requested_stack,
        "used_family": used,
        "resolved_family": resolved,
        "font_file": font_file,
        "missing_family": missing,
        "chinese_used": bool(cjk_present),
        "text_objects": text_count,
        "embedding": _embedding(pdf_bytes, formats),
        "warnings": warnings,
        "fonts_verified": bool(installed),
    }


__all__ = ["BASE_RCPARAMS", "CJK_FAMILIES", "LATIN_FAMILIES", "STYLE_MARKER", "STYLE_VERSION",
           "VERIFIED_FAMILIES", "apply_style", "available_families", "declares_style",
           "embedding_report", "font_evidence", "font_stack", "inject", "resolve_font",
           "style_preamble", "verified_families"]
