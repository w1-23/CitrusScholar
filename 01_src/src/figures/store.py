"""Where figure projects live.

A *figure project* is one figure the user can keep: its source (a
validated specification, or a script), the semantic manifest that describes
what may be edited, the user's own edits, and the exported files.

The store is a small SQLite table under the service's state directory. It
follows the same shape as the account file library, and it inherits the same
ownership rule: **every query carries the owner's id in its WHERE clause**.
A row that belongs to somebody else is not found, not found-and-refused —
an attacker must not be able to tell "this exists but is not yours" from
"this does not exist".

What is deliberately *not* here
------------------------------
The rendered files. They live in the account file library, registered as
ordinary account files, because that is where the service already knows how
to store, list, download and delete a user's files with the right
permissions. This table holds the pointers and the editing state; the bytes
live with the rest of the user's material.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

# The state directory is resolved lazily so a test can point the store at a
# temporary directory without importing the whole service.
_STATE_DIR: Path | None = None


class NatureBindingConflict(ValueError):
    """Another collection already chose the project for this execution."""


class ProjectQuotaExceeded(ValueError):
    """Existing projects remain available when creation capacity is exhausted."""


def set_state_dir(path: Path | None) -> None:
    """Point the store at a different state directory (used by tests)."""
    global _STATE_DIR
    _STATE_DIR = Path(path) if path else None


def _db_path() -> Path:
    if _STATE_DIR is not None:
        root = _STATE_DIR
    else:
        from src.config import PROJECT_ROOT
        root = PROJECT_ROOT / "state"
    path = root / "figure_projects.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class _StoreConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30, isolation_level=None,
                           factory=_StoreConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS figure_projects (
        figure_id      TEXT PRIMARY KEY,
        owner_user_id  INTEGER NOT NULL,
        session_id     TEXT NOT NULL DEFAULT '',
        title          TEXT NOT NULL DEFAULT '',
        source_kind    TEXT NOT NULL DEFAULT 'spec',
        source_json    TEXT NOT NULL DEFAULT '{}',
        manifest_json  TEXT NOT NULL DEFAULT '{}',
        overrides_json TEXT NOT NULL DEFAULT '[]',
        exports_json   TEXT NOT NULL DEFAULT '{}',
        status         TEXT NOT NULL DEFAULT 'ready',
        last_error     TEXT NOT NULL DEFAULT '',
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL)""")
    _ensure_columns(conn, "figure_projects", {
        "data_refs_json": "TEXT NOT NULL DEFAULT '[]'",
        "document_json": "TEXT NOT NULL DEFAULT '{}'",
        "render_version": "INTEGER NOT NULL DEFAULT 0",
        "pins_version": "INTEGER NOT NULL DEFAULT 0",
    })
    conn.execute("CREATE INDEX IF NOT EXISTS idx_figure_projects_owner "
                 "ON figure_projects(owner_user_id, updated_at DESC)")
    conn.execute("CREATE TABLE IF NOT EXISTS figure_project_pins "
                 "(resource_id TEXT PRIMARY KEY, figure_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL)")
    _ensure_columns(conn, "figure_project_pins", {
        "script_session": "TEXT NOT NULL DEFAULT ''",
        "script_ref_json": "TEXT NOT NULL DEFAULT 'null'",
        "obsolete": "INTEGER NOT NULL DEFAULT 0",
        "revision": "INTEGER NOT NULL DEFAULT 0",
    })
    conn.execute("""CREATE TABLE IF NOT EXISTS figure_revision_exports (
        figure_id TEXT NOT NULL,
        owner_user_id INTEGER NOT NULL,
        render_version INTEGER NOT NULL,
        format TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (figure_id, owner_user_id, render_version, format)
    )""")
    _ensure_columns(conn, "figure_revision_exports", {"pin_id": "TEXT NOT NULL DEFAULT ''",
                                                       "batch_id": "TEXT NOT NULL DEFAULT ''"})
    conn.execute("""CREATE TABLE IF NOT EXISTS figure_publications (
        batch_id TEXT PRIMARY KEY, figure_id TEXT NOT NULL, owner_user_id INTEGER NOT NULL,
        session_id TEXT NOT NULL, request_id TEXT NOT NULL, entries TEXT NOT NULL,
        optional INTEGER NOT NULL DEFAULT 0)""")
    _ensure_columns(conn, "figure_publications", {"optional": "INTEGER NOT NULL DEFAULT 0"})
    conn.execute("CREATE INDEX IF NOT EXISTS idx_figure_publications_project "
                 "ON figure_publications(figure_id,owner_user_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS figure_nature_bindings (
        owner_user_id INTEGER NOT NULL, session_id TEXT NOT NULL, execution_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, figure_id TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY (owner_user_id,session_id,execution_id))""")
    return conn


def _ensure_columns(conn, table, declarations):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in declarations.items():
        if name not in columns:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except sqlite3.OperationalError:
                if name not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    raise


def _record_publication(conn, figure_id, owner, publication, *, optional=False):
    if publication is not None:
        batch_id, session_id, request_id, entries = publication
        conn.execute("INSERT INTO figure_publications VALUES (?,?,?,?,?,?,?)",
                     (batch_id, figure_id, owner, session_id, request_id,
                      json.dumps(entries, ensure_ascii=False, allow_nan=False), int(optional)))


def pending_publication(batch_id, owner):
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM figure_publications WHERE batch_id=? AND owner_user_id=?",
                            (batch_id, owner)).fetchone() is not None


def finish_publications(figure_id: str, owner: int, *, required_batch=None) -> None:
    """Reconcile durable decisions after CAS, even after a process interruption."""
    from src.core.account_leases import account_lease
    from src.session.files import promote_figure_files

    with _connect() as conn:
        rows = conn.execute("SELECT * FROM figure_publications WHERE figure_id=? AND owner_user_id=? "
                            "ORDER BY rowid", (figure_id, owner)).fetchall()
    if rows:
        with account_lease(owner):
            for queued in rows:
                try:
                    with _connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        row = conn.execute("SELECT * FROM figure_publications WHERE batch_id=? AND owner_user_id=?",
                                           (queued["batch_id"], owner)).fetchone()
                        if row is None:
                            continue
                        promote_figure_files(owner, row["session_id"], row["request_id"], json.loads(row["entries"]))
                        conn.execute("DELETE FROM figure_publications WHERE batch_id=? AND owner_user_id=?",
                                     (row["batch_id"], owner))
                except (OSError, ValueError, sqlite3.Error):
                    # A corrupt optional export must not block opening its SVG or
                    # regeneration. Explicit export requests still see the failure.
                    if not queued["optional"] or queued["batch_id"] == required_batch:
                        raise


def get_nature_binding(owner, session_id, execution_id):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM figure_nature_bindings WHERE owner_user_id=? "
                           "AND session_id=? AND execution_id=?", (owner, session_id, execution_id)).fetchone()
    return {**dict(row), "receipt": json.loads(row["receipt"])} if row else None


def _source_script(source):
    return source.get("script_ref") if isinstance(source, dict) else None


def _pin_files(owner: int, figure_id: str, file_ids: list[str],
               data_refs: list | None = None, *, source=None, session_id="") -> tuple[str, list[dict[str, Any]]]:
    datasets = [ref for ref in (data_refs or []) if isinstance(ref, dict)
                and ref.get("kind") == "dataset_snapshot"]
    script_ref = _source_script(source)
    if not file_ids and not datasets and script_ref is None:
        return "", []
    resource_id = f"{figure_id}:{uuid.uuid4().hex}"
    script_pinned = False
    try:
        if script_ref is not None:
            from src.research.script_assets import pin_script
            pin_script(owner, session_id, script_ref, "figure", resource_id)
            script_pinned = True
        # The script transaction is already closed: never nest it inside the
        # file/dataset or figure DB locks. Existing file closure is unchanged.
        if datasets:
            from src.research.analysis_results import pin_dataset_references
            refs = pin_dataset_references(owner, "figure", resource_id, datasets, file_ids=file_ids)
        elif file_ids:
            from src.session.files import replace_resource_references
            refs = replace_resource_references(owner, "figure", resource_id, file_ids)
        else:
            refs = []
        return resource_id, refs
    except BaseException:
        if script_pinned:
            from src.research.script_assets import release_script_pin
            release_script_pin(owner, session_id, script_ref, "figure", resource_id)
        raise


def _record_pin(conn, resource_id, figure_id, owner, source, session_id, revision=0):
    if resource_id:
        conn.execute("INSERT INTO figure_project_pins "
            "(resource_id,figure_id,owner_user_id,script_session,script_ref_json,revision) VALUES (?,?,?,?,?,?)",
            (resource_id, figure_id, owner, session_id,
             json.dumps(_source_script(source), ensure_ascii=False), revision))


def _release_pin(owner: int, resource_id: str, *, source=None, session_id="") -> None:
    if resource_id:
        script_ref = _source_script(source)
        if script_ref is not None:
            from src.research.script_assets import release_script_pin
            try:
                release_script_pin(owner, session_id, script_ref, "figure", resource_id)
            except LookupError:
                pass  # Account deletion may already have purged script assets.
        from src.session.files import release_resource_references
        release_resource_references(owner, "figure", resource_id)


def _row_to_project(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    record = dict(row)
    for key, fallback in (("source_json", {}), ("manifest_json", {}),
                          ("overrides_json", []), ("exports_json", {}),
                          ("data_refs_json", []), ("document_json", {})):
        try:
            record[key] = json.loads(record.get(key) or json.dumps(fallback))
        except (TypeError, ValueError):
            record[key] = fallback
    return record


def create_project(*, owner_user_id: int, title: str = "",
                   source_kind: str = "spec", source: Any = None,
                   session_id: str = "",
                   data_refs: list[Any] | None = None, initial_manifest=None,
                   initial_exports=None, publication=None, nature_binding=None) -> dict[str, Any]:
    """Create a project while the account admits writes."""
    from src.core.account_leases import account_lease
    with account_lease(int(owner_user_id)):
        return _create_project(owner_user_id=owner_user_id, title=title,
                               source_kind=source_kind, source=source,
                               session_id=session_id, data_refs=data_refs, initial_manifest=initial_manifest,
                               initial_exports=initial_exports, publication=publication,
                               nature_binding=nature_binding)


def _create_project(*, owner_user_id: int, title: str, source_kind: str,
                    source: Any, session_id: str, data_refs: list[Any] | None,
                    initial_manifest, initial_exports, publication, nature_binding) -> dict[str, Any]:
    figure_id = uuid.uuid4().hex
    now = time.time()
    from src.figures.document import build_figure_document
    revision = int(initial_exports is not None)
    exports = initial_exports or {}
    manifest = initial_manifest or {}
    document = build_figure_document({"figure_id": figure_id, "source_kind": source_kind,
        "source_json": source or {}, "manifest_json": manifest, "exports_json": exports,
        "render_version": revision})
    data_refs = list(data_refs or [])
    file_ids = [str(ref["file_id"]) for ref in data_refs if isinstance(ref, dict) and ref.get("file_id")]
    export_ids = [item["file_id"] for item in exports.values()]
    resource_id, pinned = _pin_files(int(owner_user_id), figure_id, file_ids + export_ids, data_refs,
                                    source=source, session_id=session_id)
    binding_pin = ""
    hashes = {ref["file_id"]: ref["sha256"] for ref in pinned}
    data_refs = [{**ref, "sha256": hashes[ref["file_id"]]} if isinstance(ref, dict)
                 and ref.get("file_id") in hashes else ref for ref in data_refs]
    try:
        if nature_binding is not None:
            binding_pin, _ = _pin_files(int(owner_user_id), figure_id, export_ids)
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if nature_binding is not None:
                execution_id, fingerprint = nature_binding
                result = conn.execute("INSERT OR IGNORE INTO figure_nature_bindings VALUES (?,?,?,?,?,?)",
                    (owner_user_id, session_id, execution_id, fingerprint, figure_id,
                     json.dumps({"exports": exports, "render_version": revision})))
                if result.rowcount != 1:
                    raise NatureBindingConflict()
            import os
            maximum = int(os.environ.get("CITRUS_FIGURE_PROJECTS_PER_ACCOUNT", "500"))
            if maximum <= 0:
                raise ValueError("CITRUS_FIGURE_PROJECTS_PER_ACCOUNT must be positive")
            count = conn.execute("SELECT COUNT(*) FROM figure_projects WHERE owner_user_id=?",
                                 (int(owner_user_id),)).fetchone()[0]
            if count >= maximum:
                raise ProjectQuotaExceeded("账号图件项目数量达到上限，请删除不再需要的图件后重试。")
            conn.execute(
                "INSERT INTO figure_projects (figure_id, owner_user_id, session_id, title,"
                " source_kind, source_json, manifest_json, overrides_json, exports_json,"
                " data_refs_json, status, created_at, updated_at, document_json, render_version, pins_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,2)",
                (figure_id, int(owner_user_id), str(session_id or ""), str(title or ""),
                 str(source_kind or "spec"), json.dumps(source or {}, ensure_ascii=False),
                 json.dumps(manifest, ensure_ascii=False), "[]", json.dumps(exports, ensure_ascii=False),
                  json.dumps(data_refs, ensure_ascii=False), "ready", now, now,
                  json.dumps(document, ensure_ascii=False), revision))
            _record_pin(conn, resource_id, figure_id, int(owner_user_id), source, session_id)
            _record_pin(conn, binding_pin, figure_id, int(owner_user_id), None, "", revision=-1)
            _record_publication(conn, figure_id, owner_user_id, publication)
            row = conn.execute(
                "SELECT * FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
                (figure_id, int(owner_user_id))).fetchone()
    except BaseException:
        _release_pin(int(owner_user_id), resource_id, source=source, session_id=session_id)
        _release_pin(int(owner_user_id), binding_pin)
        raise
    return _row_to_project(row) or {}


def get_project(figure_id: str, owner_user_id: int) -> dict[str, Any] | None:
    """Fetch one project owned by this account, or ``None``."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
            (str(figure_id), int(owner_user_id))).fetchone()
    return _row_to_project(row)


