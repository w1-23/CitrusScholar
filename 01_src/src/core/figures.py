# -*- coding: utf-8 -*-
"""Request-scoped SVG figure registry (artifact-first drawing).

Why this exists
---------------
``draw_svg`` used to return the whole sanitized SVG and instruct the model to
paste it back verbatim into the final answer.  That made every diagram cost two
full generations of the same markup: once as the tool argument, once as answer
text.  Long diagrams therefore ran into the request deadline during synthesis
("第一轮绘图总是说超时"), the user retried, and each retry validated the SVG
again ("每次绘图SVG都需要反复安全校验").

With this registry the tool result is a handful of tokens: it stores the
sanitized markup and returns a ``[[FIGURE:n]]`` placeholder.  The graph expands
the placeholder once, after the answer is complete, so the browser receives
exactly the markup that passed validation and the model never retypes it.

Concurrency
-----------
The graph creates a mutable request-owned registry before dispatching tools.
Copied contexts in tool worker threads reference that same object, so writes
are visible to the owning graph without sharing data with other requests.
The lock protects concurrent tool writes within a request. Starting a new
request replaces the context binding; it never clears another request's data.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

PLACEHOLDER_RE = re.compile(r"\[\[FIGURE:(\d+)\]\]")


@dataclass
class _Registry:
    figures: dict[int, dict[str, str]] = field(default_factory=dict)
    attempts: int = 0
    high_water: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_CURRENT: ContextVar[_Registry | None] = ContextVar("request_figures", default=None)


def _registry() -> _Registry:
    registry = _CURRENT.get()
    if registry is None:
        # Direct tool callers have an isolated scope too. The graph must call
        # begin_request before dispatch so its worker contexts share this value.
        registry = _Registry()
        _CURRENT.set(registry)
    return registry

_TOOL_TEXT = (
    "SVG 已通过安全校验，登记为图示 {idx}（元素与属性均已在服务端过滤）。"
    "请在最终回答中用一行占位符 `[[FIGURE:{idx}]]` 表示该图，并附一句图示说明；"
    "不要重复输出 SVG 源码，也不要再次调用本工具生成同一张图。"
)


def begin_request() -> None:
    """Bind a fresh registry before creating this request's tool tasks."""
    _CURRENT.set(_Registry())


def bump_attempts() -> int:
    """Count draw_svg submissions in this request so retries cannot eat the deadline."""
    registry = _registry()
    with registry.lock:
        registry.attempts += 1
        return registry.attempts


def register(svg: str, alt_text: str = "", *, download_url: str = "",
             workbench_url: str = "") -> tuple[int, str]:
    """Store a validated SVG and return ``(index, text_for_model)``."""
    registry = _registry()
    with registry.lock:
        registry.high_water += 1
        index = registry.high_water
        registry.figures[index] = {"svg": svg, "alt": alt_text or "图示",
                                   "download_url": download_url, "workbench_url": workbench_url}
    return index, _TOOL_TEXT.format(idx=index)


def count() -> int:
    registry = _registry()
    with registry.lock:
        return len(registry.figures)


def snapshot() -> dict[int, dict[str, str]]:
    registry = _registry()
    with registry.lock:
        return {k: dict(v) for k, v in registry.figures.items()}


_FIGURE_FIELDS = {"svg", "alt", "download_url", "workbench_url"}


def _recovery_hash(identity, index, figure):
    encoded = json.dumps([identity, index, figure], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def export_recovery(identity) -> dict:
    """Capture validated figure references only in this run's protected checkpoint."""
    binding = asdict(identity)
    registry = _registry()
    with registry.lock:
        return {"identity": binding, "attempts": registry.attempts, "high_water": registry.high_water, "entries": [
            {"index": index, "figure": dict(figure), "sha256": _recovery_hash(binding, index, figure)}
            for index, figure in registry.figures.items()]}


def restore_recovery(identity, saved) -> None:
    """Restore the original numbering without rerendering or republishing a figure."""
    if saved is None:
        return  # Old checkpoints did not persist figures.
    binding = asdict(identity)
    if (not isinstance(saved, dict) or saved.get("identity") != binding
            or type(saved.get("attempts")) is not int or saved["attempts"] < 0
            or not isinstance(saved.get("entries"), list)):
        raise ValueError("figure_checkpoint_invalid")
    figures = {}
    high_water = saved.get("high_water", len(saved["entries"]))
    index = 0
    if type(high_water) is not int or high_water < 0:
        raise ValueError("figure_checkpoint_invalid")
    for entry in saved["entries"]:
        if (not isinstance(entry, dict) or type(entry.get("index")) is not int
                or not index < entry["index"] <= high_water):
            raise ValueError("figure_checkpoint_invalid")
        index = entry["index"]
        figure = entry.get("figure")
        if (not isinstance(figure, dict) or set(figure) != _FIGURE_FIELDS
                or any(type(value) is not str for value in figure.values())
                or entry.get("sha256") != _recovery_hash(binding, index, figure)):
            raise ValueError("figure_checkpoint_invalid")
        figures[index] = dict(figure)
    registry = _registry()
    with registry.lock:
        registry.figures = figures
        registry.attempts = saved["attempts"]
        registry.high_water = high_water


def reserve_legacy_indices(texts) -> None:
    """Legacy graph receipts had placeholders but no recoverable SVG bytes."""
    occupied = max((int(match.group(1)) for text in texts if isinstance(text, str)
                    for match in PLACEHOLDER_RE.finditer(text)
                    if len(match.group(1)) == 1 and 1 <= int(match.group(1)) <= 4), default=0)
    registry = _registry()
    with registry.lock:
        registry.high_water = max(registry.high_water, occupied)
        registry.attempts = max(registry.attempts, occupied)


def expand(text: str) -> str:
    """Replace every placeholder with the fenced SVG block it stands for."""
    if not text or "[[" not in text:
        return text
    store = snapshot()
    if not store:
        return text

    def _sub(match: re.Match[str]) -> str:
        figure = store.get(int(match.group(1)))
        if not figure:
            return match.group(0)
        links = []
        for key, label, pattern in (
            ("download_url", "下载 SVG", r"/api/v2/[A-Za-z0-9_./?=&%+-]+"),
            ("workbench_url", "在图件工作台打开", r"/figures\?project=[a-f0-9]{32}"),
        ):
            url = figure[key]
            if url and re.fullmatch(pattern, url):
                links.append(f"[{label}]({url})")
        markdown = f"```svg\n{figure['svg']}\n```"
        return markdown + ("\n\n" + " · ".join(links) if links else "")

    return PLACEHOLDER_RE.sub(_sub, text)


def clear() -> None:
    begin_request()
