"""Read-only tool-pair validation for already-persisted legacy checkpoints."""
from __future__ import annotations


def validate_pairs(sources: list[dict]) -> list[str]:
    pending, seen, calls = set(), set(), []
    for row in sources:
        role = row["role"]
        if role == "tool":
            call_id = row["tool_call_id"]
            if call_id not in pending:
                raise ValueError("checkpoint_orphan_tool")
            pending.remove(call_id)
        else:
            if pending:
                raise ValueError("checkpoint_incomplete_tool_group")
            if role in {"ai", "AIMessageChunk"}:
                for call in row["tool_calls"]:
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not call_id or call_id in seen:
                        raise ValueError("checkpoint_duplicate_tool_id")
                    pending.add(call_id)
                    seen.add(call_id)
                    calls.append(call_id)
    if pending or not sources or sources[-1]["role"] not in {"ai", "AIMessageChunk"}:
        raise ValueError("checkpoint_unfinished_turn")
    return calls