def _export_payload(row, figure_id, version, fmt):
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
        if (isinstance(payload, dict) and isinstance(payload.get("export"), dict)
                and payload.get("figure_id") == figure_id and type(payload.get("render_version")) is int
                and payload["render_version"] == version and payload.get("format") == fmt):
            return payload
    except (TypeError, ValueError):
        pass
    return {}


def get_revision_export(figure_id: str, owner: int, version: int, fmt: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT e.payload FROM figure_revision_exports e JOIN figure_projects p "
            "ON p.figure_id=e.figure_id AND p.owner_user_id=e.owner_user_id "
            "WHERE e.figure_id=? AND e.owner_user_id=? AND e.render_version=? AND e.format=?",
            (figure_id, owner, version, fmt),
        ).fetchone()
    return _export_payload(row, figure_id, version, fmt)


def list_revision_exports(figure_id: str, owner: int, version: int) -> dict[str, dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT format, payload FROM figure_revision_exports "
            "WHERE figure_id=? AND owner_user_id=? AND render_version=?",
            (figure_id, owner, version),
        ).fetchall()
    return {row["format"]: item["export"] for row in rows if (item := _export_payload(row, figure_id, version, row["format"]))}


def record_revision_export(figure_id: str, owner: int, version: int, fmt: str,
                           payload: dict, *, previous=None, publication=None) -> dict | None:
    """Install or repair a current-revision export, conditional on both reads."""
    key = (figure_id, owner, version, fmt)
    select = ("SELECT payload,pin_id,batch_id FROM figure_revision_exports WHERE figure_id=? AND "
              "owner_user_id=? AND render_version=? AND format=?")
    from src.core.account_leases import account_lease
    with account_lease(owner):
        resource_id, pinned = _pin_files(owner, figure_id, [payload["export"]["file_id"]])
        committed, obsolete = False, []
        try:
            if pinned and pinned[0]["sha256"] != payload["export"]["sha256"]:
                raise ValueError("revision export checksum mismatch")
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                project = conn.execute("SELECT render_version FROM figure_projects WHERE figure_id=? "
                                       "AND owner_user_id=?", (figure_id, owner)).fetchone()
                if project is None or project["render_version"] != version:
                    return None
                row = conn.execute(select, key).fetchone()
                existing = _export_payload(row, figure_id, version, fmt)
                if existing != previous or (row and row["pin_id"] and existing == payload):
                    return existing
                if row and row["pin_id"]:
                    obsolete = _mark_obsolete_pins(conn, figure_id, owner, "resource_id=?", (row["pin_id"],))
                if row and row["batch_id"]:
                    conn.execute("DELETE FROM figure_publications WHERE batch_id=? AND owner_user_id=?",
                                 (row["batch_id"], owner))
                conn.execute("INSERT INTO figure_revision_exports VALUES (?,?,?,?,?,?,?) "
                             "ON CONFLICT(figure_id,owner_user_id,render_version,format) DO UPDATE "
                             "SET payload=excluded.payload,pin_id=excluded.pin_id,batch_id=excluded.batch_id",
                             (*key, json.dumps(payload, ensure_ascii=False, allow_nan=False), resource_id,
                              publication[0] if publication else ""))
                _record_pin(conn, resource_id, figure_id, owner, None, "", revision=version)
                _record_publication(conn, figure_id, owner, publication, optional=True)
            committed = True
        finally:
            if not committed:
                _release_pin(owner, resource_id)
        if obsolete:
            try:
                _release_recorded_pins(owner, obsolete)
            except Exception:
                logging.getLogger(__name__).warning("figure export pin cleanup deferred", exc_info=True)
        return payload


