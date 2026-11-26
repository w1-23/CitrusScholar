"""Product question identity and durable answers for LangGraph interrupts."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from src.core.task_events import TaskConflict, _append, _job, canonical, current_terminal_event, input_hash, read_transaction, recovery_cursor, transaction


class QuestionConflict(TaskConflict):
    """HTTP adapters map this to 409; missing owner/session maps to 404."""


class DatasetIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: StrictStr
    id: StrictStr
    version: StrictInt
    sha256: StrictStr


class FieldReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: StrictStr
    dataset_ref: DatasetIdentity | None
    file_id: StrictStr | None
    sha256: StrictStr | None
    sheet: StrictStr | None
    column: StrictStr

    @model_validator(mode="after")
    def complete_reference(self):
        if not self.column.strip():
            raise ValueError("column must name a real schema field")
        if self.kind == "dataset_field":
            if self.dataset_ref is None or self.dataset_ref.kind != "dataset_snapshot":
                raise ValueError("dataset_field requires a dataset_snapshot identity")
            if any(x is not None for x in (self.file_id, self.sha256, self.sheet)):
                raise ValueError("dataset_field cannot contain source identity")
        elif self.kind == "source_field":
            if self.dataset_ref is not None or not self.file_id or not self.sha256 or self.sheet is None:
                raise ValueError("source_field requires file_id, sha256 and sheet")
        else:
            raise ValueError("unknown field reference kind")
        return self


class QuestionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: StrictStr = Field(min_length=1, max_length=64)
    label: StrictStr = Field(min_length=1, max_length=200)
    description: StrictStr = Field(max_length=1000)
    refs: list[FieldReference] = Field(default_factory=list, max_length=20)


class UserQuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: StrictStr = Field(min_length=1, max_length=2000)
    options: list[QuestionOption] = Field(max_length=8)
    multi_select: StrictBool
    allow_text: StrictBool

    @model_validator(mode="after")
    def choices(self):
        if not self.prompt.strip() or (not self.options and not self.allow_text):
            raise ValueError("question needs a prompt and a way to answer")
        if len({x.id for x in self.options}) != len(self.options):
            raise ValueError("option ids must be unique")
        return self


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: StrictInt = Field(ge=1)
    interrupt_id: StrictStr = Field(default="", max_length=64)
    selected_option_ids: list[StrictStr] = Field(default_factory=list, max_length=8)
    text: StrictStr = Field(default="", max_length=8000)
    cancelled: StrictBool = False
    client_request_id: StrictStr = Field(min_length=1, max_length=64)


class QuestionResume(BaseModel):
    """Only an authenticated, accepted answer may enter the suspended node."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["user_input"]
    question_id: StrictStr
    version: StrictInt = Field(ge=1)
    interrupt_id: StrictStr
    selected_option_ids: list[StrictStr]
    selected_labels: list[StrictStr]
    text: StrictStr


def init_schema(conn):
    def create():
        conn.execute("""CREATE TABLE IF NOT EXISTS task_questions (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
            session_id TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
            question TEXT NOT NULL, context TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
            answer_hash TEXT NOT NULL DEFAULT '', client_request_id TEXT NOT NULL DEFAULT '',
            child_job_id TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            expires_at REAL NOT NULL, pin_until REAL NOT NULL DEFAULT 0,
            recovery_cursor INTEGER NOT NULL DEFAULT 0, UNIQUE(job_id,recovery_cursor))""")

    columns = [row[1] for row in conn.execute("PRAGMA table_info(task_questions)")]
    if columns and "recovery_cursor" not in columns:
        # Before this schema a job could only ask before its first done event.
        conn.execute("ALTER TABLE task_questions RENAME TO task_questions_before_generations")
        create()
        fields = ",".join(columns)
        conn.execute(f"INSERT INTO task_questions({fields}) SELECT {fields} FROM task_questions_before_generations")
        conn.execute("DROP TABLE task_questions_before_generations")
    elif not columns:
        create()
    migrated = {row[1] for row in conn.execute("PRAGMA table_info(task_questions)")}
    for field, definition in (("interrupt_id", "TEXT NOT NULL DEFAULT ''"),
                              ("checkpoint_ref", "TEXT NOT NULL DEFAULT ''"),
                              ("resume_cursor", "INTEGER NOT NULL DEFAULT 0"),
                              ("ack_checkpoint_ref", "TEXT NOT NULL DEFAULT ''")):
        if field not in migrated:
            conn.execute(f"ALTER TABLE task_questions ADD COLUMN {field} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_questions_expiry ON task_questions(status,expires_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_questions_child ON task_questions(child_job_id) WHERE child_job_id<>''")


