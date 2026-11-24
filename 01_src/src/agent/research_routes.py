"""Compute observed research source coverage without steering execution."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping

LOCAL_DISCOVERY = frozenset({"local_hybrid_search", "search_candidates", "search_agent_corpus"})
LOCAL_REUSE = frozenset({"read_candidate", "read_local_evidence"})
WEB_SEARCH = frozenset({"deepseek_search"})

_NOT_RUN = frozenset({"not_run", "rejected"})
_SUCCESS = frozenset({"complete", "partial"})


def _attempted(records: Iterable[object], names: frozenset[str]) -> bool:
    for row in records or ():
        if not isinstance(row, Mapping) or row.get("inherited_from_job_id"):
            continue
        if str(row.get("name") or "") not in names:
            continue
        if str(row.get("status") or "").strip().lower() in _NOT_RUN:
            continue
        return True
    return False


def _succeeded(records: Iterable[object], names: frozenset[str]) -> bool:
    for row in records or ():
        if not isinstance(row, Mapping) or row.get("inherited_from_job_id"):
            continue
        if str(row.get("name") or "") not in names:
            continue
        if str(row.get("status") or "").strip().lower() in _SUCCESS:
            return True
    return False


def _message(missing: tuple[str, ...], *, local_done: bool, web_done: bool) -> str:
    routes = set(missing)
    if routes == {"local", "web"}:
        return ("宿主检查：本轮已开始检索，但本地检索与 DeepSeek 网络 Search 都还没有完成一次尝试。"
                "当前授权允许时，对同一知识子目标各补一次；已有等价证据可直接复用。")
    if "local" in routes:
        prefix = "本轮已做 DeepSeek 网络 Search，但缺少本地检索的一次尝试。" if web_done else "本轮缺少本地检索的一次尝试。"
        return "宿主检查：" + prefix + "当前授权允许时补一次本地检索；已有等价本地证据可直接复用。"
    prefix = "本轮已做本地检索，但缺少 DeepSeek 网络 Search 的一次尝试。" if local_done else "本轮缺少 DeepSeek 网络 Search 的一次尝试。"
    return "宿主检查：" + prefix + "当前授权允许时补一次网络 Search。"


def _notice(missing: tuple[str, ...]) -> str:
    labels = {"local": "本地检索", "web": "DeepSeek 网络 Search"}
    return "覆盖说明：本轮" + "、".join(labels[item] for item in missing) + "未完成；回答仅基于已实际取得的证据。"


@dataclass(frozen=True)
class RouteGate:
    active: bool = False
    missing: tuple[str, ...] = ()
    message: str = ""
    notice: str = ""
    challenge: bool = False


def explicitly_requests_both_sources(query: str) -> bool:
    from src.graph.retrieval_router import retrieval_denied, source_scope

    scope = source_scope(query, exclusive_only=True)
    if scope in {"local", "web"} or retrieval_denied(query, scope):
        return False
    local = r"本地|知识库|\blocal\b"
    web = r"公开网|联网|网络|网上|网页|\b(?:web|internet|online)\b"
    request = r"分别|同时|都要|都查|都用|都搜|一起查|交叉核对|结合|请.{0,12}(?:查|搜索|检索)|\bboth\b"
    return all(re.search(pattern, query, re.I) for pattern in (local, web, request))


def research_route_gate(records: Iterable[object], allowed: Iterable[str], *,
                        explicit: bool = False) -> RouteGate:
    """Report missing coverage without generating a model instruction."""
    allowed_names = {str(item) for item in (allowed or ())}
    local_authorized = bool(allowed_names & LOCAL_DISCOVERY)
    web_authorized = bool(allowed_names & WEB_SEARCH)
    if not explicit and not (local_authorized or web_authorized):
        return RouteGate()
    if not explicit and not _attempted(records, LOCAL_DISCOVERY | WEB_SEARCH):
        return RouteGate()
    local_done = _attempted(records, LOCAL_DISCOVERY) or _succeeded(records, LOCAL_REUSE)
    web_done = _attempted(records, WEB_SEARCH)
    missing = tuple(route for route, authorized, done in (
        ("local", local_authorized, local_done),
        ("web", web_authorized, web_done),
    ) if (authorized or explicit) and not done)
    if not missing:
        return RouteGate()
    return RouteGate(active=True, missing=missing, notice=_notice(missing))