def _pin_current_legacy_exports(artifact_id=None) -> None:
    """Adopt legacy on-demand exports while they are still current and intact."""
    from src.core.account_admission import AccountUnavailable
    from src.figures.workbench.artifacts import read_entry
    from src.figures.projects import ProjectError

    query = ("SELECT e.* FROM figure_revision_exports e JOIN figure_projects p "
             "ON p.figure_id=e.figure_id AND p.owner_user_id=e.owner_user_id "
             "AND p.render_version=e.render_version WHERE e.pin_id='' AND e.format IN ('png','pdf')")
    params = ()
    if artifact_id is not None:
        query += " AND json_extract(CASE WHEN json_valid(e.payload) THEN e.payload ELSE '{}' END, '$.export.artifact_id')=?"
        params = (artifact_id,)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    for row in rows:
        payload = _export_payload(row, row["figure_id"], row["render_version"], row["format"])
        if payload:
            try:
                read_entry(payload["export"], row["owner_user_id"], row["format"])
                record_revision_export(row["figure_id"], row["owner_user_id"], row["render_version"],
                                       row["format"], payload, previous=payload)
            except (AccountUnavailable, ProjectError, ValueError, KeyError, OSError):
                # Missing data isn't a pin candidate and is handled by replay.
                continue


def list_projects(owner_user_id: int, *, limit: int = 100,
                  offset: int = 0) -> list[dict[str, Any]]:
    """The account's projects, most recently updated first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM figure_projects WHERE owner_user_id=?"
            " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (int(owner_user_id), max(1, int(limit)), max(0, int(offset)))).fetchall()
    return [_row_to_project(row) or {} for row in rows]


def update_project(figure_id: str, owner_user_id: int, *,
                   expected_revision: int | None = None, **fields: Any) -> bool:
    """Update a project while the account admits writes."""
    return update_project_committed(
        figure_id, owner_user_id, expected_revision=expected_revision,
        **fields) is not None


def update_project_committed(figure_id: str, owner_user_id: int, *,
                             expected_revision: int | None = None, publication=None,
                             **fields: Any) -> dict[str, Any] | None:
    """Update a project and return the row that was committed.

    ``None`` means the compare-and-swap matched no row: the revision was
    stale, the project was deleted, or another writer changed it first.
    The returned row is read inside the same write transaction as the
    UPDATE, so its ``document_json`` is the persisted identity document
    for exactly the committed render revision, never a later writer's.
    """
    from src.core.account_leases import account_lease
    with account_lease(int(owner_user_id)):
        return _update_project(figure_id, owner_user_id,
                               expected_revision=expected_revision, publication=publication, **fields)


def _update_project(figure_id: str, owner_user_id: int, *,
                    expected_revision: int | None = None, publication=None,
                    **fields: Any) -> dict[str, Any] | None:
    """Update the mutable fields of a project.

    Only the fields that describe the figure's *current state* may be
    written; the identity, the owner and the creation time are not among
    them. A project of somebody else is not updated — the WHERE clause
    matches nothing and the function reports failure.
    """
    allowed = ("title", "source_kind", "source_json", "manifest_json",
               "overrides_json", "exports_json", "status", "last_error",
               "data_refs_json", "render_version")
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return None
    before = get_project(figure_id, owner_user_id)
    if before is None:
        return None
    # The identity document is derived server-side and committed with the same
    # row CAS as its manifest/source/exports. Never accept a caller's document.
    render_fields = ("source_kind", "source_json", "manifest_json", "overrides_json",
                     "exports_json", "data_refs_json", "render_version")
    if any(key in updates and updates[key] != before.get(key) for key in render_fields):
        from src.figures.document import build_figure_document
        updates["document_json"] = build_figure_document({**before, **updates})
    assignments = []
    values: list[Any] = []
    for key, value in updates.items():
        if key in {"source_json", "manifest_json", "exports_json", "data_refs_json", "document_json"}:
            value = json.dumps(value if value is not None else {}, ensure_ascii=False)
        elif key == "overrides_json":
            value = json.dumps(value if value is not None else [], ensure_ascii=False)
        assignments.append(f"{key}=?")
        values.append(value)
    assignments.append("updated_at=?")
    values.append(time.time())
    values.extend([str(figure_id), int(owner_user_id)])
    condition = " WHERE figure_id=? AND owner_user_id=?"
    if expected_revision is not None:
        condition += " AND render_version=?"
        values.append(int(expected_revision))
    exports = updates.get("exports_json", before.get("exports_json")) or {}
    refs = updates.get("data_refs_json", before.get("data_refs_json")) or []
    # Pin the complete new dependency set before CAS. Each writer has a unique
    # identity, so a losing writer can release only its own provisional pins.
    file_ids = [str(item["file_id"]) for item in exports.values()
                if isinstance(item, dict) and item.get("file_id")] if isinstance(exports, dict) else []
    file_ids.extend(str(ref["file_id"]) for ref in refs
                    if isinstance(ref, dict) and ref.get("file_id"))
    source = updates.get("source_json", before.get("source_json"))
    session_id = before.get("session_id", "")
    dependencies_changed = (
        exports != (before.get("exports_json") or {})
        or refs != (before.get("data_refs_json") or [])
        or _source_script(source) != _source_script(before.get("source_json"))
        or int(before.get("pins_version") or 0) < 2
    )
    resource_id = ""
    if dependencies_changed:
        resource_id, _ = _pin_files(int(owner_user_id), str(figure_id), file_ids, refs,
                                  source=source, session_id=session_id)
    committed = False
    obsolete = []
    if dependencies_changed:
        assignments.append("pins_version=2")
    persisted = None
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT * FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
                (str(figure_id), int(owner_user_id))).fetchone()
            current_before = _row_to_project(previous)
            if current_before != before or (expected_revision is not None
                    and int(before.get("render_version") or 0) != int(expected_revision)):
                return None
            old_revision = int(before.get("render_version") or 0)
            new_revision = int(updates.get("render_version", old_revision))
            if new_revision < old_revision:
                raise ValueError("figure revisions must not move backwards")
            if old_revision > 0 and new_revision == old_revision and any(
                key in updates and updates[key] != before.get(key)
                for key in ("source_kind", "source_json", "manifest_json",
                            "overrides_json", "exports_json", "data_refs_json")
            ):
                raise ValueError("render changes require a new figure revision")
            cursor = conn.execute(
                f"UPDATE figure_projects SET {', '.join(assignments)}" + condition, values)
            committed = cursor.rowcount > 0
            if committed:
                row = conn.execute(
                    "SELECT * FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
                    (str(figure_id), int(owner_user_id))).fetchone()
                persisted = _row_to_project(row)
                _record_publication(conn, figure_id, owner_user_id, publication)
                if new_revision > old_revision:
                    obsolete.extend(_mark_obsolete_pins(conn, figure_id, owner_user_id,
                                                        "revision BETWEEN 1 AND ?", (old_revision,)))
                    conn.execute("DELETE FROM figure_publications WHERE batch_id IN ("
                        "SELECT batch_id FROM figure_revision_exports WHERE figure_id=? AND owner_user_id=? "
                        "AND render_version<=? AND batch_id<>'')", (figure_id, owner_user_id, old_revision))
                if dependencies_changed:
                    obsolete.extend(_retire_pins(conn, str(figure_id), int(owner_user_id)))
                    _record_pin(conn, resource_id, str(figure_id), int(owner_user_id), source, session_id)
    except BaseException:
        committed = False
        raise
    finally:
        if not committed:
            _release_pin(int(owner_user_id), resource_id, source=source, session_id=session_id)
    if committed:
        try:
            _release_recorded_pins(int(owner_user_id), obsolete)
        except Exception:
            # The save is durable. Retain obsolete rows for the cleanup retry;
            # reporting a failed save could make callers remove current exports.
            logging.getLogger(__name__).warning("figure obsolete pin cleanup deferred", exc_info=True)
    return persisted


def _delete_revisions(conn, figure_id: str, owner: int) -> None:
    # Only existing databases have this retired table. Never create new history.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='figure_revisions'").fetchone():
        conn.execute("DELETE FROM figure_revisions WHERE figure_id=? AND owner_user_id=?",
                     (figure_id, owner))


def _retire_pins(conn, figure_id: str, owner: int) -> list:
    """Capture obsolete identities inside the successful current-state transaction."""
    _delete_revisions(conn, figure_id, owner)
    return _mark_obsolete_pins(conn, figure_id, owner, "revision=0")


def _mark_obsolete_pins(conn, figure_id, owner, predicate, params=()):
    where = " WHERE figure_id=? AND owner_user_id=? AND obsolete=0 AND " + predicate
    args = (figure_id, owner, *params)
    pins = conn.execute("SELECT * FROM figure_project_pins" + where, args).fetchall()
    conn.execute("UPDATE figure_project_pins SET obsolete=1" + where, args)
    return pins


def delete_project(figure_id: str, owner_user_id: int) -> bool:
    """Delete the project first, then release its source and render pins."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pins = conn.execute(
            "SELECT * FROM figure_project_pins WHERE figure_id=? AND owner_user_id=?",
            (str(figure_id), int(owner_user_id))).fetchall()
        cursor = conn.execute(
            "DELETE FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
            (str(figure_id), int(owner_user_id)))
        removed = cursor.rowcount > 0
        _delete_revisions(conn, str(figure_id), int(owner_user_id))
        conn.execute("DELETE FROM figure_publications WHERE figure_id=? AND owner_user_id=?",
                     (figure_id, owner_user_id))
        conn.execute("DELETE FROM figure_revision_exports WHERE figure_id=? AND owner_user_id=?",
                     (str(figure_id), int(owner_user_id)))
    _release_recorded_pins(int(owner_user_id), pins)
    return removed


