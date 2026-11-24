"""Bounded local + DeepSeek Search coverage for one research turn.

The gate is deliberately small. It does not classify the user request and it
does not decide whether a question is a research question: the model already
made that decision by starting a discovery tool. Once one of the two
authorized discovery routes has actually started, the host asks for the other
route once; a second final attempt is accepted with an explicit coverage note.

Tool names are the contract:

* local discovery: ``local_hybrid_search`` / ``search_candidates`` /
  ``search_agent_corpus``;
* local reuse: a successful ``read_candidate`` / ``inspect_candidates`` /
  ``read_local_evidence``;
* web discovery: ``deepseek_search`` only.

Fetch, Browse and literature metadata lookup are reads or metadata operations,
not the DeepSeek Search route.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

LOCAL_DISCOVERY = frozenset({"local_hybrid_search", "search_candidates", "search_agent_corpus"})
LOCAL_REUSE = frozenset({"read_candidate", "inspect_candidates", "read_local_evidence"})
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


def _challenged(prior: object) -> dict[str, int]:
    if not isinstance(prior, Mapping) or prior.get("version") != 1:
        return {}
    raw = prior.get("challenged")
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key, value in raw.items()
            if isinstance(key, str) and type(value) is int and 0 <= value <= 2}


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
    attempts: int = 0
    prior: Mapping | None = None

    @property
    def challenge(self) -> bool:
        return self.active and bool(self.missing) and self.attempts == 0

    @property
    def exhausted(self) -> bool:
        return self.active and bool(self.missing) and self.attempts > 0

    def next_state(self) -> dict:
        state = {"version": 1, "challenged": dict(_challenged(self.prior))}
        key = "+".join(self.missing)
        state["challenged"][key] = int(state["challenged"].get(key, 0)) + 1
        return state

    def as_dict(self) -> dict:
        return {"schema_version": 1, "active": self.active,
                "missing": list(self.missing), "attempts": self.attempts,
                "challenge": self.challenge, "exhausted": self.exhausted,
                "notice": self.notice}


def research_route_gate(records: Iterable[object], allowed: Iterable[str], *,
                        prior: object = None) -> RouteGate:
    """Return the bounded route gate for one final-answer attempt."""
    allowed_names = {str(item) for item in (allowed or ())}
    local_authorized = bool(allowed_names & LOCAL_DISCOVERY)
    web_authorized = bool(allowed_names & WEB_SEARCH)
    if not (local_authorized or web_authorized):
        return RouteGate()
    if not _attempted(records, LOCAL_DISCOVERY | WEB_SEARCH):
        return RouteGate()
    local_done = _attempted(records, LOCAL_DISCOVERY) or _succeeded(records, LOCAL_REUSE)
    web_done = _attempted(records, WEB_SEARCH)
    missing = tuple(route for route, authorized, done in (
        ("local", local_authorized, local_done),
        ("web", web_authorized, web_done),
    ) if authorized and not done)
    if not missing:
        return RouteGate()
    prior_state = prior if isinstance(prior, Mapping) else None
    attempts = int(_challenged(prior_state).get("+".join(missing), 0))
    return RouteGate(active=True, missing=missing,
                     message=_message(missing, local_done=local_done, web_done=web_done),
                     notice=_notice(missing), attempts=attempts, prior=prior_state)
