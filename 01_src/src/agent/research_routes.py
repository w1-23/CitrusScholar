"""Report observed coverage only when the user explicitly requested both sources."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.core.tool_catalog import capability_groups
from src.graph.retrieval_router import parse_request_constraints

_GROUPS = capability_groups()
_LOCAL_TOOLS = _GROUPS["route_local"] | {"read_local_evidence"}
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


# These are business capability scopes, not a second agent or a second Skill loader.
# The same selection narrows the SDK's tool schemas and admission of shared tools.
_COMMON = frozenset({"request_user_input", "read_tool_result", "read_context_evidence",
    "read_account_file", "deliver_research_artifact", "word_document"})
_EXTERNAL = frozenset({"run_external_skill", "collect_external_skill"})
_SCOPE_TOOLS = {
    "nature-figure": frozenset({"inspect_uploaded_table", "analyze_research_data",
        "research_workspace", "run_research_figure_script", "draw_svg"}),
    "data-audit": _EXTERNAL | {"inspect_uploaded_table", "audit_uploaded_data",
        "prepare_audit_dataset", "audit_prepared_dataset"},
    "research-integrity": _EXTERNAL | {"inspect_uploaded_table", "audit_research_images",
        "run_numeric_integrity", "collect_numeric_integrity"},
    "literature-verification": _EXTERNAL | {"continue_external_literature", "verify_uploaded_references",
        "verify_literature_references", "search_literature", "deepseek_search", "deepseek_fetch", "deepseek_fetch_paper"},
}


def scientific_scope(query, *, selected=(), has_tables=False):
    from src.graph.agent_operations import _explicit_data_audit, _explicit_reference_check
    from src.graph.intent_text import mask_literal_payload
    from src.graph.retrieval_router import attachment_processing_only, parse_request_constraints
    from src.graph.task_contract import FIGURE_KINDS, contract_for_turn

    picked = frozenset(selected)
    if picked:
        # An explicit mixed selection (e.g. question answering + plotting) is
        # not a single-purpose method; keep the ordinary authorized surface.
        return picked if picked <= _SCOPE_TOOLS.keys() else frozenset()
    if not has_tables or not attachment_processing_only(query, has_source=True):
        return frozenset()
    text = mask_literal_payload(query)
    if _explicit_reference_check(text):
        return frozenset()
    figure = any(FIGURE_KINDS.intersection(row.get("kinds", ()))
                 for row in contract_for_turn(text, has_tables=True).deliverables)
    scope = {"nature-figure"} if figure else set()
    # Table analysis can be a preparatory plotting step; only an explicit
    # data-quality request adds the separate audit workflow to a figure turn.
    if _explicit_data_audit(text) and (not figure or parse_request_constraints(text).data_audit_requested):
        scope.add("data-audit")
    return frozenset(scope)


def scoped_tools(available, scope, *, retrieval=()):
    if not scope:
        return set(available)
    return set(available).intersection(_COMMON.union(retrieval, *(_SCOPE_TOOLS[key] for key in scope)))


def accepts_method(tool, args, scope):
    return not scope or tool not in _EXTERNAL or args.get("skill_id") in scope



def uncollected_skill_results(records, calls, messages):
    """Only an accepted Worker submission creates a collection obligation."""
    pending = set()
    numeric_pending = set()
    for row in records:
        if row.get("inherited_from_job_id"):
            continue
        cid = row.get("tool_call_id")
        name = row.get("name") or row.get("tool_name")
        if name in {"run_numeric_integrity", "collect_numeric_integrity"}:
            artifact = getattr(messages.get(cid), "artifact", None)
            if isinstance(artifact, dict) and artifact.get("execution_id"):
                execution_id = artifact["execution_id"]
                if name == "run_numeric_integrity" and artifact.get("collect_required") is True:
                    numeric_pending.add(execution_id)
                elif name == "collect_numeric_integrity" and row.get("status") in {"complete", "partial", "failed"}:
                    numeric_pending.discard(execution_id)
            continue
        if name not in {"run_external_skill", "continue_external_literature", "collect_external_skill"}:
            continue
        reason = row.get("reason") or row.get("reason_code")
        args = (calls.get(cid) or {}).get("args") or {}
        skill = "literature-verification" if name == "continue_external_literature" else args.get("skill_id")
        artifact = getattr(messages.get(cid), "artifact", None)
        if skill not in ("data-audit", "literature-verification") or not isinstance(artifact, dict):
            continue
        if (name in {"run_external_skill", "continue_external_literature"}
                and reason == "external_skill_ready" and row.get("status") == "complete"
                and artifact.get("collect_required") is True and artifact.get("_code_execution_id")):
            pending.add((skill, artifact["_code_execution_id"]))
        elif (name == "collect_external_skill" and row.get("status") in {"complete", "partial"}
              and reason in {"external_skill_report", "external_report_not_published"}
              and artifact.get("execution_id") == args.get("execution_id")):
            pending.discard((skill, args.get("execution_id")))
    return frozenset(skill for skill, _ in pending) | ({"research-integrity"} if numeric_pending else set())
