"""Explicit application boundary for immutable artifact publication.

The legacy tool entry point remains the single persistence implementation.
HTTP/workspace callers use this adapter instead of writing bytes and inventing
artifact URLs themselves. A copied context prevents identity changes leaking
back into the calling thread or coroutine.
"""
from __future__ import annotations

import hashlib
from contextvars import copy_context
from typing import Any


def publish_artifact(*, owner: int, session_id: str, filename: str,
                     content: bytes, mime: str, producer_tool: str,
                     request_id: str = "", source_file_id: str = "",
                     source_sha256: str = "", logical_task_id: str = "") -> dict[str, Any]:
    """Publish via the existing validated, hashed, cancellation-aware service.

    owner/session must come from the authenticated application context; callers
    must authorize the session before invoking this internal service.
    """
    if int(owner) <= 0 or not session_id:
        raise ValueError("缺少已授权账号或会话，无法发布文件。")

    def publish() -> dict[str, Any]:
        from src.core import tracing
        from src.research.tools import _save_artifact
        from src.session import files

        tracing.set_user_id(int(owner))
        tracing.set_session_id(session_id)
        tracing.set_current_tool_name(producer_tool)
        tracing.set_client_request_id(request_id)
        url = _save_artifact(filename, content, mime,
                             source_file_id=source_file_id,
                             source_sha256=source_sha256,
                             logical_task_id=logical_task_id)
        artifact_id = url.rsplit("/", 1)[-1]
        entry = next((row for row in files.list_files(session_id, int(owner))
                      if row.get("artifact_id") == artifact_id), None)
        if not entry or not entry.get("file_id"):
            raise ValueError("产物发布后缺少文件登记，无法交付。")
        # _save_artifact may normalize OOXML, so its verified registered digest
        # is authoritative. Binary figure formats are unchanged by publication.
        if mime in {"image/svg+xml", "image/png", "application/pdf"}:
            if entry.get("sha256") != hashlib.sha256(content).hexdigest():
                raise ValueError("发布图件的摘要与渲染结果不一致。")
        return {
            "artifact_id": artifact_id, "file_id": entry["file_id"],
            "filename": entry.get("filename", filename),
            "sha256": entry.get("sha256", ""),
            "size": entry.get("size_bytes", 0),
            "download_url": entry.get("download_url", ""),
            "artifact_url": url,
        }

    return copy_context().run(publish)


FIGURE_STAGE = "figure-stage:"
_FIGURE_PRODUCER = "figure_workspace"


def stage_figure_artifact(*, owner: int, session_id: str, filename: str,
                          content: bytes, mime: str, request_id: str) -> dict:
    """Register immutable bytes privately. A project transaction authorizes promotion."""
    import os
    import sqlite3
    import time
    from contextlib import closing
    from uuid import uuid4

    from src.core.cancel import raise_if_cancelled
    from src.core.deliverable_check import validate_deliverable
    from src.core.publication_reconcile import _owner_valid, _verify_bytes
    from src.research import tools
    from src.session import files

    if not files.valid_session(session_id) or not _owner_valid(session_id, owner):
        raise ValueError("Invalid figure publication owner or session")
    filename = tools._safe_name(filename)
    validate_deliverable(filename=filename, content=content, mime=mime, strict=False)
    artifact_id = uuid4().hex
    digest = hashlib.sha256(content).hexdigest()
    root = tools.PROJECT_ROOT.resolve() / "state" / "artifacts" / session_id
    if any(parent.is_symlink() for parent in (root, root.parent, root.parent.parent)):
        raise ValueError("Unsafe figure publication path")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / f"{artifact_id}_{filename}"
    file_id = ""
    created = False
    try:
        raise_if_cancelled("artifact:before_write")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _verify_bytes({"session_id": session_id, "artifact_id": artifact_id, "filename": filename,
                       "artifact_sha256": digest, "artifact_size_bytes": len(content),
                       "path": str(path)}, project_root=tools.PROJECT_ROOT)
        row = files.register_file(owner_user_id=owner, session_id=session_id, filename=filename,
            mime=mime, origin=files.ORIGIN_GENERATED, artifact_id=artifact_id,
            storage_path=str(path), visibility="internal", producer_tool=_FIGURE_PRODUCER,
            dedupe_key=FIGURE_STAGE + artifact_id)
        file_id = row["file_id"]
        if row["sha256"] != digest or row["size_bytes"] != len(content):
            raise ValueError("Figure publication checksum mismatch")
        raise_if_cancelled("artifact:before_commit")
        with closing(sqlite3.connect(str(tools._db_path()), timeout=30)) as conn, conn:
            conn.execute("INSERT INTO artifacts(artifact_id,session_id,filename,mime,created_at,path,"
                         "logical_artifact_id,artifact_sha256,artifact_size_bytes,owner_user_id,"
                         "producer_tool,logical_task_id,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (artifact_id, session_id, filename, mime, time.time(), str(path), artifact_id,
                          digest, len(content), owner, _FIGURE_PRODUCER, FIGURE_STAGE + file_id,
                          request_id[:128]))
    except BaseException:
        if file_id:
            discard_staged_figures(owner, session_id, [{"file_id": file_id, "artifact_id": artifact_id}])
        elif created:
            path.unlink(missing_ok=True)
        raise
    return {"artifact_id": artifact_id, "file_id": file_id, "filename": filename,
            "sha256": digest, "size": len(content), "download_url": f"/api/v2/files/{file_id}/download",
            "artifact_url": f"/api/v2/session/{session_id}/artifacts/{artifact_id}"}


