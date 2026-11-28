"""Host admission limits for new evidence. Authorized readback is separate."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import time

from src.retrieval.evidence_contract import evidence_contract, retrieval_limitation

_DISCOVERY = frozenset({"local", "web_search", "literature"})
_LANES = {
    "local_hybrid_search": "local", "search_candidates": "local", "search_agent_corpus": "local",
    "deepseek_search": "web_search", "search_literature": "literature",
    "deepseek_fetch": "web_fetch", "deepseek_fetch_paper": "web_fetch", "browse_web": "browser",
}
_NON_RETRIEVAL_BROWSER = frozenset({"close", "close_tab", "list_tabs", "download"})


def _lane(name: str, args: Mapping) -> str | None:
    if name == "browse_web" and args.get("action") in _NON_RETRIEVAL_BROWSER:
        return None
    return _LANES.get(name)


def _estimated_tokens(content, artifact: Mapping) -> int:
    # Approximate provider-independent token accounting. The cap is a stop for
    # new retrieval, not a truncation or mutation of the original receipt.
    if isinstance(content, str):
        return math.ceil(len(content.encode("utf-8", errors="replace")) / 3)
    size = 0
    for field in ("main_results", "web_results"):
        rows = artifact.get(field)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if isinstance(row, Mapping):
                text = next((row[key] for key in ("text", "content", "snippet", "summary")
                             if isinstance(row.get(key), str) and row[key]), "")
                size += len(text.encode("utf-8", errors="replace"))
    return math.ceil(size / 3)


def _key(row: dict) -> tuple:
    if row["origin"] == "local":
        return (row["corpus_ref"], row["generation"], row["chunk_ref"] or row["source_ref"])
    # Discovery and fetched/rendered text are distinct levels of evidence.
    return (row["url"], row["source_status"], row.get("text_sha256"))


@dataclass(frozen=True)
class RetrievalLimits:
    calls: int = 10
    # One first pass and no more than two gap searches per discovery lane.
    discovery_calls: int = 3
    observed_tokens: int = 16_000
    seconds: float = 60.0


@dataclass
class TurnRetrievalBudget:
    limits: RetrievalLimits = field(default_factory=RetrievalLimits)
    _started: float | None = None
    _wall_started: float | None = None
    _calls: dict[str, str] = field(default_factory=dict)
    _pending: dict[str, str] = field(default_factory=dict)
    _lanes: Counter = field(default_factory=Counter)
    _seen: dict[str, set[tuple]] = field(default_factory=dict)
    _observed: set[str] = field(default_factory=set)
    _no_gain: Counter = field(default_factory=Counter)
    _token_count: int = 0
    _stops: dict[str, set[str]] = field(default_factory=dict)
    _limitations: set[str] = field(default_factory=set)

    def timer_state(self) -> float | None:
        return self._wall_started

    def restore_timer_state(self, started_at) -> None:
        if (type(started_at) in {int, float} and 0 < started_at <= time.time()
                and (self._wall_started is None or started_at < self._wall_started)):
            self._wall_started = float(started_at)
            self._started = time.monotonic() - max(0.0, time.time() - started_at)

    def restore(self, name: str, call_id: str, args: Mapping) -> None:
        """Count a verified dispatched call at most once, including ambiguous results."""
        lane = _lane(name, args)
        if not lane or not call_id or call_id in self._calls:
            return
        self._pending.pop(call_id, None)
        if self._started is None:
            self._started = time.monotonic()
            self._wall_started = time.time()
        self._calls[call_id] = lane
        self._lanes[lane] += 1

    def _stop_reason(self, lane: str) -> str | None:
        if len(self._calls) + len(self._pending) >= self.limits.calls:
            return "retrieval_call_budget_exhausted"
        if self._token_count >= self.limits.observed_tokens:
            return "retrieval_token_budget_exhausted"
        if self._started is not None and time.monotonic() - self._started >= self.limits.seconds:
            return "retrieval_deadline_exhausted"
        if self._no_gain[lane] >= 2:
            return "retrieval_no_new_evidence"
        if lane in _DISCOVERY and self._lanes[lane] + sum(v == lane for v in self._pending.values()) >= self.limits.discovery_calls:
            return "retrieval_followup_budget_exhausted"
        return None

    def visible_tools(self, names) -> set[str]:
        """Retire discovery/fetch schemas, keeping exact reads and non-search tools.

        The browser also closes tabs and downloads existing results. Gate its
        retrieval actions per invocation instead of removing its whole schema.
        """
        return {name for name in names if name == "browse_web" or name not in _LANES
                or not self._stop_reason(_LANES[name])}

    def reserve(self, name: str, call_id: str, args: Mapping) -> str | None:
        lane = _lane(name, args)
        if not lane or call_id in self._calls or call_id in self._pending:
            return None
        if reason := self._stop_reason(lane):
            self._stops.setdefault(reason, set()).add(lane)
            return reason
        self._pending[call_id] = lane
        return None

    def release(self, call_id: str) -> None:
        self._pending.pop(call_id, None)

    def admit(self, name: str, call_id: str, args: Mapping) -> str | None:
        if reason := self.reserve(name, call_id, args):
            return reason
        self.restore(name, call_id, args)
        return None

    def observe(self, name: str, call_id: str, artifact, *, status: str, reason: str, content=None) -> None:
        lane = self._calls.get(call_id)
        if lane is None or name not in _LANES or call_id in self._observed:
            return
        self._observed.add(call_id)
        artifact = artifact if isinstance(artifact, Mapping) else {}
        self._token_count += _estimated_tokens(content, artifact)
        limitation = retrieval_limitation(artifact, status, reason)
        if limitation:
            self._limitations.add(limitation)
        # A failed or denied provider is not the same thing as a successful
        # probe with no new evidence. Other providers remain independently usable.
        if limitation not in {None, "partial_source"}:
            return
        projected = evidence_contract(name, artifact, status=status, reason=reason)
        if projected is None:
            return  # an unstructured result is not affirmative no-progress evidence
        seen = self._seen.setdefault(lane, set())
        keys = {_key(row) for row in projected["results"]}
        if keys - seen:
            seen.update(keys)
            self._no_gain[lane] = 0
        elif keys or reason == "no_new_candidates":
            self._no_gain[lane] = 2
        elif (projected["status"] == "no_results"
              and not (artifact.get("corpus_contract") == 1 and artifact.get("selected_result_count"))):
            self._no_gain[lane] += 1

    def summary(self) -> dict:
        """Bounded diagnostics with operation coverage, not proof of answer completeness."""
        return {
            "status": "partial" if self._stops or self._limitations else "complete" if self._calls else "not_run",
            "coverage": {lane: len(keys) for lane, keys in sorted(self._seen.items())},
            "calls": dict(sorted(self._lanes.items())), "estimated_observed_tokens": self._token_count,
            "stop_reasons": sorted(self._stops), "unresolved_lanes": sorted(set().union(*self._stops.values())),
            "limitations": sorted(self._limitations),
        }