def _authorized(conn, job_id, owner_user_id, session_id):
    job = _job(conn, job_id)
    if type(owner_user_id) is not int or owner_user_id <= 0 or job["owner_user_id"] != owner_user_id or job["session_id"] != session_id:
        raise QuestionConflict("question_not_found")
    return job


def _view(row):
    return json.loads(row["question"]) | {"id": row["id"], "job_id": row["job_id"],
        "version": row["version"], "status": row["status"], "expires_at": row["expires_at"],
        "recovery_cursor": row["recovery_cursor"], "interrupt_id": row["interrupt_id"],
        "child_job_id": row["child_job_id"],
        "answer": json.loads(row["answer"]) if row["answer"] else None}


def validate_question_refs(options, owner, context):
    """Only schema identities loaded by the host may authorize field options."""
    trusted = (context or {}).get("source_refs") or []
    for option in options:
        for ref in option["refs"]:
            if ref["kind"] == "dataset_field":
                from src.research.analysis_results import load_dataset_snapshot
                snapshot = load_dataset_snapshot(owner, ref["dataset_ref"])
                if ref["column"] not in snapshot.to_dict()["columns"]:
                    raise ValueError("field_not_in_dataset_schema")
            else:
                if not any(source.get("file_id") == ref["file_id"]
                           and source.get("sha256") == ref["sha256"]
                           and str(source.get("sheet", "")) == ref["sheet"]
                           and ref["column"] in source.get("columns", [])
                           for source in trusted if isinstance(source, dict)):
                    raise ValueError("field_not_in_trusted_source_schema")
                from src.session.files import get_account_file
                # The exact identity/schema was authorized by host refs above;
                # a model-supplied internal file_id alone never reaches this read.
                source = get_account_file(ref["file_id"], owner, allow_internal=True)
                if not source or source.get("sha256") != ref["sha256"]:
                    raise ValueError("source_version_changed")


def _script_refs(context):
    refs = list(context.get("script_refs") or [])
    for ref in (context.get("context_working_state") or {}).get("script_refs") or []:
        if ref not in refs:
            refs.append(ref)
    return refs


def _source_refs(context):
    """The same host-carried file identities must be validated and pinned."""
    working = context.get("context_working_state") or {}
    return (list(context.get("source_refs") or [])
            + list(context.get("attachment_refs") or [])
            + list(working.get("source_refs") or []))


def _has_inputs(context):
    return bool(_source_refs(context) or context.get("dataset_refs") or _script_refs(context)
                or (context.get("context_working_state") or {}).get("dataset_refs"))


def _pin_scripts(identity, owner, session_id, context, previous_refs=()):
    from src.research import script_assets
    refs = _script_refs(context)
    # Validate the complete host-scoped set before adding any new dependency.
    for ref in refs:
        script_assets.get_script(owner, session_id, ref)
    added = []
    try:
        for ref in refs:
            script_assets.pin_script(owner, session_id, ref, "user_question", identity)
            if ref not in previous_refs:
                added.append(ref)
    except BaseException:
        for ref in added:
            script_assets.release_script_pin(owner, session_id, ref, "user_question", identity)
        raise


