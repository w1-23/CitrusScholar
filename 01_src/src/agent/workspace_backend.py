"""One native file backend over the existing session Workspace storage.

This is an adapter, not a second store: every path, version, hash and byte is
owned by :class:`WorkspaceStorage` and the existing session file library. It is
what ``FilesystemMiddleware`` uses for the SDK's ``ls``/``read_file``/
``write_file``/``edit_file``/``delete``/``glob``/``grep`` tools, so the model
sees one file view instead of the native read set plus a private duplicate.

Concurrency without an explicit CAS argument
--------------------------------------------
The native tools carry no ``expected_version``/``expected_sha256``. Instead:

- the backend records the version and SHA-256 the *current run* has actually
  observed (reads record ``origin="read"``; listings record ``"listing"``);
- ``write_file`` may create a new path directly, and may overwrite an existing
  one only when this run read that exact version and hash;
- ``edit_file`` requires a read of the current version (an exact string match
  against unknown content is not a safe substitute);
- ``delete`` requires an observed current version and becomes a versioned
  tombstone: session bytes and published objects are never physically removed.

A stale observation is refused with ``stale_revision`` so the model re-reads
instead of overwriting another writer's version.
"""
from __future__ import annotations

from deepagents.backends.protocol import (BackendProtocol, DeleteResult, EditResult,
                                          FileDownloadResponse, FileUploadResponse,
                                          GlobResult, GrepResult, LsResult, ReadResult,
                                          WriteResult)
from deepagents.backends.utils import create_file_data, slice_read_response

from .workspace_storage import (WorkspaceConflict, WorkspaceStorage, _ensure_tables,
                                virtual_path)

READONLY_MOUNTS = frozenset({"input", "data", "skills", "01_src"})
WRITABLE_MOUNTS = frozenset({"workspace", "outputs"})


def _error(exc):
    """Map a bounded storage contract failure to a model-readable message."""
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, LookupError):
        return "file_not_found"
    return str(exc)[:300] or "workspace_operation_failed"


