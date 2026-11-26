"""Project committed SDK todos to the owner-scoped public task timeline."""
import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

NATIVE_PLAN_TOOL = "write_todos"
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})
_LOGGER = logging.getLogger(__name__)


def publish_native_plan(state, identity, published):
    """Only project a paired, successful native update from the current run."""
    from src.core import jobs, tracing
    from src.core.progress_bus import emit_status
    from src.core.public_progress import write_native_todo_progress

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
    # The official custom stream carries the opt-in active title and never
    # the raw ToolMessage. Keep the separate structured plan status event.
    original = (state.get("host_state") or {}).get("original_request_state") or {}
    wake = state.get("user_input_wake") or {}
    protected_text = (original.get("query"), wake.get("text"), *(
        message.content for message in state["messages"] if isinstance(message, HumanMessage)
        and message.additional_kwargs.get("run_id") == identity.run_id
        and isinstance(message.content, str)))
    try:
        write_native_todo_progress(identity.run_id, cid, todos, protected_text=protected_text)
        emit_status("task_plan", plan_id=identity.run_id, steps=steps)
    except Exception as exc:
        _LOGGER.warning("task_plan_publication_failed kind=%s", type(exc).__name__)
        return
    published.add(cid)
