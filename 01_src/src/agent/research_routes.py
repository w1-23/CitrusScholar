"""Report observed coverage only when the user explicitly requested both sources."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.core.tool_catalog import capability_groups
from src.graph.retrieval_router import parse_request_constraints

_GROUPS = capability_groups()
_LOCAL_TOOLS = _GROUPS["route_local"] | {"read_candidate", "read_local_evidence", "search_candidates", "search_agent_corpus"}
_WEB_TOOLS = _GROUPS["web_retrieval"] | {"search_literature"}
_OBSERVED = frozenset({"complete", "partial", "no_results"})


def explicitly_requests_both_sources(query: str) -> bool:
    constraints = parse_request_constraints(query)
    return constraints.source_policy == "required" and constraints.requested_mode == "fusion"


def dual_source_notice(query: str, records: Iterable[object]) -> str:
    """No route parity requirement, provider preference or retry instructions."""
    if not explicitly_requests_both_sources(query):
        return ""
    used = {row.get("name") for row in records
            if isinstance(row, Mapping) and not row.get("inherited_from_job_id")
            and row.get("status") in _OBSERVED}
    missing = [label for names, label in ((_LOCAL_TOOLS, "本地检索"), (_WEB_TOOLS, "公开网络检索"))
               if not used.intersection(names)]
    if not missing:
        return ""
    return "覆盖说明：本轮" + "、".join(missing) + "未完成；回答仅基于已实际取得的证据。"