class CitrusWorkspaceBackend(BackendProtocol):
    """The SDK file protocol over one authenticated Workspace scope."""

    def __init__(self, owner: int, session: str, run_id: str):
        if (type(owner) is not int or owner <= 0 or not isinstance(session, str) or not session
                or not isinstance(run_id, str) or not run_id):
            raise ValueError("workspace_backend_scope_required")
        self._scope = (owner, session, run_id)
        self._storage = None

    @property
    def storage(self) -> WorkspaceStorage:
        # Built on first use so graph construction never depends on storage
        # availability; a denied scope still fails loudly at the first call.
        if self._storage is None:
            self._storage = WorkspaceStorage(*self._scope)
        return self._storage

    # -- helpers ---------------------------------------------------------
    def _observe(self, refs, *, origin):
        try:
            self.storage.remember(refs, origin=origin)
        except (OSError, ValueError):
            # Observation bookkeeping must never turn a successful read into an
            # error; without it a later edit/delete is simply refused as stale.
            return

    def _current(self, path):
        with self.storage.files._connect() as conn:
            _ensure_tables(conn)
            return self.storage._state(conn, path)

    # -- read tools ------------------------------------------------------
    def ls(self, path: str) -> LsResult:
        try:
            path = virtual_path(path, directory=True)
            if path == "/":
                from .workspace_projection import ROOTS
                return LsResult(entries=[{"path": root + "/", "is_dir": True} for root in ROOTS])
            backend, refs = self.storage.index(path)
            listed = backend.ls(path)
            if listed.error:
                return LsResult(error=listed.error)
            prefix = path.rstrip("/") + "/"
            merged = {}
            for key, ref in refs.items():
                if not key.startswith(prefix) or key == path:
                    continue
                remainder = key[len(prefix):]
                head, separator, _ = remainder.partition("/")
                entry = prefix + head + ("/" if separator else "")
                merged[entry] = {"path": entry, "is_dir": bool(separator),
                                 **({"size": ref.get("size_bytes", 0)} if not separator else {})}
            for entry in listed.entries or ():
                merged[entry["path"]] = {**merged.get(entry["path"], {}), **entry}
            self._observe({key: ref for key, ref in refs.items() if key.startswith(prefix)}, origin="listing")
            return LsResult(entries=[merged[key] for key in sorted(merged)])
        except (PermissionError, ValueError, OSError, UnicodeError) as exc:
            return LsResult(error=_error(exc))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            virtual_path(file_path)
            text, ref = self.storage.text(file_path)
        except (PermissionError, ValueError, LookupError, OSError, UnicodeError) as exc:
            return ReadResult(error=_error(exc))
        self._observe({file_path: ref}, origin="read")
        return slice_read_response(create_file_data(text), offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             *, max_count: int | None = None) -> GrepResult:
        try:
            scope = virtual_path(path or "/", directory=True)
            found = self.storage.search(pattern, path=scope,
                                        max_matches=min(max_count or 20, 50))
        except (PermissionError, ValueError, OSError, UnicodeError) as exc:
            return GrepResult(error=_error(exc))
        matches = [{"path": row["path"], "line": row["line"], "text": row["text"]}
                   for row in found["matches"]]
        self._observe({row["ref"]["path"]: row["ref"] for row in found["matches"] if row.get("ref")},
                      origin="listing")
        return GrepResult(matches=matches, truncated=bool(found["truncated"]))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        try:
            scope = virtual_path(path or "/", directory=True)
            backend, refs = self.storage.index(scope)
            found = backend.glob(pattern, scope)
        except (PermissionError, ValueError, OSError, UnicodeError) as exc:
            return GlobResult(error=_error(exc))
        if found.error:
            return found
        matched = [item["path"] for item in found.matches or () if item.get("path") in refs]
        self._observe({key: refs[key] for key in matched}, origin="listing")
        return found

    # -- write tools -----------------------------------------------------
    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            virtual_path(file_path, writable=True)
            state = self._current(file_path)
            if state is None:
                version, expected = 0, ""
            elif state["kind"] == "workspace_tombstone":
                # The path does not exist for this run: creating it again is a
                # normal native create, and the tombstone version is the CAS
                # predecessor.
                version, expected = state["version"], ""
            else:
                observed = self.storage.observed(file_path, read_only_ok=False)
                if (observed is None or observed["version"] != state["version"]
                        or observed["sha256"] != state["sha256"]):
                    return WriteResult(error=_stale(file_path))
                version, expected = state["version"], state["sha256"]
            saved = self.storage.save(file_path, content=content, expected_version=version,
                                      expected_sha256=expected)
        except WorkspaceConflict as exc:
            return WriteResult(error=_conflict(file_path, exc.latest_ref))
        except (PermissionError, ValueError, LookupError, OSError, UnicodeError) as exc:
            return WriteResult(error=_error(exc))
        self._observe({file_path: saved["ref"]}, origin="read")
        return WriteResult(path=file_path)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        try:
            virtual_path(file_path, writable=True)
            state = self._current(file_path)
            if state is None or state["kind"] != "workspace_file":
                return EditResult(error="file_not_found")
            observed = self.storage.observed(file_path, read_only_ok=False)
            if (observed is None or observed["version"] != state["version"]
                    or observed["sha256"] != state["sha256"]):
                return EditResult(error=_stale(file_path))
            saved = self.storage.save(file_path, old_string=old_string, new_string=new_string,
                                      replace_all=replace_all,
                                      expected_version=state["version"],
                                      expected_sha256=state["sha256"])
        except WorkspaceConflict as exc:
            return EditResult(error=_conflict(file_path, exc.latest_ref))
        except (PermissionError, ValueError, LookupError, OSError, UnicodeError) as exc:
            return EditResult(error=_error(exc))
        self._observe({file_path: saved["ref"]}, origin="read")
        return EditResult(path=file_path, occurrences=saved.get("occurrences"))

    def delete(self, file_path: str) -> DeleteResult:
        try:
            path = virtual_path(file_path, writable=True)
            state = self._current(path)
            if state is None or state["kind"] != "workspace_file":
                return DeleteResult(error="file_not_found")
            observed = self.storage.observed(path)
            if observed is None or observed["version"] != state["version"] \
                    or observed["sha256"] != state["sha256"]:
                return DeleteResult(error=_stale(path))
            self.storage.tombstone(path, expected_version=state["version"],
                                   expected_sha256=state["sha256"])
        except WorkspaceConflict as exc:
            return DeleteResult(error=_conflict(file_path, exc.latest_ref))
        except (PermissionError, ValueError, LookupError, OSError, UnicodeError) as exc:
            return DeleteResult(error=_error(exc))
        return DeleteResult(path=path)

    # -- bulk transport (used by the SDK and by host delivery adapters) ---
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses = []
        for path, content in files:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                responses.append(FileUploadResponse(path=path, error="invalid_path"))
                continue
            result = self.write(path, text)
            responses.append(FileUploadResponse(path=path, error=result.error))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses = []
        for path in paths:
            try:
                virtual_path(path)
                text, ref = self.storage.text(path)
            except PermissionError:
                responses.append(FileDownloadResponse(path=path, error="permission_denied"))
                continue
            except (ValueError, LookupError, OSError, UnicodeError):
                responses.append(FileDownloadResponse(path=path, error="file_not_found"))
                continue
            self._observe({path: ref}, origin="read")
            responses.append(FileDownloadResponse(path=path, content=text.encode("utf-8")))
        return responses


def _stale(path):
    return (f"stale_revision: {path} was not read at its current version in this run. "
            "Read it again, then retry.")


def _conflict(path, latest):
    version = latest.get("version") if isinstance(latest, dict) else None
    return (f"version_conflict: {path} is no longer at the observed version"
            + (f" (current version {version})" if version is not None else "")
            + ". Read it again, then retry.")