def _validate_context_inputs(owner, context):
    """Recheck host-carried identities, never file IDs supplied in model options."""
    import hashlib
    from src.session.files import get_account_file
    working = context.get("context_working_state") or {}
    refs = _source_refs(context)
    checked = set()
    for ref in refs:
        if not isinstance(ref, dict) or not ref.get("file_id") or str(ref["file_id"]).startswith("upload:"):
            continue
        identity = (ref["file_id"], ref.get("sha256"))
        if identity in checked:
            continue
        checked.add(identity)
        # Context is captured by the host, not accepted from question arguments.
        # Private snapshots need the same owner, immutable hash and byte check.
        entry = get_account_file(ref["file_id"], owner, allow_internal=True)
        if not entry or not ref.get("sha256") or entry.get("sha256") != ref["sha256"]:
            raise ValueError("source_version_changed")
        try:
            with open(entry["path"], "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError as exc:
            raise ValueError("source_unavailable") from exc
        if digest != ref["sha256"]:
            raise ValueError("source_version_changed")
    datasets = list(context.get("dataset_refs") or []) + list(working.get("dataset_refs") or [])
    if datasets:
        from src.research.analysis_results import load_dataset_snapshot
        for ref in datasets:
            load_dataset_snapshot(owner, ref)


def _pin_inputs(identity, owner, context, lease=None, *, session_id=""):
    _validate_context_inputs(owner, context)
    refs = _source_refs(context)
    file_ids = list(dict.fromkeys(r["file_id"] for r in refs if isinstance(r, dict)
        and r.get("file_id") and not str(r["file_id"]).startswith("upload:")))
    datasets = list(context.get("dataset_refs") or [])
    for ref in (context.get("context_working_state") or {}).get("dataset_refs") or []:
        if ref not in datasets:
            datasets.append(ref)
    if not (file_ids or datasets or _script_refs(context)):
        return
    if lease is not None and lease.owner != owner:
        raise ValueError("question pin owner mismatch")
    if file_ids or datasets:
        from src.research.analysis_results import pin_dataset_references
        # One atomic replacement includes payloads, every parent and source.
        pin_dataset_references(owner, "user_question", identity, datasets, file_ids=file_ids)
    _pin_scripts(identity, owner, session_id, context)
    if lease is not None:
        try:
            lease.add_finalizer(lambda: _release_pin(identity, owner, context, session_id=session_id))
        except BaseException:
            _release_pin(identity, owner, context, session_id=session_id)
            raise


def _release_pin(identity, owner, context=None, *, session_id=""):
    from src.session.files import release_resource_references
    from src.research import script_assets
    # Each store transaction closes before entering the next one. Release does
    # not admit new account work, so deletion/lease finalizers can drain safely.
    for ref in _script_refs(context or {}):
        try:
            script_assets.release_script_pin(owner, session_id, ref, "user_question", identity)
        except LookupError:
            pass  # The account/script may already have been purged.
    release_resource_references(owner, "user_question", identity)


def create_question(job_id, *, owner_user_id, session_id, prompt, options,
                    multi_select=False, allow_text=True, context=None, ttl_seconds=86400):
    data = UserQuestionInput.model_validate(dict(prompt=prompt, options=options,
        multi_select=multi_select, allow_text=allow_text)).model_dump()
    context = dict(context or {})
    validate_question_refs(data["options"], owner_user_id, context)
    datasets = list(context.get("dataset_refs") or [])
    for option in data["options"]:
        for ref in option["refs"]:
            if ref["kind"] == "dataset_field" and ref["dataset_ref"] not in datasets:
                datasets.append(ref["dataset_ref"])
    context["dataset_refs"] = datasets
    ttl_seconds = min(86400, max(60, int(ttl_seconds)))
    now, identity = time.time(), uuid.uuid4().hex
    pin_attempted = False
    try:
        with transaction() as conn:
            job = _authorized(conn, job_id, owner_user_id, session_id)
            cursor = recovery_cursor(conn, job_id)
            existing = conn.execute("SELECT * FROM task_questions WHERE job_id=? AND recovery_cursor=?",
                                    (job_id, cursor)).fetchone()
            if existing:
                if json.loads(existing["question"]) != data:
                    raise QuestionConflict("question_already_exists")
                return _view(existing)
            if job["status"] not in {"running", "detached"} or current_terminal_event(conn, job_id) is not None:
                raise QuestionConflict("task_not_running")
            conn.execute("INSERT INTO task_questions(id,job_id,owner_user_id,session_id,version,status,question,context,created_at,expires_at,pin_until,recovery_cursor) VALUES (?,?,?,?,1,'pending',?,?,?,?,?,?)",
                (identity, job_id, owner_user_id, session_id, canonical(data), canonical(context), now,
                 now+ttl_seconds, now+ttl_seconds, cursor))
            row = conn.execute("SELECT * FROM task_questions WHERE id=?", (identity,)).fetchone()
            question = _view(row)
            pin_attempted = _has_inputs(context)
            _pin_inputs(identity, owner_user_id, context, session_id=session_id)

    except BaseException:
        if pin_attempted:
            _release_pin(identity, owner_user_id, context, session_id=session_id)
        raise
    return question


def save_question_context(job_id, context, *, question_id, interrupt_id, checkpoint_ref):
    """Called at the graph pause boundary, after all paired tool receipts exist."""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM task_questions WHERE job_id=? AND recovery_cursor=? AND status='pending'",
                           (job_id, recovery_cursor(conn, job_id))).fetchone()
        if (row is None or row["id"] != question_id or not interrupt_id or not checkpoint_ref
                or (row["interrupt_id"] and (row["interrupt_id"], row["checkpoint_ref"]) != (interrupt_id, checkpoint_ref))):
            raise QuestionConflict("pending_question_interrupt_mismatch")
        previous = json.loads(row["context"])
        context = dict(context)
        from src.agent.installed_skills import stored_release_pin
        previous_pin = stored_release_pin(previous)
        if previous_pin and previous_pin != stored_release_pin(context):
            raise QuestionConflict("question_skill_release_mismatch")
        if previous.get("selected_skills", []) != context.get("selected_skills", []):
            raise QuestionConflict("question_skill_selection_mismatch")
        datasets = list(context.get("dataset_refs") or [])
        for ref in previous.get("dataset_refs") or []:
            if ref not in datasets:
                datasets.append(ref)
        context["dataset_refs"] = datasets
        scripts = _script_refs(context)
        previous_scripts = _script_refs(previous)
        for ref in previous_scripts:
            if ref not in scripts:
                scripts.append(ref)
        context["script_refs"] = scripts
        encoded_context = canonical(context)
        _pin_scripts(row["id"], row["owner_user_id"], row["session_id"], context,
                     previous_refs=previous_scripts)
        question, record = _bind_native_pause(conn, row, interrupt_id, checkpoint_ref, encoded_context)
    if record:
        from src.core.progress_bus import get_progress_queue, _encode_event
        queue = get_progress_queue()
        if getattr(queue, "job_id", None) == job_id:
            queue.put_nowait(_encode_event(record["event"], record["data"]) | {"id": str(record["seq"])})
    return question