def _release_recorded_pins(owner: int, pins: list) -> None:
    for pin in pins:
        resource_id = str(pin["resource_id"])
        _release_pin(owner, resource_id, source={"script_ref": json.loads(pin["script_ref_json"])},
                     session_id=pin["script_session"])
        with _connect() as conn:
            conn.execute("DELETE FROM figure_project_pins WHERE resource_id=? AND owner_user_id=?",
                         (resource_id, owner))


def purge_account(owner_user_id: int) -> int:
    """Delete every project belonging to an account.

    Called when an account is deleted. The rows go before the account's
    identity does, so a half-completed purge cannot leave a resurrected
    figure behind.
    """
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        pins = conn.execute("SELECT * FROM figure_project_pins WHERE owner_user_id=?",
                            (int(owner_user_id),)).fetchall()
        cursor = conn.execute(
            "DELETE FROM figure_projects WHERE owner_user_id=?",
            (int(owner_user_id),))
        removed = int(cursor.rowcount or 0)
        conn.execute("DELETE FROM figure_publications WHERE owner_user_id=?", (owner_user_id,))
        conn.execute("DELETE FROM figure_nature_bindings WHERE owner_user_id=?", (owner_user_id,))
        conn.execute("DELETE FROM figure_revision_exports WHERE owner_user_id=?",
                     (int(owner_user_id),))
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='figure_revisions'").fetchone():
            conn.execute("DELETE FROM figure_revisions WHERE owner_user_id=?", (int(owner_user_id),))
    _release_recorded_pins(int(owner_user_id), pins)
    return removed