def discard_staged_figures(owner: int, session_id: str, entries: list[dict]) -> None:
    """Clean only still-private, unpinned, exact staged objects after a failed CAS."""
    import logging
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    from src.research import tools
    from src.session import files

    for entry in entries:
        try:
            with files._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT storage_path FROM session_files WHERE file_id=? AND "
                    "session_id=? AND owner_user_id=? AND artifact_id=? AND visibility='internal' "
                    "AND producer_tool=? AND dedupe_key=? AND " + files._UNREFERENCED,
                    (entry["file_id"], session_id, owner, entry["artifact_id"], _FIGURE_PRODUCER,
                     FIGURE_STAGE + entry["artifact_id"])).fetchone()
                if row is None:
                    continue
                path = Path(row["storage_path"])
                root = tools.PROJECT_ROOT.resolve() / "state" / "artifacts" / session_id
                if path.parent != root or not path.name.startswith(entry["artifact_id"] + "_"):
                    continue
                with closing(sqlite3.connect(str(tools._db_path()), timeout=30)) as db, db:
                    db.execute("DELETE FROM artifacts WHERE artifact_id=? AND session_id=? "
                               "AND owner_user_id=? AND logical_task_id=?",
                               (entry["artifact_id"], session_id, owner, FIGURE_STAGE + entry["file_id"]))
                path.unlink(missing_ok=True)
                conn.execute("DELETE FROM session_files WHERE file_id=?", (entry["file_id"],))
        except Exception:
            logging.getLogger(__name__).warning("private figure stage cleanup deferred", exc_info=True)


def figure_artifact_visible(row) -> bool:
    """Both artifact URLs consult the file library's atomic visibility decision."""
    marker = str(row["logical_task_id"] or "")
    if not marker.startswith(FIGURE_STAGE):
        return True
    from src.session import files

    with files._connect() as conn:
        return conn.execute("SELECT 1 FROM session_files WHERE file_id=? AND session_id=? "
            "AND owner_user_id=? AND artifact_id=? AND visibility='public' AND producer_tool=?",
            (marker[len(FIGURE_STAGE):], row["session_id"], row["owner_user_id"],
             row["artifact_id"], _FIGURE_PRODUCER)).fetchone() is not None


def verify_nature_original(owner, session_id, execution_id, request_id, filename, registered, entry):
    """Cross-check the exact immutable raw worker output, not just the producer label."""
    import sqlite3
    from contextlib import closing

    from src.research import tools

    artifact_id = registered.get("artifact_id")
    if not artifact_id or artifact_id != entry.get("artifact_id"):
        raise ValueError("Nature original artifact identity mismatch")
    with closing(sqlite3.connect(str(tools._db_path()), timeout=30)) as conn:
        row = conn.execute("SELECT 1 FROM artifacts WHERE artifact_id=? AND session_id=? "
            "AND owner_user_id=? AND filename=? AND producer_tool='run_nature_plot' "
            "AND logical_task_id=? AND request_id=? AND artifact_sha256=? AND artifact_size_bytes=?",
            (artifact_id, session_id, owner, filename, execution_id, request_id[:128],
             entry["sha256"], entry["size"])).fetchone()
    if row is None:
        raise ValueError("Nature original does not belong to the exact worker execution")