def _bind_native_pause(conn, row, interrupt_id, checkpoint_ref, context):
    conn.execute("UPDATE task_questions SET context=?,interrupt_id=?,checkpoint_ref=? WHERE id=? AND status=?",
                 (context, interrupt_id, checkpoint_ref, row["id"], row["status"]))
    question = _view(conn.execute("SELECT * FROM task_questions WHERE id=?", (row["id"],)).fetchone())
    record = (_append(conn, row["job_id"], "request_user_input", {"question": question})
              if not row["interrupt_id"] and row["status"] == "pending" else None)
    return question, record


def adopt_question_pause(conn, identity, pause):
    """Bind an exact protected interrupt in the existing restart transaction."""
    _authorized(conn, identity.run_id, identity.owner_user_id, identity.session_id)
    row = conn.execute("SELECT * FROM task_questions WHERE job_id=? AND owner_user_id=? AND session_id=? "
        "AND recovery_cursor=? AND id=? AND version=? AND status IN ('pending','expired') "
        "AND answer_hash='' AND resume_cursor=0 AND child_job_id=''",
        (identity.run_id, identity.owner_user_id, identity.session_id, recovery_cursor(conn, identity.run_id),
         pause["question_id"], pause["version"])).fetchone()
    reference = pause["interrupt_id"], pause["checkpoint_ref"]
    if row is None or (row["interrupt_id"], row["checkpoint_ref"]) not in {("", ""), reference}:
        raise QuestionConflict("pending_question_interrupt_mismatch")
    if row["status"] == "pending" and row["expires_at"] <= time.time():
        conn.execute("UPDATE task_questions SET status='expired' WHERE id=? AND status='pending'", (row["id"],))
        row = conn.execute("SELECT * FROM task_questions WHERE id=?", (row["id"],)).fetchone()
    question, _ = _bind_native_pause(conn, row, *reference, row["context"])
    return question


