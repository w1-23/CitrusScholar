"""A2 admission/transfer layer for the durable independent chat turn queue.

Contract: ``docs/plans/8043-durable-turn-queue-contract.md`` (baseline
``624c252124a1efa5dcdda257fa366dc6f10ec664``).

This module is the narrow bridge between the authenticated HTTP layer and the
durable queue in ``src.core.chat_turn_queue``. It deliberately does *not*
import ``src.api.main``. The caller (Web host) has already authenticated the
owner, proven session ownership/activity, and owns the short in-process lock
that serialises session clear/delete/admit. This module then, synchronously and
in a worker thread:

* validates the bounded request (query / options / skill identities / ordered
  opaque file references) and enforces the existing file count and byte limits;
* registers uploads through the existing account file library, resolves
  library references by ownership only, and pins the complete ordered input
  set with the existing ``replace_resource_references`` writer transaction
  *before* the queue becomes visible (so a 202 means bytes, authorised refs and
  the pin are readable);
* admits one queue row through ``chat_turn_queue.enqueue_turn`` with a bounded
  payload that never contains raw bytes, Base64, tokens or host paths;
* treats an idempotent replay (same owner-level client id + same canonical
  fingerprint) as the original accepted fact, without resolving or repinning
  current library entries;
* distinguishes a same-bytes upload whose name/type changed, and changed
  library references or order, as a payload conflict;
* exposes an authorised public view and a dispatch-time rehydration that
  re-verifies the frozen SHA-256 of every input;
* exposes deterministic pin release for rejects/cancel without ever deleting
  inputs of an already accepted turn.

The queue's CAS state machine, sequence allocator, limits and error taxonomy
remain owned by ``src.core.chat_turn_queue``. HTTP adapters map its
``TurnQueueError`` subclasses (``to_http_exception`` is available as a helper).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

from src.core import chat_turn_queue
from src.session import files as session_files
from src.session import turn_input_staging as staging

logger = logging.getLogger(__name__)

# One deterministic resource id per accepted-or-staged turn input set. The
# account library pins the complete ordered attachment set under this id so a
# queued turn protects its bytes from TTL/cleanup while it waits.
RESOURCE_KIND = staging.RESOURCE_KIND
PAYLOAD_SCHEMA = 1

MAX_FILES = 8
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Standalone library upload/list retain the existing boundary and field names.
MAX_UPLOAD_BYTES = MAX_LIBRARY_REF_BYTES = MAX_ATTACHMENT_BYTES
MAX_QUERY_CHARS = 20000
MAX_NAME_CHARS = 180
MAX_REQUEST_ID_CHARS = 128

# The HTTP host accepts exactly this request-id alphabet. A1's repository
# validation is intentionally looser (it bounds length only), so admission is
# the boundary that guarantees a stored key always fits the public contract.
_CLIENT_REQUEST_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_FILE_ID_RE = re.compile(r"[a-f0-9]{32}")
_SHA256_RE = re.compile(r"[a-f0-9]{64}")

# Mirrors ``src.api.main._ALLOWED_UPLOAD_SUFFIXES`` without importing main.
_ALLOWED_UPLOAD_SUFFIXES = frozenset({
    "csv", "tsv", "xlsx", "xls", "xlsm", "xlsb", "pdf", "docx",
    "png", "jpg", "jpeg", "webp", "gif", "txt", "md", "json",
})

_PUBLIC_TURN_KEYS = (
    "session_id", "client_request_id", "queue_seq", "state", "generation",
    "active_child_job_id", "accepted_at", "dispatch_at", "started_at",
    "finished_at", "updated_at", "history_saved", "clear_reason", "blocked_by",
)

_HTTP_STATUS_BY_CODE = {
    chat_turn_queue.TurnNotFound.code: 404,
    chat_turn_queue.TurnQueueFull.code: 429,
    chat_turn_queue.TurnPayloadConflict.code: 409,
    chat_turn_queue.TurnNotQueued.code: 409,
    chat_turn_queue.TurnReconcileRequired.code: 409,
    chat_turn_queue.TurnQueueNotReady.code: 409,
    chat_turn_queue.TurnStateConflict.code: 409,
}
_HTTP_MESSAGE_BY_CODE = {
    chat_turn_queue.TurnPayloadConflict.code: "相同请求标识已用于不同内容。",
    chat_turn_queue.TurnQueueFull.code: "待处理消息已达上限，请稍后再试。",
    chat_turn_queue.TurnNotQueued.code: "消息已不在可取消的排队状态。",
    chat_turn_queue.TurnNotFound.code: "找不到此消息。",
}

__all__ = [
    "RESOURCE_KIND",
    "PAYLOAD_SCHEMA",
    "admit_turn",
    "load_dispatch_input",
    "present_turn",
    "release_turn_inputs",
    "turn_input_resource_id",
    "to_http_exception",
]


# ---------------------------------------------------------------------------
# Small helpers: deterministic identity, JSON, hashing.
# ---------------------------------------------------------------------------

def turn_input_resource_id(session_id: str, client_request_id: str) -> str:
    """Deterministic, bounded resource id for one turn's ordered inputs.

    It is stable across replay/retry and never exposes a host path. The
    account file library scopes pins by ``(owner, resource_kind, resource_id)``,
    so including the session and the owner-level client id is sufficient.
    """
    material = f"turn-input\x00{session_id}\x00{client_request_id}".encode("utf-8")
    return "turn-input:" + hashlib.sha256(material).hexdigest()[:32]


def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_path(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def to_http_exception(exc: chat_turn_queue.TurnQueueError) -> HTTPException:
    """Map an A1 queue error to the HTTP fact the Web adapter returns.

    Dispatch/cancel code paths may let ``TurnQueueError`` propagate; HTTP
    adapters that do not catch it centrally can call this helper instead.
    """
    code = getattr(exc, "code", "turn_queue_error")
    status = _HTTP_STATUS_BY_CODE.get(code, 409)
    message = _HTTP_MESSAGE_BY_CODE.get(
        code, "消息状态已变更，请刷新后重试。")
    return HTTPException(
        status_code=status, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# Request validation. Bad HTTP input becomes HTTPException here; durable queue
# facts stay as A1 ``TurnQueueError`` for the host to map.
# ---------------------------------------------------------------------------

def _http(status: int, message: str, *, code: str | None = None) -> HTTPException:
    detail = {"code": code, "message": message} if code else message
    return HTTPException(status_code=status, detail=detail)


def _optional_bool(value, name: str):
    if value is None:
        return None
    if type(value) is not bool:
        raise _http(400, f"{name} 参数无效。")
    return value


def _bool_or(value, name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise _http(400, f"{name} 参数无效。")
    return value


def _selected_skill_identities(value) -> list[dict[str, str]]:
    """Check request shape independently of the active release for idempotent replay."""
    if value is None:
        return []
    fields = {"id", "version", "sha256"}
    if (type(value) is not list or len(value) > 4
            or any(type(row) is not dict or set(row) != fields
                   or any(type(row[key]) is not str for key in fields) for row in value)):
        raise _http(409, "所选技能标识无效，请刷新技能目录后重新选择。", code="skill_selection_changed")
    return value


def _pin_selected_skill_release(selected: list[dict[str, str]]) -> dict[str, str]:
    from src.agent.installed_skills import active_skills, release_path, release_pin, validate_scenario_selection

    if not selected:
        return {}
    try:
        pin = release_pin(release_path())
        root = pin["native_skill_release"]
        validate_scenario_selection(selected, catalog=active_skills(Path(root)) if root else [])
        return pin
    except (OSError, ValueError, KeyError) as exc:
        raise _http(409, "所选技能已变更或不可用，请刷新技能目录后重新选择。",
                    code="skill_selection_changed") from exc


def _validate_request(*, request, owner_user_id, session_id,
                      account_epoch) -> dict:
    if type(owner_user_id) is not int or owner_user_id <= 0:
        raise _http(400, "账号标识无效。")
    if not isinstance(session_id, str) or not session_files.valid_session(session_id):
        raise _http(400, "会话标识无效。")
    if type(account_epoch) is not int or account_epoch < 0:
        raise _http(400, "账号代次无效。")
    if not isinstance(request, dict):
        raise _http(400, "请求内容无效。")
    requested_session = request.get("session_id")
    if requested_session not in (None, "", session_id):
        raise _http(400, "会话标识不一致。")

    client_request_id = request.get("client_request_id")
    if (not isinstance(client_request_id, str)
            or not _CLIENT_REQUEST_RE.fullmatch(client_request_id)):
        raise _http(400, "请求标识无效。")

    query = request.get("query")
    # Pure whitespace is not a valid user body; attachments without a caption
    # are rejected here and the client keeps the selection and explains why.
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise _http(400, "问题内容为空或过长。")

    baseline = request.get("continue_from")
    if baseline is not None and (not isinstance(baseline, str) or not _CLIENT_REQUEST_RE.fullmatch(baseline)
                                 or baseline == client_request_id):
        raise _http(400, "续问来源无效。")

    options = {
        "light_mode": _optional_bool(request.get("light_mode"), "light_mode"),
        "web_search_enabled": _optional_bool(
            request.get("web_search_enabled"), "web_search_enabled"),
        "visualization_enabled": _bool_or(
            request.get("visualization_enabled"), "visualization_enabled", False),
        "context_compression": _bool_or(
            request.get("context_compression"), "context_compression", True),
    }
    if _bool_or(request.get("browse_requested"), "browse_requested", False):
        options["browse_requested"] = True
    selected_skills = _selected_skill_identities(request.get("selected_skills"))

    attachments = request.get("attachments") or []
    file_refs = request.get("file_refs") or []
    if not isinstance(attachments, list) or not isinstance(file_refs, list):
        raise _http(400, "附件或引用参数无效。")
    if len(attachments) > MAX_FILES or len(file_refs) > MAX_FILES:
        raise _http(413, "每轮最多上传或引用 8 个文件。")
    if len(attachments) + len(file_refs) > MAX_FILES:
        raise _http(413, "每轮最多上传或引用 8 个文件。")
    if any(not isinstance(item, dict) for item in attachments):
        raise _http(400, "附件参数无效。")
    if any(not isinstance(item, (str, int)) or isinstance(item, bool)
           for item in file_refs):
        raise _http(400, "引用参数无效。")

    return {
        "client_request_id": client_request_id,
        "query": query,
        "options": options,
        "selected_skills": selected_skills,
        "attachments": attachments,
        "file_refs": [str(item or "") for item in file_refs],
        "continue_from": baseline,
    }


def _decode_upload(item: dict, *, remaining: int) -> tuple[str, str, bytes, str]:
    """Validate one in-memory upload without persisting anything yet."""
    name = str(item.get("name") or "attachment")[:MAX_NAME_CHARS]
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise _http(400, f"附件格式暂不支持：{name}")
    raw = str(item.get("data") or "")
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    if len(raw) > 4 * ((remaining + 2) // 3):
        raise _http(413, "本轮附件总大小不能超过 20 MiB。")
    try:
        content = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - normalize decoder failures
        raise _http(400, f"附件内容无法读取：{name}") from exc
    if len(content) > remaining:
        raise _http(413, "本轮附件总大小不能超过 20 MiB。")
    mime = str(item.get("type") or "")
    return name, mime, content, _sha256_bytes(content)


def _resolve_library_refs(file_ids: list[str], owner_user_id: int) -> list[dict]:
    """Resolve first-seen opaque account-file ids by ownership for new turns."""
    resolved: list[dict] = []
    for file_id in file_ids:
        info = session_files.get_account_file(file_id, owner_user_id)
        if not info:
            raise _http(404, "引用的资料不存在、已过期或不属于当前账号。")
        try:
            info["size_bytes"] = Path(info["path"]).stat().st_size
        except OSError as exc:
            raise _http(404, "引用的资料不存在、已过期或不属于当前账号。") from exc
        resolved.append(info)
    return resolved


def _input_metadata(info: dict, *, source: str) -> dict:
    file_id = str(info.get("file_id") or "")
    sha256 = str(info.get("sha256") or "")
    if not _FILE_ID_RE.fullmatch(file_id) or not _SHA256_RE.fullmatch(sha256):
        raise _http(503, "资料库返回了无效的文件标识。")
    return {
        "file_id": file_id,
        "name": str(info.get("filename") or "attachment")[:MAX_NAME_CHARS],
        "type": str(info.get("mime_type") or "application/octet-stream")[:200],
        "size_bytes": int(info.get("size_bytes") or 0),
        "sha256": sha256,
        "origin": str(info.get("origin") or ""),
        "source": source,
    }


def _request_fingerprint(*, session_id: str, query: str, options: dict,
                         selected_skills: list[dict], uploads: list[tuple],
                         file_refs: list[str], continue_from: str | None) -> str:
    """Pre-registration identity of the *client's* request.

    Uploads are identified by requested name/type plus immutable byte SHA-256,
    so the same bytes with a changed name/type, a changed query/option/skill,
    or a changed ordered library-reference set is a different request. No
    generated file id participates here, which is what lets a replay be
    recognised before any new registration happens.
    """
    material = {
        "schema": PAYLOAD_SCHEMA,
        "session_id": session_id,
        "query": query,
        "options": options,
        "selected_skills": selected_skills,
        "uploads": [
            {"name": name, "type": mime, "sha256": sha}
            for name, mime, _content, sha in uploads
        ],
        "file_refs": file_refs,
    }
    # Ordinary request fingerprints remain compatible with existing receipts.
    if continue_from is not None:
        material["continue_from"] = continue_from
    try:
        return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as exc:
        raise _http(400, "请求内容无法规范化。") from exc


def _stored_fingerprint_matches(payload: dict, *, session_id: str,
                                computed: str) -> bool:
    stored = payload.get("request_fingerprint")
    if isinstance(stored, str) and _SHA256_RE.fullmatch(stored):
        return stored == computed
    # Fallback for a row written before the fingerprint field existed. Rebuild
    # the same material from the frozen inputs; generated ids never vote here.
    inputs = payload.get("inputs") or []
    uploads, file_refs = [], []
    for item in inputs:
        if not isinstance(item, dict):
            return False
        if item.get("source") == "upload":
            uploads.append({
                "name": item.get("name"),
                "type": item.get("type"),
                "sha256": item.get("sha256"),
            })
        elif item.get("source") == "library":
            file_refs.append(str(item.get("file_id") or ""))
        elif item.get("source") != "previous_turn":
            return False
    material = {
        "schema": payload.get("schema", PAYLOAD_SCHEMA),
        "session_id": session_id,
        "query": payload.get("query"),
        "options": payload.get("options") or {},
        "selected_skills": payload.get("selected_skills") or [],
        "uploads": uploads,
        "file_refs": file_refs,
    }
    try:
        rebuilt = hashlib.sha256(
            _canonical_json(material).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return False
    return rebuilt == computed


# ---------------------------------------------------------------------------
# Admission.
# ---------------------------------------------------------------------------

def _register_upload(*, session_id: str, owner_user_id: int, name: str,
                     mime: str, content: bytes,
                     client_request_id: str, stage_turn_input: bool = False) -> dict:
    try:
        return session_files.register_file(
            session_id=session_id, owner_user_id=owner_user_id,
            filename=name, content=content, mime=mime,
            origin=session_files.ORIGIN_UPLOADED,
            client_request_id=client_request_id, stage_turn_input=stage_turn_input,
        )
    except session_files.StorageQuotaExceeded as exc:
        raise _http(413, "账号资料库容量不足，附件未能保存。") from exc
    except ValueError as exc:
        raise _http(400, f"附件无法保存：{name}") from exc
    except Exception as exc:  # noqa: BLE001 - host storage outage
        logger.warning("turn queue upload registration failed: %s", type(exc).__name__)
        raise _http(503, "附件未能保存到账号资料库，请稍后重试。") from exc


def _existing_turn(*, owner_user_id: int, session_id: str,
                   client_request_id: str):
    return chat_turn_queue.get_turn(
        owner_user_id=owner_user_id, session_id=session_id,
        client_request_id=client_request_id)


def _replay_existing(turn: dict, *, session_id: str, fingerprint: str) -> dict:
    """Return the stored admission fact. Current bytes are checked at dispatch."""
    payload = turn.get("bounded_payload")
    if not isinstance(payload, dict) or not _stored_fingerprint_matches(
            payload, session_id=session_id, computed=fingerprint):
        raise chat_turn_queue.TurnPayloadConflict(
            "turn_payload_conflict", job_id=turn.get("job_id"))
    return {**turn, "created": False}


class _AdmissionBusy(RuntimeError):
    pass


@contextmanager
def _admission_lock(owner, request):
    # Bounded lockfile set. The owner-level id is unique across sessions and
    # the OS releases the scope lock after a stopped or killed process.
    from src.agent.harness import execution_lock
    stripe = hashlib.sha256(f"{owner}:{request}".encode()).hexdigest()[:3]
    try:
        with execution_lock(session_files.DB_PATH.parent / "turn-input-locks", stripe):
            yield
    except RuntimeError as exc:
        if str(exc) != "agent_run_already_active":
            raise
        raise _AdmissionBusy from exc


def _resolve_stage(scope):
    owner, session, request = scope
    accepted = _existing_turn(owner_user_id=owner, session_id=session, client_request_id=request)
    staging.resolve(scope, turn_input_resource_id(session, request), accepted=accepted is not None)


def reconcile_staged_turn_inputs():
    """Startup/periodic repair. Never compensate against an uncertain queue read."""
    cursor = 0
    while rows := staging.pending(after=cursor):
        for cursor, owner, session, request in rows:
            try:
                with _admission_lock(owner, request):
                    _resolve_stage((owner, session, request))
            except _AdmissionBusy:
                continue
            except Exception as exc:
                logger.warning("staged turn input reconciliation deferred: %s", type(exc).__name__)


def _continued_inputs(owner_user_id: int, session_id: str, baseline: str) -> list[dict]:
    try:
        previous = chat_turn_queue.continuation_baseline(owner_user_id=owner_user_id, session_id=session_id,
                                                        client_request_id=baseline)
    except chat_turn_queue.TurnStateConflict as exc:
        raise _http(409, "指定的续问来源尚未形成可验证的历史记录。", code="turn_baseline_not_ready") from exc
    if previous is None:
        raise _http(404, "找不到指定的续问来源。", code="turn_baseline_not_found")
    payload = _load_payload(previous)
    items = payload.get("inputs")
    if not isinstance(items, list):
        raise _http(409, "续问来源的附件无法验证。", code="turn_baseline_unavailable")
    inherited = []
    for item in items:
        unavailable = _http(409, "续问来源的附件已失效或变更。", code="turn_baseline_unavailable")
        if not isinstance(item, dict):
            raise unavailable
        file_id, expected = str(item.get("file_id") or ""), str(item.get("sha256") or "")
        if not _FILE_ID_RE.fullmatch(file_id) or not _SHA256_RE.fullmatch(expected):
            raise unavailable
        info = session_files.get_account_file(file_id, owner_user_id)
        if not info or info.get("sha256") != expected or not info.get("path"):
            raise unavailable
        try:
            info["size_bytes"] = Path(info["path"]).stat().st_size
        except OSError as exc:
            raise unavailable from exc
        # The transactional pin verifies actual bytes against this exact hash.
        inherited.append(_input_metadata(info, source="previous_turn"))
    return inherited


def admit_turn(*, request: dict, owner_user_id: int, session_id: str,
               account_epoch: int, request_id: str,
               limits: chat_turn_queue.QueueLimits | None = None,
               enforce_rate: bool = True) -> dict:
    """Synchronously admit one independent turn through ``run_account_thread``.

    ``request`` is ``ChatRequest.model_dump(mode='json')`` with an explicit
    ``client_request_id`` and ``session_id``. The caller owns authentication,
    session-ownership/activity checks and the short in-process lock around
    clear/delete/admit. Returns the A1 queue view (with ``created``) or the
    original view on an idempotent replay.

    Bad HTTP input raises ``HTTPException`` (400/404/409/413/429/503). Queue
    facts raise A1 ``TurnQueueError`` subclasses so the host maps them with its
    own reason codes; ``to_http_exception`` is available for other adapters.
    """
    req = _validate_request(
        request=request, owner_user_id=owner_user_id, session_id=session_id,
        account_epoch=account_epoch)
    if not isinstance(request_id, str) or len(request_id) > MAX_REQUEST_ID_CHARS:
        raise _http(400, "服务端请求标识无效。")
    client_request_id = req["client_request_id"]

    uploads = []
    upload_bytes = 0
    for item in req["attachments"]:
        upload = _decode_upload(item, remaining=MAX_ATTACHMENT_BYTES - upload_bytes)
        upload_bytes += len(upload[2])
        uploads.append(upload)
    file_refs = list(dict.fromkeys(req["file_refs"]))
    baseline = req["continue_from"]
    fingerprint = _request_fingerprint(
        session_id=session_id, query=req["query"], options=req["options"],
        selected_skills=req["selected_skills"], uploads=uploads, file_refs=file_refs, continue_from=baseline)

    scope = (owner_user_id, session_id, client_request_id)
    def existing():
        return chat_turn_queue.admission_receipt(owner_user_id=owner_user_id, session_id=session_id,
                                                 client_request_id=client_request_id)

    # A receipt proves historical acceptance, not current file authorization.
    if prior := existing():
        return _replay_existing(prior, session_id=session_id, fingerprint=fingerprint)
    try:
        with _admission_lock(owner_user_id, client_request_id):
            if prior := existing():
                return _replay_existing(prior, session_id=session_id, fingerprint=fingerprint)
            return _admit_new_turn(scope, req, baseline=baseline, uploads=uploads, upload_bytes=upload_bytes,
                                   file_refs=file_refs, fingerprint=fingerprint, request_id=request_id,
                                   account_epoch=account_epoch, limits=limits, enforce_rate=enforce_rate)
    except _AdmissionBusy as exc:
        raise _http(503, "消息正等待受理，请使用相同请求标识重试。", code="turn_admission_busy") from exc


def _admit_new_turn(scope, req, *, baseline, uploads, upload_bytes, file_refs, fingerprint,
                    request_id, account_epoch, limits, enforce_rate):
    owner_user_id, session_id, client_request_id = scope
    resource_id = turn_input_resource_id(session_id, client_request_id)
    # Retry any unaccepted attempt first, even if the new request has no files.
    staging.resolve(scope, resource_id, accepted=False)
    inherited = _continued_inputs(owner_user_id, session_id, baseline) if baseline is not None else []
    inherited_ids = {item["file_id"] for item in inherited}
    refs = _resolve_library_refs([file_id for file_id in file_refs if file_id not in inherited_ids], owner_user_id)
    inputs = inherited + [_input_metadata(info, source="library") for info in refs]
    if len(uploads) + len(inputs) > MAX_FILES:
        raise _http(413, "每轮最多上传或引用 8 个文件。")
    attachment_bytes = upload_bytes + sum(item["size_bytes"] for item in inputs)
    if attachment_bytes > MAX_ATTACHMENT_BYTES:
        raise _http(413, "本轮附件总大小不能超过 20 MiB。")
    skill_pin = _pin_selected_skill_release(req["selected_skills"])
    if enforce_rate:
        _enforce_rate_limit(owner_user_id)

    has_files = bool(uploads or inputs)
    if has_files:
        staging.begin(scope)
    try:
        upload_inputs = []
        for name, mime, content, _sha in uploads:
            info = _register_upload(session_id=session_id, owner_user_id=owner_user_id,
                name=name, mime=mime, content=content, client_request_id=client_request_id,
                stage_turn_input=True)
            upload_inputs.append(_input_metadata(info, source="upload"))
        inputs = inherited + upload_inputs + inputs[len(inherited):]
        bounded_payload = {
            "schema": PAYLOAD_SCHEMA, "request_fingerprint": fingerprint, "query": req["query"],
            "options": req["options"], "selected_skills": req["selected_skills"], **skill_pin,
            "inputs": inputs, "attachment_bytes": attachment_bytes,
        }
        if baseline is not None:
            bounded_payload["continue_from"] = baseline
        if has_files:
            try:
                staging.pin(scope, resource_id, inputs)
            except ValueError as exc:
                raise _http(409, "附件已失效或变更。", code="turn_input_unavailable") from exc
        turn = chat_turn_queue.enqueue_turn(owner_user_id=owner_user_id, session_id=session_id,
            client_request_id=client_request_id, bounded_payload=bounded_payload, request_id=request_id,
            attachment_bytes=attachment_bytes, account_epoch=account_epoch, limits=limits)
    except Exception:
        if has_files:
            try:
                _resolve_stage(scope)
            except Exception as exc:
                logger.warning("staged turn input compensation deferred: %s", type(exc).__name__)
        raise
    if has_files:
        try:
            staging.resolve(scope, resource_id, accepted=True)
        except Exception as exc:
            # Committed receipt and pins are already complete. Restart repair
            # forgets the stage without ever unpinning this accepted turn.
            logger.warning("staged turn input handoff deferred: %s", type(exc).__name__)
    return turn


def _enforce_rate_limit(owner_user_id: int) -> None:
    """Existing burst limit; idempotent replays never reach this point.

    Execution/daily quota is deliberately not reserved here: the contract says
    a queued (or cancelled-queued) turn does not consume an execution. The
    dispatcher/consumer reserves capacity at claim/dispatch time.
    """
    from src.auth import check_rate_limit
    if not check_rate_limit("chat", str(owner_user_id),
                            limit=20, window_seconds=60):
        raise _http(429, "请求过于频繁，请稍后再试。")


# ---------------------------------------------------------------------------
# Dispatch-time rehydration.
# ---------------------------------------------------------------------------

def _load_payload(turn: dict) -> dict:
    payload = turn.get("bounded_payload")
    if isinstance(payload, dict):
        return payload
    owner_user_id = turn.get("owner_user_id")
    session_id = turn.get("session_id")
    client_request_id = turn.get("client_request_id")
    if (type(owner_user_id) is not int or owner_user_id <= 0
            or not isinstance(session_id, str)
            or not isinstance(client_request_id, str)):
        raise chat_turn_queue.TurnNotFound(
            "turn_not_found", job_id=turn.get("job_id"))
    refreshed = chat_turn_queue.get_turn(
        owner_user_id=owner_user_id, session_id=session_id,
        client_request_id=client_request_id)
    if refreshed is None:
        raise chat_turn_queue.TurnNotFound(
            "turn_not_found", job_id=turn.get("job_id"))
    payload = refreshed.get("bounded_payload")
    if not isinstance(payload, dict):
        raise chat_turn_queue.TurnReconcileRequired(
            "turn_payload_missing", job_id=turn.get("job_id"))
    return payload


def load_dispatch_input(turn: dict) -> tuple[dict, list[dict]]:
    """Rehydrate the validated ChatRequest input and verify frozen input hashes.

    Returns ``(request_dict, file_metadata)``:

    * ``request_dict`` has the same fields as a validated ``ChatRequest`` with
      ``attachments=[]`` and ``file_refs`` listing every registered input id in
      original order (uploads first, then library references).
    * ``file_metadata`` is the public per-file metadata after re-reading each
      object and matching its bytes against the frozen SHA-256.

    The caller invokes this under a fresh account lease when dispatching. No
    host path is returned. A vanished/changed input raises
    ``TurnReconcileRequired`` rather than silently re-registering or replaying.
    """
    if not isinstance(turn, dict):
        raise ValueError("turn must be a mapping")
    owner_user_id = turn.get("owner_user_id")
    session_id = turn.get("session_id")
    client_request_id = turn.get("client_request_id")
    if (type(owner_user_id) is not int or owner_user_id <= 0
            or not isinstance(session_id, str)
            or not isinstance(client_request_id, str)):
        raise ValueError("turn identity is invalid")
    payload = _load_payload(turn)
    query = payload.get("query")
    if not isinstance(query, str) or not query:
        raise chat_turn_queue.TurnReconcileRequired(
            "turn_payload_invalid", job_id=turn.get("job_id"))
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    selected_skills = payload.get("selected_skills") or []
    if not isinstance(selected_skills, list):
        raise chat_turn_queue.TurnReconcileRequired(
            "turn_payload_invalid", job_id=turn.get("job_id"))

    metadata: list[dict] = []
    file_ids: list[str] = []
    for item in payload.get("inputs") or []:
        if not isinstance(item, dict):
            raise chat_turn_queue.TurnReconcileRequired(
                "turn_payload_invalid", job_id=turn.get("job_id"))
        file_id = str(item.get("file_id") or "")
        expected = str(item.get("sha256") or "")
        info = session_files.get_account_file(file_id, owner_user_id)
        legacy_implicit = item.get("source") == "previous_turn" and "continue_from" not in payload
        if not info:
            if legacy_implicit:
                continue
            raise chat_turn_queue.TurnReconcileRequired(
                "dispatch_input_missing", job_id=turn.get("job_id"))
        path = info.get("path")
        actual = _sha256_path(Path(path)) if path else None
        if (actual is None or actual != expected
                or str(info.get("sha256")) != expected):
            if legacy_implicit:
                continue
            raise chat_turn_queue.TurnReconcileRequired(
                "dispatch_input_checksum_changed", job_id=turn.get("job_id"))
        file_ids.append(file_id)
        metadata.append({
            "file_id": file_id,
            "filename": str(info.get("filename") or "attachment"),
            "mime_type": str(info.get("mime_type") or "application/octet-stream"),
            "size_bytes": int(info.get("size_bytes") or 0),
            "sha256": expected,
            "origin": str(info.get("origin") or ""),
            "source": str(item.get("source") or "library"),
            "kind": str(info.get("kind") or ""),
        })

    from src.agent.installed_skills import stored_release_pin

    request_dict = {
        "session_id": session_id,
        "query": query,
        "client_request_id": client_request_id,
        "light_mode": options.get("light_mode"),
        "web_search_enabled": options.get("web_search_enabled"),
        "visualization_enabled": bool(options.get("visualization_enabled", False)),
        "context_compression": bool(options.get("context_compression", True)),
        "browse_requested": options.get("browse_requested") is True,
        "selected_skills": selected_skills,
        **stored_release_pin(payload),
        "attachments": [],
        "file_refs": file_ids,
    }
    return request_dict, metadata


# ---------------------------------------------------------------------------
# Public view.
# ---------------------------------------------------------------------------

def _public_attachments(inputs) -> list[dict]:
    result = []
    for item in inputs or []:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "")
        if not _FILE_ID_RE.fullmatch(file_id):
            continue
        result.append({
            "file_id": file_id,
            "filename": item.get("name"),
            "mime_type": item.get("type"),
            "size_bytes": int(item.get("size_bytes") or 0),
            "sha256": item.get("sha256"),
            "origin": item.get("origin"),
            "source": item.get("source"),
            "download_url": f"/api/v2/files/{file_id}/download",
            "preview_url": f"/api/v2/files/{file_id}/preview",
        })
    return result


def present_turn(turn: dict, *, include_content: bool = False) -> dict:
    """Allow-listed public turn view; never exposes owner or stored payload.

    ``include_content`` re-reads the bounded payload under the turn's own
    internal owner/session scope when the caller's row came from a list/find
    query, then adds the original query text and per-turn public attachment
    metadata for authorised queue read/recovery/history.
    """
    if not isinstance(turn, dict):
        raise ValueError("turn must be a mapping")
    view = {key: turn.get(key) for key in _PUBLIC_TURN_KEYS}
    job_id = str(turn.get("job_id") or turn.get("root_job_id") or "")
    view["job_id"] = job_id
    view["queue_position"] = int(turn.get("queue_position") or 0)
    view["status_url"] = f"/jobs/{job_id}" if job_id else ""
    view["events_url"] = f"/jobs/{job_id}/events" if job_id else ""
    for key in ("dispatch_at", "started_at", "finished_at"):
        if not view.get(key):
            view[key] = None

    if include_content:
        payload = turn.get("bounded_payload")
        if not isinstance(payload, dict):
            owner_user_id = turn.get("owner_user_id")
            session_id = turn.get("session_id")
            client_request_id = turn.get("client_request_id")
            if (type(owner_user_id) is int and owner_user_id > 0
                    and isinstance(session_id, str)
                    and isinstance(client_request_id, str)):
                try:
                    refreshed = chat_turn_queue.get_turn(
                        owner_user_id=owner_user_id, session_id=session_id,
                        client_request_id=client_request_id)
                except Exception:  # noqa: BLE001 - view must stay bounded
                    refreshed = None
                if refreshed is not None:
                    payload = refreshed.get("bounded_payload")
        if isinstance(payload, dict):
            view["query"] = str(payload.get("query") or "")
            view["attachments"] = _public_attachments(payload.get("inputs") or [])
    return view


# ---------------------------------------------------------------------------
# Cleanup / cancellation.
# ---------------------------------------------------------------------------

def release_turn_inputs(owner_user_id: int, session_id: str,
                        client_request_id: str) -> int:
    """Release one turn's input pin; never delete an accepted turn's inputs.

    Returns the number of removed pins (idempotent). A still-active
    (non-terminal) turn keeps its pin. If no accepted turn exists for this
    owner-level id, any uploads staged for the request that are still unpinned
    and still belong to this session are removed as orphan cleanup. For an
    accepted (including cancelled/terminal) turn the bytes stay in the account
    library; only the queue's protection pin is released.
    """
    if type(owner_user_id) is not int or owner_user_id <= 0:
        raise ValueError("owner_user_id must be a positive integer")
    if not isinstance(session_id, str) or not session_files.valid_session(session_id):
        raise ValueError("session_id is invalid")
    if (not isinstance(client_request_id, str)
            or not _CLIENT_REQUEST_RE.fullmatch(client_request_id)):
        raise ValueError("client_request_id is invalid")

    try:
        accepted = _existing_turn(
            owner_user_id=owner_user_id, session_id=session_id,
            client_request_id=client_request_id)
    except Exception:  # noqa: BLE001 - never mutate on an unsure read
        logger.exception("turn queue release could not confirm acceptance")
        return 0
    if (accepted is not None
            and accepted.get("state") in chat_turn_queue.NONTERMINAL_STATES):
        # The turn can still be claimed/dispatched; its inputs must stay
        # protected. Cancellation transitions to terminal before cleanup.
        return 0

    removed = session_files.release_resource_references(
        owner_user_id, RESOURCE_KIND,
        turn_input_resource_id(session_id, client_request_id))
    if accepted is not None:
        return removed

    grouped = session_files.list_turn_files(
        session_id, owner_user_id, [client_request_id])
    for info in grouped.get(client_request_id, []):
        try:
            if str(info.get("origin") or "") != session_files.ORIGIN_UPLOADED:
                continue
            source_session = str(
                info.get("source_session_id") or info.get("session_id") or "")
            if source_session != session_id:
                continue
            session_files.delete_account_file(
                str(info.get("file_id") or ""), owner_user_id)
        except Exception:  # noqa: BLE001
            logger.warning("turn queue release skipped orphan file",
                           exc_info=True)
    return removed
