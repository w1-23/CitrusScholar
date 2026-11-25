"""Server-only provenance for opening a delivered Code SVG in the workbench."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from src.code_execution.protocol import ExecutionRequest, FileSnapshot, identifier


def delivery_key(execution_id, path, digest):
    # This is the existing Code artifact identifier. Retain it so already
    # delivered outputs remain importable without rewriting publication rows.
    value = json.dumps([execution_id, path, digest], ensure_ascii=False, separators=(",", ":"))
    return "code_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_delivered_svg(owner, session_id, entry):
    """Return exact execution provenance, never identity supplied by a client."""
    from src.config import PROJECT_ROOT
    from src.core.account_leases import account_lease
    from src.research import tools
    from src.session import files
    from .repository import ExecutionRepository

    artifact_id = identifier(entry.get("artifact_id"))
    if (entry.get("producer_tool") != "code_deliver" or entry.get("origin") != files.ORIGIN_GENERATED
            or entry.get("visibility") != "public" or entry.get("session_id") != session_id
            or entry.get("mime_type") != "image/svg+xml"):
        raise ValueError("code_svg_publication_scope_mismatch")
    filename, digest, size = (entry[key] for key in ("filename", "sha256", "size_bytes"))
    with closing(sqlite3.connect(str(tools._db_path()), timeout=30)) as conn:
        conn.row_factory = sqlite3.Row
        published = conn.execute("SELECT source_file_id,logical_task_id,path FROM artifacts "
            "WHERE artifact_id=? AND owner_user_id=? AND session_id=? AND filename=? "
            "AND producer_tool='code_deliver' AND mime='image/svg+xml' "
            "AND source_sha256=? AND artifact_sha256=? AND artifact_size_bytes=?",
            (artifact_id, owner, session_id, filename, digest, digest, size)).fetchone()
    if published is None or entry["path"] != Path(published["path"]):
        raise ValueError("code_svg_publication_unverified")
    file_id = identifier(published["source_file_id"])
    with files._connect() as conn:
        # Resolve the historical binding by source file, without trusting an
        # execution ID from the browser or interpreting a filename as identity.
        matches = conn.execute("SELECT f.*, w.task_id, w.metadata FROM task_workspace_files f "
            "JOIN task_workspaces w ON w.workspace_id=f.workspace_id "
            "WHERE w.owner=? AND w.session=? AND f.file_id=? AND f.sha256=? "
            "AND f.mime_type='image/svg+xml' AND f.size_bytes=? LIMIT 2",
            (owner, session_id, file_id, digest, size)).fetchall()
    if len(matches) != 1:
        raise ValueError("code_svg_source_binding_unverified")
    row = matches[0]
    source = json.loads(row["source"])
    if json.loads(row["metadata"]).get("code_scope") in {"citrus.fusion-worker/1", "citrus.nature-plot/1"}:
        raise ValueError("code_svg_private_scope_denied")
    execution_id = identifier(source.get("execution_id"))
    ref = {key: row[key] for key in ("path", "file_id", "version", "sha256", "size_bytes", "mime_type")}
    FileSnapshot(**{key: value for key, value in ref.items() if key != "mime_type"})
    if (published["logical_task_id"] != delivery_key(execution_id, row["path"], digest)
            or filename != tools._safe_name(row["path"].rsplit("/", 1)[-1])):
        raise ValueError("code_svg_delivery_binding_mismatch")
    repository = ExecutionRepository(PROJECT_ROOT / "state" / "code_executions.sqlite3")
    execution = repository.get(execution_id, owner=owner, session=session_id)
    request = ExecutionRequest.parse(execution["request"])
    receipt = execution["receipt"]
    if (execution["status"] != "succeeded" or request.identity.owner_user_id != owner
            or request.identity.session_id != session_id or request.workspace_id != row["workspace_id"]
            or request.task_id != row["task_id"] or row["path"] not in request.outputs
            or type(receipt) is not dict or receipt.get("process_stopped") is not True
            or receipt.get("execution_id") != execution_id or receipt.get("fence") != execution["fence"]
            or source.get("fence") != execution["fence"]
            or receipt.get("request_fingerprint") != request.fingerprint
            or source.get("request_fingerprint") != request.fingerprint
            or type(receipt.get("files")) is not list
            or [item for item in receipt["files"] if isinstance(item, dict) and item.get("path") == row["path"]] != [ref]):
        raise ValueError("code_svg_execution_receipt_unverified")
    with account_lease(owner) as lease:
        if lease.epoch != request.account_epoch:
            raise ValueError("code_svg_account_epoch_changed")
    return {"execution_id": execution_id, "artifact_id": artifact_id, "output_path": row["path"],
            "request_fingerprint": request.fingerprint}