def list_questions(job_id, *, owner_user_id, session_id, conn=None):
    with read_transaction(conn) as conn:
        _authorized(conn, job_id, owner_user_id, session_id)
        result = []
        for row in conn.execute("SELECT * FROM task_questions WHERE job_id=? AND recovery_cursor=?",
                                (job_id, recovery_cursor(conn, job_id))):
            view = _view(row)
            # An observation can report time expiry without releasing a pin,
            # cancelling a job or waiting for the lifecycle sweep to write.
            if view["status"] == "pending" and row["expires_at"] <= time.time():
                view["status"] = "expired"
            if row["child_job_id"]:
                child = conn.execute("SELECT status FROM task_jobs WHERE job_id=? AND question_id=? AND "
                    "parent_job_id=? AND owner_user_id=? AND session_id=?",
                    (row["child_job_id"], row["id"], job_id, owner_user_id, session_id)).fetchone()
                view["child_status"] = child[0] if child else ""
            result.append(view)
        return result


def question_history_input(job_id, *, owner_user_id, session_id):
    """Rediscover the original user input of a parked native turn."""
    with read_transaction() as conn:
        row = conn.execute("SELECT context FROM task_questions WHERE job_id=? AND owner_user_id=? "
            "AND session_id=? AND interrupt_id<>'' ORDER BY created_at LIMIT 1",
            (job_id, owner_user_id, session_id)).fetchone()
        return str(json.loads(row["context"]).get("query") or "") if row else ""


def queue_wait_state(job_id, *, owner_user_id, session_id):
    """Publish only the scheduler's dispatch or settlement evidence."""
    with read_transaction() as conn:
        _authorized(conn, job_id, owner_user_id, session_id)
        rows = conn.execute("""SELECT q.status, q.child_job_id, q.expires_at, q.interrupt_id,
                                      c.status AS child_status
            FROM task_questions q LEFT JOIN task_jobs c ON c.job_id=q.child_job_id
                  AND c.question_id=q.id AND c.parent_job_id=q.job_id
                  AND c.owner_user_id=q.owner_user_id AND c.session_id=q.session_id
            WHERE q.job_id=? AND q.owner_user_id=? AND q.session_id=? AND q.recovery_cursor=? LIMIT 2""",
            (job_id, owner_user_id, session_id, recovery_cursor(conn, job_id))).fetchall()
        if len(rows) != 1:
            return "unknown", ""
        row = rows[0]
        if row["status"] == "pending":
            return ("expire" if row["expires_at"] <= time.time() else "pending"), ""
        if row["status"] in {"cancelled", "expired"} and not row["child_job_id"]:
            return "question_" + row["status"], ""
        if row["status"] == "answered":
            if row["interrupt_id"] and not row["child_job_id"]:
                if row["expires_at"] <= time.time():
                    return "expire", ""
                return "dispatch", job_id
            if row["child_status"] in {"completed", "failed", "cancelled"}:
                return "legacy_question_settled", ""
        return "unknown", ""