def migrate_resource_references(*, legacy_export_artifact_id=None) -> dict[str, int]:
    """Protect current dependencies and retire legacy history before cleanup.

    Version 2 retains one current dependency set instead of every saved render.
    Pin files first, then attach the pin and hashes only if the complete project
    is unchanged; only then retire its snapshots and captured old pin identities.
    Missing/corrupt current files stop migration. Obsolete or orphaned pins left
    by interrupted cleanup are retried without selecting a newer current pin.
    This does not rewrite source, rendered bytes or timestamps, or delete files.
    """
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM figure_projects WHERE pins_version < 2").fetchall()
    migrated = 0
    for row in rows:
        project = _row_to_project(row)
        owner = int(project["owner_user_id"])
        figure_id = str(project["figure_id"])
        refs = project["data_refs_json"]
        exports = project["exports_json"]
        file_ids = [str(ref["file_id"]) for ref in refs
                    if isinstance(ref, dict) and ref.get("file_id")]
        file_ids.extend(str(item["file_id"]) for item in exports.values()
                        if isinstance(item, dict) and item.get("file_id"))
        resource_id, pinned = _pin_files(owner, figure_id, file_ids, refs,
            source=project["source_json"], session_id=project.get("session_id", ""))
        committed = False
        obsolete = []
        try:
            hashes = {ref["file_id"]: ref["sha256"] for ref in pinned}
            refs = [{**ref, "sha256": hashes[ref["file_id"]]} if isinstance(ref, dict)
                    and ref.get("file_id") in hashes else ref for ref in refs]
            with _connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT * FROM figure_projects WHERE figure_id=? AND owner_user_id=?",
                    (figure_id, owner)).fetchone()
                if _row_to_project(current) == project:
                    conn.execute(
                        "UPDATE figure_projects SET data_refs_json=?, pins_version=2 "
                        "WHERE figure_id=? AND owner_user_id=?",
                        (json.dumps(refs, ensure_ascii=False), figure_id, owner))
                    obsolete = _retire_pins(conn, figure_id, owner)
                    _record_pin(conn, resource_id, figure_id, owner, project["source_json"],
                                project.get("session_id", ""))
                    committed = True
            migrated += int(committed)
        except BaseException:
            committed = False
            raise
        finally:
            if not committed:
                _release_pin(owner, resource_id, source=project["source_json"],
                             session_id=project.get("session_id", ""))
        if committed:
            _release_recorded_pins(owner, obsolete)
    with _connect() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM figure_projects WHERE pins_version < 2").fetchone()[0]
        abandoned = conn.execute(
            "SELECT p.* FROM figure_project_pins p "
            "LEFT JOIN figure_projects f ON f.figure_id=p.figure_id "
            "AND f.owner_user_id=p.owner_user_id WHERE f.figure_id IS NULL OR p.obsolete=1").fetchall()
    for pin in abandoned:
        _release_recorded_pins(int(pin["owner_user_id"]), [pin])
    if pending:
        raise RuntimeError("figure reference migration raced with an update; retry cleanup later")
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck under the write lock before retiring the old snapshot table.
        if not conn.execute("SELECT 1 FROM figure_projects WHERE pins_version < 2 LIMIT 1").fetchone():
            conn.execute("DROP TABLE IF EXISTS figure_revisions")
    _pin_current_legacy_exports(legacy_export_artifact_id)
    return {"migrated": migrated, "released_abandoned": len(abandoned)}


def count_projects(owner_user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM figure_projects WHERE owner_user_id=?",
            (int(owner_user_id),)).fetchone()
    return int(row["n"]) if row else 0


__all__ = [
    "count_projects", "create_project", "delete_project", "get_project",
    "list_projects", "purge_account", "set_state_dir", "update_project",
    "update_project_committed",
]
