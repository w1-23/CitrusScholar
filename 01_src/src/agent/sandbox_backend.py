"""Native ``execute`` at the single Deep Agents file surface.

``FilesystemMiddleware`` registers its ``execute`` tool only for a backend that
implements ``SandboxBackendProtocol``. This adapter supplies exactly that, over
the machinery that already exists:

- every file operation is the lead's :class:`CitrusWorkspaceBackend`, so the
  native ``ls``/``read_file``/``write_file``/``edit_file``/``delete``/``glob``/
  ``grep`` tools and the ``execute`` tool share one persisted Workspace, one
  version index and one CAS contract. Nothing is stored twice.
- ``execute`` is :mod:`src.code_execution.execution_bridge`: the command becomes
  data inside a bounded Python wrapper that runs ``/bin/sh -c`` only inside the
  offline Code Worker sandbox (network denied, non-root, no Docker socket, no
  host directory mounted). When the Worker is absent or offline execution is
  disabled the call reports "not run" and stops there. This class never imports
  ``subprocess``/``os.system``/a local shell, so no host fallback exists here.

Trust boundary: the model chooses the command text and nothing else. Identity,
timeout, limits, network policy, environment image, output caps and the
write-back paths are all host decisions made below this line or by the Worker.
"""
from __future__ import annotations

from deepagents.backends.protocol import (DeleteResult, EditResult, ExecuteResponse,
                                          FileDownloadResponse, FileUploadResponse,
                                          GlobResult, GrepResult, LsResult, ReadResult,
                                          SandboxBackendProtocol, WriteResult)

from src.code_execution import execution_bridge

from .native_execute_receipt import backend_evidence
from .workspace_backend import CitrusWorkspaceBackend
from .workspace_storage import virtual_path

__all__ = ["CitrusSandboxBackend"]

# One session Workspace file is a bounded UTF-8 text object (see
# workspace_storage.MAX_FILE_BYTES); larger or binary results stay in the
# Worker's staging area and are reported by path + SHA-256 instead.
WRITEBACK_MAX_BYTES = 65536


class _SessionFiles:
    """The bridge's narrow write-back view over one authenticated session."""

    def __init__(self, storage):
        self._storage = storage

    def refs(self):
        """Current session paths with the exact pinned file id/version/hash."""
        _, refs = self._storage.index("/")
        return refs

    def write_back(self, path, data, *, expected_version, expected_sha256):
        """Store verified bytes as a new version of one writable session path."""
        virtual_path(path, writable=True)
        if not isinstance(data, bytes) or len(data) > WRITEBACK_MAX_BYTES:
            raise ValueError("file_too_large_for_workspace")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("binary_not_stored_in_workspace")
        if "\x00" in text:
            raise ValueError("binary_not_stored_in_workspace")
        saved = self._storage.save(path, content=text, expected_version=expected_version,
                                   expected_sha256=expected_sha256)
        ref = saved["ref"]
        return {"version": ref["version"], "sha256": ref["sha256"]}


class CitrusSandboxBackend(SandboxBackendProtocol):
    """One admitted run's native file surface plus isolated command execution."""

    def __init__(self, owner: int, session: str, run_id: str):
        self._files = CitrusWorkspaceBackend(owner, session, run_id)
        self._scope = (owner, session, run_id)

    @property
    def id(self) -> str:
        """Stable sandbox identity; the scope is never inferred from content."""
        return "citrus-sandbox:%d:%s:%s" % self._scope

    # -- isolated execution ---------------------------------------------
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run one shell command inside the offline Code Worker sandbox."""
        owner, session, run_id = self._scope
        evidence = backend_evidence.get()
        if evidence is not None:
            evidence.entered = True
        try:
            view = _SessionFiles(self._files.storage)
        except Exception:
            if evidence is not None:
                evidence.not_run_reason = "execution_session_unavailable"
            return ExecuteResponse(output="Error: execute not run (execution_session_unavailable).")
        try:
            result = execution_bridge.execute(owner, session, run_id, command,
                                              timeout=timeout, view=view)
        except Exception:  # an unexpected error may have followed submission
            return ExecuteResponse(output="The command's execution status could not be confirmed.")
        if evidence is not None:
            evidence.not_run_reason = result.not_run_reason
        return ExecuteResponse(output=result.output, exit_code=result.exit_code,
                               truncated=result.truncated)

    # -- the inherited file protocol, delegated unchanged -----------------
    def ls(self, path: str) -> LsResult:
        return self._files.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._files.read(file_path, offset, limit)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None,
             *, max_count: int | None = None) -> GrepResult:
        return self._files.grep(pattern, path, glob, max_count=max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._files.glob(pattern, path)

    def write(self, file_path: str, content: str) -> WriteResult:
        return self._files.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        return self._files.edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        return self._files.delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._files.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._files.download_files(paths)