def answer_question(job_id, question_id, *, owner_user_id, session_id, version,
                    interrupt_id="", selected_option_ids=None, text="", cancelled=False, client_request_id):
    answer = QuestionAnswer.model_validate(dict(version=version, interrupt_id=interrupt_id,
        selected_option_ids=selected_option_ids or [], text=text, cancelled=cancelled,
        client_request_id=client_request_id))
    selected = answer.selected_option_ids
    if len(set(selected)) != len(selected) or not answer.client_request_id.strip():
        raise ValueError("invalid_answer")
    value = {"selected_option_ids": sorted(selected), "text": answer.text.strip(), "cancelled": cancelled}
    digest = input_hash(value)
    expire_questions()
    with transaction() as conn:
        job = _authorized(conn, job_id, owner_user_id, session_id)
        row = conn.execute("SELECT * FROM task_questions WHERE id=? AND job_id=?", (question_id, job_id)).fetchone()
        if row is None:
            raise QuestionConflict("question_not_found")
        # Version identifies the prompt; answering never silently changes it.
        if row["version"] != version:
            raise QuestionConflict("question_version_conflict")
        if row["interrupt_id"] != interrupt_id:
            raise QuestionConflict("question_interrupt_conflict")
        if row["status"] in {"answered", "cancelled"} and row["answer_hash"]:
            if row["answer_hash"] != digest:
                raise QuestionConflict("question_answer_conflict")
            dispatch = bool(row["interrupt_id"] and row["status"] == "answered"
                            and not row["resume_cursor"] and job["status"] == "awaiting_input")
            return _answer_result(row, dispatch=dispatch, changed=False)
        if row["recovery_cursor"] != recovery_cursor(conn, job_id):
            raise QuestionConflict("question_version_conflict")
        if row["status"] != "pending" or row["expires_at"] <= time.time():
            raise QuestionConflict("question_expired")
        if not row["interrupt_id"] and not cancelled:
            raise QuestionConflict("question_legacy_dispatch_closed" if job["status"] == "awaiting_input"
                                   else "question_turn_not_closed")
        if job["status"] != "awaiting_input":
            raise QuestionConflict("question_turn_not_closed")
        q = json.loads(row["question"])
        allowed = {o["id"] for o in q["options"]}
        if not set(selected) <= allowed or (not q["multi_select"] and len(selected) > 1):
            raise ValueError("invalid_option_selection")
        if value["text"] and not q["allow_text"]:
            raise ValueError("custom_text_not_allowed")
        if cancelled and (selected or value["text"]):
            raise ValueError("cancelled_answer_must_be_empty")
        if not cancelled and not (selected or value["text"]):
            raise ValueError("empty_answer")
        # Cancellation must remain possible if a source has disappeared.
        # A semantic option with no field refs still carries immutable inputs.
        if not cancelled:
            context = json.loads(row["context"])
            validate_question_refs(q["options"], owner_user_id, context)
            _validate_context_inputs(owner_user_id, context)
            from src.research.script_assets import get_script
            for ref in _script_refs(context):
                get_script(owner_user_id, session_id, ref)
        status = "cancelled" if cancelled else "answered"
        changed = conn.execute("UPDATE task_questions SET status=?,answer=?,answer_hash=?,client_request_id=? "
            "WHERE id=? AND status='pending' AND version=? AND interrupt_id=?",
            (status, canonical(value), digest, client_request_id, question_id, version, interrupt_id)).rowcount
        if changed != 1:
            raise QuestionConflict("question_answer_conflict")
        if cancelled:
            context = json.loads(row["context"])
            if _has_inputs(context):
                _release_pin(question_id, owner_user_id, context, session_id=session_id)
            conn.execute("UPDATE task_questions SET pin_until=0 WHERE id=?", (question_id,))
        return _answer_result(conn.execute("SELECT * FROM task_questions WHERE id=?", (question_id,)).fetchone(),
                              dispatch=not cancelled, changed=True)


def _answer_result(row, *, dispatch, changed):
    return {"question": _view(row), "job_id": row["job_id"],
            "resume_job_id": row["job_id"] if row["interrupt_id"] and row["status"] == "answered" else "",
            "dispatch_required": dispatch, "created": changed}


def resume_question_row(conn, job_id, *, active=False):
    cursor = recovery_cursor(conn, job_id)
    query = ("resume_cursor=?" if active else "recovery_cursor=? AND resume_cursor=0")
    return conn.execute("SELECT * FROM task_questions WHERE job_id=? AND status='answered' "
                        "AND interrupt_id<>'' AND child_job_id='' AND " + query, (job_id, cursor)).fetchone()


def question_resume(identity, *, active=False):
    with read_transaction() as conn:
        _authorized(conn, identity.run_id, identity.owner_user_id, identity.session_id)
        row = resume_question_row(conn, identity.run_id, active=active)
        if row is None or (not active and row["expires_at"] <= time.time()):
            raise QuestionConflict("question_resume_unavailable")
        answer = json.loads(row["answer"])
        options = json.loads(row["question"])["options"]
        value = QuestionResume(kind="user_input", question_id=row["id"], version=row["version"],
            interrupt_id=row["interrupt_id"], selected_option_ids=answer["selected_option_ids"],
            selected_labels=[option["label"] for option in options if option["id"] in answer["selected_option_ids"]],
            text=answer["text"])
        return {"checkpoint_ref": row["checkpoint_ref"], "value": value.model_dump()}


