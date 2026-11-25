"""Keep the existing graph edge while local retrieval runs as a named tool.

No lookup or translation happens before the ToolNode. Old same-job resumes
retain their saved candidate snapshot and the host restores existing read refs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.graph.state import AgentState

NODE_NAME = "candidate_prefetch"
STATUS_SKIPPED = "skipped"


async def prefetch_candidates(state: AgentState) -> dict:
    if state.get("agent_resume") is True:
        return {}
    reason = "framework_tool_routing"
    return {
        "candidate_pool": {"pool_id": "", "status": STATUS_SKIPPED, "count": 0, "families": [],
                           "reason": reason, "translation_used": False, "elapsed_ms": 0.0, "error": ""},
        "candidate_pool_notice": "",
        "_trace": {"node": NODE_NAME, "status": STATUS_SKIPPED, "reason": reason},
    }
