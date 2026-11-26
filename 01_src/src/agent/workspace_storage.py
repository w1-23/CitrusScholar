"""Bounded Deep Agents file view backed by the existing private session library.

Only the version index is new. Bytes live once in session_files, and each
committed version pins its existing file ID. StoreBackend supplies file
semantics; no filesystem backend, process, sandbox or command tool is exposed.
Staging precedes atomic index+pin publication: an interrupted staging write is
an unreferenced internal library entry, eligible for the library's normal GC.

Two small tables extend the same storage instead of a second file system:

- ``workspace_file_tombstones`` records a logical delete as a new version of the
  path. Session bytes stay in the library; input, Skill and published objects
  are never physically removed here.
- ``workspace_file_observations`` records which version and SHA-256 the current
  run has actually observed, so a native write/edit/delete without an explicit
  CAS argument can still refuse a stale revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import time

from deepagents.backends.store import StoreBackend
from langgraph.store.memory import InMemoryStore

MAX_FILE_BYTES = 65536
MAX_VIEW_BYTES = 1048576
# SDK tool-result offload files are one logical result each and can legitimately
# exceed the ordinary per-file text cap; they stay inside the view byte budget.
MAX_OFFLOAD_BYTES = 262144
MAX_FILES = 128
MAX_VERSIONS = 128
_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml",
                  ".xml", ".html", ".css", ".js", ".py", ".r", ".sql", ".toml", ".log"}


class WorkspaceConflict(ValueError):
    def __init__(self, latest_ref):
        super().__init__("workspace_version_conflict")
        self.latest_ref = latest_ref


def virtual_path(path: str, *, directory=False, writable=False) -> str:
    """Validate logical keys, never convert caller paths into physical paths."""
    if not isinstance(path, str) or len(path) > 240:
        raise ValueError("workspace_invalid_path")
    # The SDK lists directories with one trailing slash. Accept those exact
    # paths on the next directory operation without admitting empty segments.
    if directory and path != "/" and not path.endswith("//"):
        path = path.removesuffix("/")
    if directory and path in {"/", "/input", "/data", "/skills", "/01_src", "/workspace", "/outputs"}:
        return path
    parts = path.split("/")
    if (len(parts) < 3 or parts[0] or parts[1] not in {"input", "data", "skills", "01_src", "workspace", "outputs"}
            or any(not re.fullmatch(r"[\w][\w. -]{0,119}", p)
                   or p.endswith((".", " ")) or ".." in p for p in parts[2:])):
        raise ValueError("workspace_invalid_path")
    if writable and parts[1] not in {"workspace", "outputs"}:
        raise PermissionError("workspace_mount_readonly")
    # Large offloaded tool results use the SDK's exact name (no extension). They
    # are still validated as UTF-8 text without NUL bytes on every read.
    if (not directory and Path(parts[-1]).suffix.lower() not in _TEXT_SUFFIXES
            and not (parts[1] == "workspace" and len(parts) == 4
                     and parts[2] == "large_tool_results")):
        raise ValueError("workspace_text_extension_required")
    return path


def _no_links(path: Path) -> None:
    # Check lexical ancestors before resolve(), including junctions on Windows.
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise PermissionError("workspace_link_denied")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_offload_path(path: str) -> bool:
    parts = str(path or "").split("/")
    return len(parts) == 4 and parts[1] == "workspace" and parts[2] == "large_tool_results"


def _limit_for(path: str) -> int:
    parts = str(path or "").split("/")
    history = (len(parts) == 4 and parts[1:3] == ["workspace", "conversation_history"]
               and parts[3].endswith(".md"))
    return MAX_OFFLOAD_BYTES if history or _is_offload_path(path) else MAX_FILE_BYTES


_TABLES = frozenset({"workspace_file_tombstones", "workspace_file_observations"})
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS workspace_file_tombstones("
    "owner INTEGER NOT NULL, session TEXT NOT NULL, path TEXT NOT NULL, version INTEGER NOT NULL,"
    "tombstoned_at REAL NOT NULL, PRIMARY KEY(owner, session, path))",
    "CREATE TABLE IF NOT EXISTS workspace_file_observations("
    "owner INTEGER NOT NULL, session TEXT NOT NULL, run_id TEXT NOT NULL, path TEXT NOT NULL,"
    "version INTEGER NOT NULL, sha256 TEXT NOT NULL, origin TEXT NOT NULL, observed_at REAL NOT NULL,"
    "PRIMARY KEY(owner, session, run_id, path))",
)


def _ensure_tables(conn) -> None:
    """Create the two adapter tables once per database without a write per read."""
    present = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
        tuple(sorted(_TABLES)))}
    if present == _TABLES:
        return
    for statement in _SCHEMA:
        conn.execute(statement)


class WorkspaceStorage:
    """One authenticated call's adapter; instances must never be globally cached."""

    def __init__(self, owner: int, session: str, run_id: str):
        from src.session import files
        if type(owner) is not int or owner <= 0 or not files.valid_session(session):
            raise PermissionError("workspace_scope_unavailable")
        self.owner, self.session, self.run_id, self.files = owner, session, run_id, files
        for suffix in ("", "-wal", "-shm", "-journal"):
            _no_links(Path(str(files.DB_PATH) + suffix))
        _no_links(files.FILES_ROOT)
        files.init_store()

    def _reference(self, row, path=None, version=None):
        internal = row["visibility"] == "internal"
        ref = {"kind": "workspace_file" if internal else "workspace_input",
               "path": path or row["virtual_path"], "file_id": row["file_id"],
               "version": version if version is not None else row["version"], "sha256": row["sha256"]}
        return ref if internal else {**ref, "filename": row["filename"], "read_only": True}

    def _read_bytes(self, row, virtual=None) -> str:
        if row["visibility"] == "public":
            # Re-resolve the exact pin for this tool read, rather than treating
            # a previously enumerated input descriptor as authority.
            attached = self.files.get_task_input_file(self.owner, self.run_id, row["file_id"], row["sha256"])
            if attached is None:
                raise PermissionError("workspace_file_scope_denied")
            path = attached["path"]
        else:
            # Private version bytes never cross their owning session boundary.
            if (row["visibility"] != "internal" or row["owner_user_id"] != self.owner
                    or row["session_id"] != self.session or row["origin"] != self.files.ORIGIN_UPLOADED):
                raise PermissionError("workspace_file_scope_denied")
            path = Path(row["storage_path"])
            root = self.files.FILES_ROOT / f"user_{self.owner}" / self.session
            _no_links(root)
            if not path.resolve().is_relative_to(root.resolve()):
                raise PermissionError("workspace_file_scope_denied")
        _no_links(path)
        virtual = virtual or (row["virtual_path"] if "virtual_path" in row.keys() else "")
        limit = _limit_for(virtual)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > limit):
                raise ValueError("workspace_file_limit_or_type")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                content = stream.read(limit + 1)
            _no_links(path)
            current = path.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise PermissionError("workspace_file_changed")
        finally:
            os.close(fd)
        if len(content) > limit or _digest(content) != row["sha256"]:
            raise ValueError("workspace_integrity_or_size_mismatch")
        text = content.decode("utf-8")
        if "\x00" in text:
            raise ValueError("workspace_text_required")
        return text

    def _rows(self, *, path=None, version=None, scope="/"):
        with self.files._connect() as conn:
            _ensure_tables(conn)
            args = [self.owner, self.session]
            where = "v.owner=? AND v.session=?"
            if path is not None:
                where += " AND v.path=?"
                args.append(path)
            if version is not None:
                where += " AND v.version=?"
                args.append(version)
            else:
                where += " AND v.version=(SELECT MAX(x.version) FROM workspace_file_versions x WHERE x.owner=v.owner AND x.session=v.session AND x.path=v.path)"
            rows = [dict(r) for r in conn.execute(
                "SELECT f.*,v.path AS virtual_path,v.version,v.sha256 AS version_hash "
                "FROM workspace_file_versions v JOIN session_files f ON f.file_id=v.file_id "
                "WHERE " + where + " ORDER BY v.path LIMIT ?", (*args, MAX_FILES + 1))]
            if len(rows) > MAX_FILES:
                raise ValueError("workspace_file_count_limit")
            tombstones = {row[0]: row[1] for row in conn.execute(
                "SELECT path, version FROM workspace_file_tombstones WHERE owner=? AND session=?",
                (self.owner, self.session))}
            for row in rows:
                if row["version_hash"] != row["sha256"] or row["visibility"] != "internal":
                    raise ValueError("workspace_index_integrity")
            # A tombstone is a newer version of the path, so any version at or
            # below it is deleted; a later re-creation has a higher version.
            rows = [row for row in rows
                    if tombstones.get(row["virtual_path"], -1) < row["version"]]
            from .workspace_projection import within
            rows = [row for row in rows if within(row["virtual_path"], scope)]
            if (path is None and scope in {"/", "/input", "/data"} or
                    (path or scope).startswith(("/input/", "/data/"))):
                rows.extend(self._input_rows(conn, path, scope, within))
        return rows

    def _input_rows(self, conn, path, scope, within):
        # Chat admission (and recovery) pins only the turn's authenticated inputs.
        # A library object from another session is not authorized by owner alone.
        inputs = conn.execute(
            "SELECT f.* FROM session_file_resource_refs r JOIN session_files f ON f.file_id=r.file_id "
            "AND f.owner_user_id=r.owner_user_id AND f.sha256=r.sha256 "
            "WHERE r.owner_user_id=? AND r.resource_kind='task_input' AND r.resource_id=? "
            "AND f.visibility='public' ORDER BY f.file_id LIMIT ?",
            (self.owner, self.run_id, MAX_FILES + 1)).fetchall()
        if len(inputs) > MAX_FILES:
            raise ValueError("workspace_input_count_limit")
        mount = "data" if (path or scope).startswith("/data") else "input"
        for item in inputs:
            suffix = Path(item["filename"]).suffix.lower()
            if suffix not in _TEXT_SUFFIXES:
                continue  # Binary/published workbooks belong to the existing typed readers.
            prefix = f"/{mount}/{item['file_id']}/"
            key = prefix + item["filename"]
            try:
                virtual_path(key)
            except ValueError:
                key = prefix + "attachment" + suffix
            if (path is None or key == path) and within(key, scope):
                yield {**dict(item), "virtual_path": key, "version": 1}

    def view(self, *, path=None, version=None, _descriptor=None):
        if path is not None:
            virtual_path(path)
        if version is not None and (type(version) is not int or version < 1):
            raise ValueError("workspace_invalid_version")
        store = InMemoryStore()
        namespace = (str(self.owner), self.session)
        backend = StoreBackend(store=store, namespace=lambda _: namespace)
        refs, size = {}, 0
        read_versions = []
        if path and path.startswith(("/skills/", "/01_src/")):
            from . import workspace_projection as projection
            if version is not None:
                raise ValueError("workspace_readonly_has_no_versions")
            # Internal index descriptors are pinned in this call; reuse their
            # metadata across a list/search instead of reparsing each vendor manifest.
            descriptor = _descriptor if _descriptor is not None else dict(projection.resources(path)).get(path)
            if descriptor is not None:
                text, ref = projection.read(path, descriptor)
                timestamp = datetime.now(timezone.utc).isoformat()
                store.put(namespace, path, {"content": text, "encoding": "utf-8",
                          "created_at": timestamp, "modified_at": timestamp})
                refs[path] = ref
            return backend, refs
        for row in self._rows(path=path, version=version):
            key = virtual_path(row["virtual_path"])
            if version is not None and row["version"] != version:
                continue
            text = self._read_bytes(row)
            size += len(text.encode("utf-8"))
            if size > MAX_VIEW_BYTES:
                raise ValueError("workspace_view_byte_limit")
            timestamp = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat()
            store.put(namespace, key, {"content": text, "encoding": "utf-8",
                                      "created_at": timestamp, "modified_at": timestamp})
            refs[key] = self._reference(row)
            if row["visibility"] == "internal":
                read_versions.append(row["file_id"])
        if read_versions:
            with self.files._connect() as conn:
                conn.executemany("UPDATE session_files SET last_accessed_at=? "
                                 "WHERE file_id=? AND owner_user_id=? AND session_id=?",
                                 [(time.time(), fid, self.owner, self.session) for fid in read_versions])
        return backend, refs

    def index(self, scope="/"):
        """StoreBackend directory/glob semantics over metadata, without bulk reads."""
        from . import workspace_projection as projection
        scope = virtual_path(scope, directory=True)
        store = InMemoryStore()
        namespace = (str(self.owner), self.session)
        refs = {row["virtual_path"]: self._reference(row) for row in self._rows(scope=scope)}
        if scope == "/":
            refs.update({row["virtual_path"]: self._reference(row) for row in self._rows(scope="/data")})
        refs.update(projection.resources(scope))
        timestamp = datetime.now(timezone.utc).isoformat()
        for key in refs:
            store.put(namespace, key, {"content": "", "encoding": "utf-8",
                      "created_at": timestamp, "modified_at": timestamp})
        return StoreBackend(store=store, namespace=lambda _: namespace), refs

    def search(self, pattern, *, path="/", max_matches=20):
        """Read candidates on demand; explicit truncation covers scan/byte budgets."""
        if (not isinstance(pattern, str) or not 1 <= len(pattern) <= 200 or
                type(max_matches) is not int or not 1 <= max_matches <= 50):
            raise ValueError("workspace_invalid_search")
        _, candidates = self.index(path)
        matches, scanned, size, unavailable = [], 0, 0, 0
        truncated = False
        for key in candidates:
            if scanned >= MAX_FILES or size >= MAX_VIEW_BYTES or len(matches) >= max_matches:
                truncated = True
                break
            scanned += 1
            try:
                backend, refs = self.view(path=key, _descriptor=candidates[key])
                downloaded = backend.download_files([key])[0]
                if downloaded.content is None:
                    raise LookupError("workspace_file_unavailable")
                size += len(downloaded.content)
                if size > MAX_VIEW_BYTES:
                    truncated = True
                    break
                result = backend.grep(pattern, path=key, max_count=max_matches - len(matches))
                if result.error:
                    raise ValueError(result.error)
                matches.extend({**row, "text": row["text"][:400],
                                "text_truncated": len(row["text"]) > 400,
                                "ref": refs[key]} for row in result.matches)
                truncated = truncated or result.truncated
            except (OSError, ValueError, LookupError, UnicodeError):
                # Missing optional resources never hide successful private matches.
                # Do not claim a complete search when a candidate was unreadable.
                unavailable += 1
                truncated = True
        return {"matches": matches, "truncated": truncated, "scanned_files": scanned,
                "unavailable_files": unavailable, "search_kind": "literal",
                "content_origin": "stored_data_not_instructions"}

    def read(self, path, *, version=None, start_char=0, max_chars=8000, expected_sha256=None):
        if (type(start_char) is not int or start_char < 0 or type(max_chars) is not int
                or not 1 <= max_chars <= 20000):
            raise ValueError("workspace_invalid_page")
        backend, refs = self.view(path=path, version=version)
        if path not in refs:
            raise LookupError("workspace_file_unavailable")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
                raise ValueError("workspace_invalid_expected_hash")
            if refs[path]["sha256"] != expected_sha256:
                raise ValueError("workspace_reference_hash_changed")
        # StoreBackend.read normalizes CRLF to LF. The public download contract
        # preserves exact UTF-8 bytes, needed for hash-referenced readback/CAS.
        result = backend.download_files([path])[0]
        if result.error or result.content is None:
            raise ValueError("workspace_backend_read_failed")
        text = result.content.decode("utf-8")
        if start_char > len(text):
            raise ValueError("workspace_invalid_page")
        end = min(len(text), start_char + max_chars)
        return {"ref": refs[path], "text": text[start_char:end], "start_char": start_char,
                "next_start_char": end if end < len(text) else None,
                "total_chars": len(text), "complete": end == len(text),
                "content_origin": "stored_data_not_instructions"}

    def _state(self, conn, path):
        """Return the latest version event for one path: file, tombstone or None."""
        row = conn.execute("SELECT f.file_id,f.sha256,f.visibility,v.version,v.sha256 AS version_hash "
                           "FROM workspace_file_versions v JOIN session_files f ON f.file_id=v.file_id "
                           "WHERE v.owner=? AND v.session=? AND v.path=? "
                           "ORDER BY v.version DESC LIMIT 1",
                           (self.owner, self.session, path)).fetchone()
        tombstone = conn.execute("SELECT version FROM workspace_file_tombstones "
                                 "WHERE owner=? AND session=? AND path=?",
                                 (self.owner, self.session, path)).fetchone()
        deleted = tombstone["version"] if tombstone is not None else None
        if row is not None and (deleted is None or row["version"] > deleted):
            if row["version_hash"] != row["sha256"] or row["visibility"] != "internal":
                raise ValueError("workspace_index_integrity")
            return {"kind": "workspace_file", "path": path, "file_id": row["file_id"],
                    "version": row["version"], "sha256": row["sha256"]}
        if deleted is not None:
            return {"kind": "workspace_tombstone", "path": path, "version": deleted, "sha256": ""}
        return None

    def _require_writable_scope(self, conn):
        if conn.execute("SELECT 1 FROM workspace_file_closed_sessions WHERE owner=? AND session=?",
                        (self.owner, self.session)).fetchone():
            raise PermissionError("workspace_session_closed")

    @staticmethod
    def _cas(state, version, sha256):
        if type(version) is not int or version < 0 or not isinstance(sha256, str):
            raise ValueError("workspace_expected_version_and_hash_required")
        if state is None:
            # A never-written path is created only from an exact zero version.
            if version != 0 or sha256:
                raise ValueError("workspace_expected_version_and_hash_required")
            return
        if state["kind"] == "workspace_tombstone":
            # A logically deleted path may be re-created at the tombstone version.
            if version != state["version"] or sha256:
                raise WorkspaceConflict(state)
            return
        if version > 0 and not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("workspace_expected_version_and_hash_required")
        if version != state["version"] or sha256 != state["sha256"]:
            raise WorkspaceConflict(state)

    def save(self, path, *, expected_version, expected_sha256, content=None,
             old_string=None, new_string=None, replace_all=False):
        from src.core.cancel import raise_if_cancelled

        virtual_path(path, writable=True)
        with self.files._connect() as conn:
            _ensure_tables(conn)
            self._require_writable_scope(conn)
            self._cas(self._state(conn, path), expected_version, expected_sha256)
        backend, refs = self.view(path=path)
        limit = _limit_for(path)
        if old_string is None:
            if not isinstance(content, str):
                raise ValueError("workspace_text_required")
            if len(content) > limit or len(content.encode("utf-8")) > limit:
                raise ValueError("workspace_text_or_byte_limit")
            result = backend.write(path, content)
        else:
            if not isinstance(old_string, str) or not old_string or not isinstance(new_string, str):
                raise ValueError("workspace_nonempty_old_string_required")
            if max(len(old_string), len(new_string)) > limit:
                raise ValueError("workspace_text_or_byte_limit")
            # Bound expansion BEFORE StoreBackend allocates the replaced text.
            # A 64-KiB file with a one-character needle must not allocate GiB.
            current = backend.download_files([path])[0]
            if current.content is not None:
                occurrences = current.content.decode("utf-8").count(old_string)
                growth = len(new_string.encode("utf-8")) - len(old_string.encode("utf-8"))
                if len(current.content) + occurrences * growth > limit:
                    raise ValueError("workspace_text_or_byte_limit")
            result = backend.edit(path, old_string, new_string, replace_all=replace_all)
        if result.error:
            raise ValueError(result.error)
        data = backend.download_files([path])[0].content
        if data is None:
            raise ValueError("workspace_backend_read_failed")
        text = data.decode("utf-8")
        if "\x00" in text or len(data) > limit:
            raise ValueError("workspace_text_or_byte_limit")
        if expected_version >= MAX_VERSIONS:
            raise ValueError("workspace_version_count_limit")
        # Existing library owns byte persistence and quota enforcement. No
        # request deduplication: each immutable version has a distinct file ID.
        _no_links(self.files.FILES_ROOT / f"user_{self.owner}" / self.session)
        raise_if_cancelled("workspace_before_staging")
        staged = self.files.register_file(session_id=self.session, owner_user_id=self.owner,
            filename=Path(path).name, content=data, visibility="internal", producer_tool="workspace_write")
        try:
            with self.files._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._require_writable_scope(conn)
                state = self._state(conn, path)
                self._cas(state, expected_version, expected_sha256)
                count = conn.execute("SELECT COUNT(DISTINCT path) FROM workspace_file_versions "
                                     "WHERE owner=? AND session=?", (self.owner, self.session)).fetchone()[0]
                if (state is None or state["kind"] == "workspace_tombstone") and count >= MAX_FILES:
                    raise ValueError("workspace_file_count_limit")
                sizes = conn.execute("SELECT COALESCE(SUM(f.size_bytes),0) FROM workspace_file_versions v "
                    "JOIN session_files f ON f.file_id=v.file_id WHERE v.owner=? AND v.session=? "
                    "AND v.path<>? AND v.version=(SELECT MAX(x.version) FROM workspace_file_versions x "
                    "WHERE x.owner=v.owner AND x.session=v.session AND x.path=v.path)",
                    (self.owner, self.session, path)).fetchone()[0]
                if sizes + len(data) > MAX_VIEW_BYTES:
                    raise ValueError("workspace_view_byte_limit")
                staged_row = conn.execute("SELECT * FROM session_files WHERE file_id=?", (staged["file_id"],)).fetchone()
                if staged_row is None:
                    raise ValueError("workspace_staging_unavailable")
                self._read_bytes(staged_row, path)
                version = expected_version + 1
                # Pin and version are committed under the same library lock.
                self.files._replace_resource_references_locked(conn, self.owner, "workspace_version",
                    staged["file_id"], [staged["file_id"]])
                conn.execute("INSERT INTO workspace_file_versions VALUES(?,?,?,?,?,?)",
                             (self.owner, self.session, path, version, staged["file_id"], staged["sha256"]))
                raise_if_cancelled("workspace_before_commit")
            return {"ref": {"kind": "workspace_file", "path": path, "version": version,
                            "file_id": staged["file_id"], "sha256": staged["sha256"]},
                    "previous_ref": refs.get(path), "size_bytes": len(data),
                    "occurrences": getattr(result, "occurrences", None)}
        except BaseException:
            # Clean only our uncommitted staging object, using the library's
            # shared delete lock/pin guard. Never migrate unrelated stores here.
            with self.files._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM session_files WHERE file_id=? AND owner_user_id=? AND session_id=?",
                    (staged["file_id"], self.owner, self.session)).fetchone()
                if row is not None:
                    self.files._delete_indexed_file(conn, row)
            raise

    def text(self, path, *, version=None):
        """Return the exact committed UTF-8 text and its ref for a file tool read."""
        backend, refs = self.view(path=path, version=version)
        if path not in refs:
            raise LookupError("workspace_file_unavailable")
        result = backend.download_files([path])[0]
        if result.error or result.content is None:
            raise ValueError("workspace_backend_read_failed")
        return result.content.decode("utf-8"), refs[path]

    def remember(self, refs, *, origin="read"):
        """Persist this run's observed versions for later no-argument CAS checks."""
        items = [(path, int(ref["version"]), str(ref["sha256"]))
                 for path, ref in refs.items()
                 if isinstance(ref, dict) and type(ref.get("version")) is int and ref["version"] > 0
                 and isinstance(ref.get("sha256"), str) and ref["sha256"]]
        if not items:
            return
        now = time.time()
        with self.files._connect() as conn:
            _ensure_tables(conn)
            conn.executemany(
                "INSERT INTO workspace_file_observations(owner,session,run_id,path,version,sha256,origin,observed_at)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner,session,run_id,path) DO UPDATE SET"
                " version=excluded.version, sha256=excluded.sha256, origin=excluded.origin,"
                " observed_at=excluded.observed_at",
                [(self.owner, self.session, self.run_id, path, version, sha256, origin, now)
                 for path, version, sha256 in items])

    def observed(self, path, *, read_only_ok=True):
        """Return the version/hash this run observed, or None when it has not."""
        with self.files._connect() as conn:
            _ensure_tables(conn)
            row = conn.execute("SELECT version, sha256, origin FROM workspace_file_observations "
                               "WHERE owner=? AND session=? AND run_id=? AND path=?",
                               (self.owner, self.session, self.run_id, path)).fetchone()
        if row is None:
            return None
        if not read_only_ok and row["origin"] != "read":
            return None
        return {"version": row["version"], "sha256": row["sha256"], "origin": row["origin"]}

    def tombstone(self, path, *, expected_version, expected_sha256):
        """Logically delete one private path as a new version; bytes are retained."""
        from src.core.cancel import raise_if_cancelled

        virtual_path(path, writable=True)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("workspace_expected_version_and_hash_required")
        with self.files._connect() as conn:
            _ensure_tables(conn)
            self._require_writable_scope(conn)
            state = self._state(conn, path)
            self._cas(state, expected_version, expected_sha256)
            if state is None or state["kind"] != "workspace_file":
                raise LookupError("workspace_delete_target_missing")
            if expected_version >= MAX_VERSIONS:
                raise ValueError("workspace_version_count_limit")
            raise_if_cancelled("workspace_before_delete")
            conn.execute("BEGIN IMMEDIATE")
            self._require_writable_scope(conn)
            self._cas(self._state(conn, path), expected_version, expected_sha256)
            version = expected_version + 1
            conn.execute("INSERT OR REPLACE INTO workspace_file_tombstones VALUES(?,?,?,?,?)",
                         (self.owner, self.session, path, version, time.time()))
        return {"path": path, "version": version, "tombstoned": True}