def acknowledge_question_resume(identity, value, checkpoint_ref):
    wake = QuestionResume.model_validate(value)
    with transaction() as conn:
        _authorized(conn, identity.run_id, identity.owner_user_id, identity.session_id)
        row = resume_question_row(conn, identity.run_id, active=True)
        if (row is None or (wake.question_id, wake.version, wake.interrupt_id) !=
                (row["id"], row["version"], row["interrupt_id"]) or not checkpoint_ref
                or (row["ack_checkpoint_ref"] and row["ack_checkpoint_ref"] != checkpoint_ref)):
            raise QuestionConflict("question_wake_ack_conflict")
        conn.execute("UPDATE task_questions SET ack_checkpoint_ref=? WHERE id=?", (checkpoint_ref, row["id"]))


def retired_question(identity, previous_job_id):
    """A closed question can retire a parked head without replaying it."""
    with read_transaction() as conn:
        row = conn.execute("SELECT q.status,q.child_job_id,c.status AS child_status "
            "FROM task_questions q LEFT JOIN task_jobs c ON c.job_id=q.child_job_id "
            "AND c.parent_job_id=q.job_id AND c.question_id=q.id AND c.owner_user_id=q.owner_user_id "
            "AND c.session_id=q.session_id WHERE q.job_id=? AND q.owner_user_id=? AND q.session_id=? "
            "AND q.recovery_cursor=?", (previous_job_id, identity.owner_user_id, identity.session_id,
            recovery_cursor(conn, previous_job_id))).fetchone()
        return bool(row and (row["status"] in {"cancelled", "expired"} or
                    (row["status"] == "answered" and row["child_status"] in {"completed", "failed", "cancelled"})))


def supersede_pending_questions(new_job_id, *, owner_user_id, session_id):
    """A new ordinary chat turn replaces unanswered choices in this conversation.

    Typed question answers use the continuation path and never call this. Only
    older pending questions are invalidated; accepted answers and running work
    keep their recorded identity. Closed parent event streams stay immutable.
    """
    with transaction() as conn:
        job = _authorized(conn, new_job_id, owner_user_id, session_id)
        rows = conn.execute("""SELECT q.* FROM task_questions q
            JOIN task_jobs j ON j.job_id=q.job_id
            WHERE q.owner_user_id=? AND q.session_id=? AND q.status='pending'
              AND q.job_id<>? AND j.created_at<=?""",
            (owner_user_id, session_id, new_job_id, job["created_at"])).fetchall()
        for row in rows:
            context = json.loads(row["context"])
            if _has_inputs(context):
                _release_pin(row["id"], owner_user_id, context, session_id=session_id)
            conn.execute("UPDATE task_questions SET status='expired',pin_until=0 WHERE id=? AND status='pending'", (row["id"],))
        return len(rows)


def expire_questions(now=None):
    """Host calls periodically; answer and queue lifecycle may also sweep. Bounded DB pins end here."""
    now = time.time() if now is None else now
    with transaction() as conn:
        expired = conn.execute("SELECT * FROM task_questions WHERE pin_until>0 AND pin_until<=?", (now,)).fetchall()
        for row in expired:
            context = json.loads(row["context"])
            if _has_inputs(context):
                _release_pin(row["id"], row["owner_user_id"], context, session_id=row["session_id"])
            conn.execute("UPDATE task_questions SET pin_until=0 WHERE id=?", (row["id"],))
            if row["child_job_id"]:
                conn.execute("UPDATE task_jobs SET status='cancelled',error='continuation_inputs_expired',finished_at=?,updated_at=? WHERE job_id=? AND status='queued'", (datetime.now().isoformat(), datetime.now().isoformat(), row["child_job_id"]))
        cur = conn.execute("UPDATE task_questions SET status='expired',pin_until=0 WHERE expires_at<=? AND "
            "(status='pending' OR (status='answered' AND interrupt_id<>'' AND resume_cursor=0))", (now,))
        return int(cur.rowcount)
