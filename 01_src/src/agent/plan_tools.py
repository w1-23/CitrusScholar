"""Project committed SDK todos to the owner-scoped public task timeline."""
import logging

from langchain_core.messages import AIMessage, ToolMessage

NATIVE_PLAN_TOOL = "write_todos"
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
_LOGGER = logging.getLogger(__name__)


def publish_native_plan(state, identity, published):
    """Only project a paired, successful native update from the current run."""
    from src.core import jobs, tracing
    from src.core.progress_bus import emit_status

    if (state.get("active_run_id") != identity.run_id
            or (tracing.get_user_id(), tracing.get_session_id(), tracing.get_request_id(), tracing.get_job_id())
            != (identity.owner_user_id, identity.session_id, identity.user_turn_id, identity.run_id)):
        return
    tail = []
    for message in reversed(state["messages"]):
        if isinstance(message, AIMessage):
            break
        if isinstance(message, ToolMessage):
            tail.append(message)
    else:
        return
    calls = [call for call in message.tool_calls if call["name"] == NATIVE_PLAN_TOOL]
    todos = state.get("todos")
    if (len(calls) != 1 or not isinstance(todos, list) or calls[0]["args"].get("todos") != todos
            or any(message.additional_kwargs.get(k) != v for k, v in identity.message_metadata("assistant").items())):
        return
    cid = calls[0]["id"]
    if cid in published or not any(m.tool_call_id == cid and m.name == NATIVE_PLAN_TOOL and m.status == "success"
            and all(m.additional_kwargs.get(k) == v for k, v in identity.message_metadata("tool").items())
            for m in tail):
        return
    statuses = [item.get("status") if isinstance(item, dict) else None for item in todos]
    if any(not isinstance(status, str) or status not in _PLAN_STATUSES for status in statuses):
        return
    job = jobs.get_job(identity.run_id)
    if (not job or (job["owner_user_id"], job["session_id"], job["request_id"]) != identity.turn_key
            or job["status"] not in {"running", "detached"}):
        return
    steps = [{"id": f"todo:{identity.run_id}:{index}", "status": status}
             for index, status in enumerate(statuses[:12], 1)]
    # Only ordinal placeholders reach the user. DurableEventQueue adds the event cursor.
    try:
        emit_status("task_plan", plan_id=identity.run_id, steps=steps)
    except Exception as exc:
        _LOGGER.warning("task_plan_publication_failed kind=%s", type(exc).__name__)
        return
    published.add(cid)
