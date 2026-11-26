"""Bridge explicitly public native TodoList titles onto durable progress events."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
import hashlib
import logging
import re
import unicodedata

from langgraph.config import get_stream_writer
from langgraph.types import GraphOutput

PUBLIC_TODO_INSTRUCTIONS = ("Todos are private. An active title prefixed 公开步骤： is public. "
    "Use a generic action phrase of at most 80 characters in the user's language. No user text, "
    "names, IDs, queries, paths, code, results, secrets, reasoning or drafts. Omit the prefix "
    "if unsure. Never add a todo solely to announce progress.")

_PREFIX = "公开步骤："
_MAX_TITLE = 80
_TODO_STATUSES = ("pending", "in_progress", "completed")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NOTE_ID = re.compile(r"todo:[a-f0-9]{64}")
_PRIVATE_MARKER = re.compile(
    r"\b(?:i\s+(?:think|need|should|must|will|reason|have)|let me|chain of thought|"
    r"reasoning|thought process|system prompt|developer message|bearer|api key|"
    r"password|secret|token|private|draft|dsml|https?|sk[- ]\w+)\b"
    r"|我(?:认为|需要|应该|必须|先|将|会)|让我|思考过程|推理过程|思维链|草稿|内部指令|"
    r"系统提示|开发者消息|密码|密钥|访问令牌|\d{4,}|[A-Za-z0-9]{26,}", re.I)
_EVENT_KEYS = {"origin", "source", "visibility", "status", "note_id", "job_id", "text", "seq"}
_LOG = logging.getLogger(__name__)


def _title(value: str) -> str:
    """Require a bounded, plain action label. Reject rather than truncate or repair."""
    if not isinstance(value, str) or len(value) > _MAX_TITLE:
        return ""
    value = value.strip(" ")
    if len(value) < 2 or _PRIVATE_MARKER.search(value):
        return ""
    if any(not (unicodedata.category(char)[0] in "LM" or char in " -") for char in value):
        return ""
    if not any(char.isalpha() for char in value):
        return ""
    from src.core.pii_mask import mask_sensitive

    return value if mask_sensitive(value) == value else ""


def _source_copy(title: str, protected_text: tuple[str, ...]) -> bool:
    def normalize(value):
        return "".join(ch for ch in unicodedata.normalize("NFKC", value).casefold() if ch.isalpha())
    clean = normalize(title)
    return any(clean == source or len(clean) >= 8 and clean in source
               for value in protected_text if isinstance(value, str) and value
               if (source := normalize(value)))


def public_todo_title(todos, *, protected_text: tuple[str, ...] = ()) -> str:
    """Select only the one unambiguous in-progress label, never other todos."""
    if not isinstance(todos, list) or not 1 <= len(todos) <= 12:
        return ""
    if any(not isinstance(item, dict) or item.keys() != {"content", "status"}
           or not isinstance(item["content"], str) or item["status"] not in _TODO_STATUSES
           for item in todos):
        return ""
    active = [item["content"] for item in todos if item["status"] == "in_progress"]
    if len(active) != 1 or not active[0].startswith(_PREFIX):
        return ""
    title = _title(active[0][len(_PREFIX):])
    return title if title and not _source_copy(title, protected_text) else ""


def project_native_progress(data: dict) -> dict:
    """Accept the typed public event, never older host or model narration."""
    if (data.keys() - _EVENT_KEYS or data.get("origin") != "langgraph"
            or data.get("source") != "write_todos" or data.get("visibility") != "public"
            or not isinstance(data.get("job_id"), str) or not _ID.fullmatch(data["job_id"])
            or not isinstance(data.get("note_id"), str) or not _NOTE_ID.fullmatch(data["note_id"])):
        return {}
    if data.get("status") == "in_progress":
        title = _title(data.get("text"))
        if not title or title != data["text"]:
            return {}
    elif data.get("status") != "inactive" or "text" in data:
        return {}
    return {k: v for k, v in data.items() if k != "seq"}


@dataclass(frozen=True)
class _PublicTodoProgress:
    job_id: str
    native_update_id: str
    title: str

    def event(self) -> dict:
        status = "in_progress" if self.title else "inactive"
        title = {"text": self.title} if self.title else {}
        digest = hashlib.sha256(
            f"{self.job_id}\0{self.native_update_id}\0{status}\0{self.title}".encode()).hexdigest()
        return project_native_progress({"origin": "langgraph", "source": "write_todos",
            "visibility": "public", "job_id": self.job_id, "note_id": f"todo:{digest}",
            "status": status, **title})


def write_native_todo_progress(job_id: str, native_update_id: str, todos: list, *,
                               protected_text: tuple[str, ...]) -> None:
    """Call from the authorized graph node after the native todo commit."""
    title = public_todo_title(todos, protected_text=protected_text)
    get_stream_writer()(_PublicTodoProgress(job_id, native_update_id, title))


async def invoke_with_public_progress(graph, initial, config, *, context, job_id):
    """Match native ainvoke(v2) values/interrupts, plus opt-in root custom events."""
    from src.core.progress_bus import emit_progress

    latest, interrupts, active = None, [], ""
    async with aclosing(graph.astream(initial, config, context=context, durability="sync",
            stream_mode=["values", "custom"], output_keys=graph.output_channels, version="v2")) as events:
        async for part in events:
            if part["type"] == "values":
                latest = part["data"]
                interrupts.extend(part.get("interrupts", ()))
            elif part["type"] == "custom" and not part["ns"]:
                value = part["data"]
                if type(value) is not _PublicTodoProgress or value.job_id != job_id or value.title == active:
                    continue
                if event := value.event():
                    try:
                        emit_progress("assistant_progress", event)
                        active = value.title
                    except Exception as exc:
                        _LOG.warning("public_todo_progress_failed kind=%s", type(exc).__name__)
    return GraphOutput(value=latest, interrupts=tuple(interrupts))
