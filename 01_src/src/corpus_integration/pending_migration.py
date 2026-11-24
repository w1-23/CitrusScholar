"""Stage exact legacy prepared works with their verified originals, without grants."""
from contextlib import closing, contextmanager, nullcontext
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from citrus_corpus import chunks, originals, prepare
from citrus_corpus.contract import ISOLATED
from citrus_corpus.queue import SHA256, work_identity
from citrus_corpus.store import Store
from src.shared_corpus.prepare import read_document_files

_TABLES = ("shared_activity", "shared_audit", "shared_build_events", "shared_builds",
           "shared_documents", "shared_preparations", "shared_proposals", "shared_rights")
_FORMATS = {"markdown": "markdown", "plain_text": "text"}
_LIMIT = 100


def _serial(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _snapshot(root, destination):
    database = root / "catalog.sqlite3"
    if database.is_symlink():
        raise ValueError("legacy_database_symlink")
    wal, shm = (root / (database.name + suffix) for suffix in ("-wal", "-shm"))

    def stat_files():
        stamps = []
        for path in (database, wal, shm):
            try:
                info = path.stat()
                stamps.append((info.st_ino, info.st_size, info.st_mtime_ns))
            except FileNotFoundError:
                stamps.append(None)
        return stamps

    before = stat_files()
    # On a checkpointed WAL database, immutable mode avoids creating sidecars on
    # production. A live WAL must be read via SQLite with both sidecars present.
    immutable = before[1:] == [None, None]
    if not immutable and any(value is None for value in before[1:]):
        raise ValueError("legacy_wal_sidecar_missing")
    uri = database.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as source:
        source.backup(destination, pages=128, sleep=0.01)
    if stat_files() != before:
        raise ValueError("legacy_database_changed_snapshot")


@contextmanager
def _legacy_writer_fence(root):
    # Share the legacy Web's exact lock inode without opening it for writing.
    descriptor = os.open(root / ".web.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("legacy_web_writer_still_active") from exc
        yield
    finally:
        os.close(descriptor)


def _past_publication(root):
    return any(os.path.lexists(root / name) for name in ("active_index.json", "previous_index.json", "generations"))


def _originals(archive, required):
    """Resolve by the historical proposal digest, not the mutable candidate row."""
    found = {}
    for folder in sorted(archive.iterdir()):
        if folder.is_symlink() or not folder.is_dir():
            continue
        relative = folder.name + "/meta.json"
        try:
            sidecar = originals.read_inbox(archive, relative)
            if len(sidecar) > prepare.MAX_ARCHIVE_METADATA_BYTES:
                continue
            record = json.loads(sidecar)
        except (OSError, ValueError, prepare.PreparationError):
            continue
        digest = record.get("sha256_of_saved_bytes") if isinstance(record, dict) else None
        if not isinstance(digest, str) or digest not in required:
            continue
        path = folder.name + "/paper.md"
        raw = originals.read_inbox(archive, path)
        if sha256(raw).hexdigest() != digest:
            raise ValueError("legacy_original_hash_mismatch")
        if digest in found:
            raise ValueError("legacy_original_ambiguous")
        found[digest] = (path, record, raw)
    if set(found) != required:
        raise ValueError("legacy_original_missing")
    return found


def _prepare_record(root, row, original):
    path, source, raw = original
    legacy_doc, legacy_version = row["document_id"], row["version_id"]
    metadata, markdown = read_document_files(root / "documents", legacy_doc, legacy_version)
    digest = sha256(markdown.encode()).hexdigest()
    source_text_sha = metadata.get("source_sha256", "")
    if (digest != row["content_sha256"] or digest != metadata.get("content_sha256")
            or not SHA256.fullmatch(source_text_sha) or source_text_sha != source.get("sha256_of_source_text")):
        raise ValueError("legacy_prepared_hash_mismatch")
    # The legacy archive stored upstream-converted UTF-8. Legacy preparation
    # validated those bytes and changed only line endings and the final newline.
    raw_text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not raw_text.endswith("\n"):
        raw_text += "\n"
    if raw_text != markdown:
        raise ValueError("legacy_original_prepared_mismatch")
    fmt = _FORMATS.get(source.get("raw_format"))
    if fmt is None:
        raise ValueError("legacy_source_format_unsupported")
    doi = row["doi"] or ""
    identity = work_identity(doi=doi, url=metadata.get("source_url") or "",
                             local_key="legacy-" + legacy_doc)
    raw_hash = row["candidate_raw_sha256"]
    doc, version = "doc_" + sha256(identity.encode()).hexdigest()[:32], "v_" + raw_hash[:40]
    outline = chunks.section_outline(markdown)
    migrated = {"schema_version": chunks.SCHEMA_VERSION, "document_id": doc, "version_id": version,
                "title": prepare.public_text(metadata.get("title"), 512),
                "doi": identity[4:] if identity.startswith("doi:") else "",
                "year": prepare.public_text(metadata.get("year"), 16), "source_kind": "legacy_import",
                "source_format": fmt, "source_parser": "legacy-prepared-v1", "source_sha256": raw_hash,
                "content_sha256": digest, "char_count": len(markdown),
                "prepared_at": metadata["prepared_at"],
                "section_path": [section["section_name"] for section in outline if section["section_level"]]}
    migrated["chunk_count"] = len(chunks.chunk_document(migrated, markdown, spec=ISOLATED))
    provenance = {"legacy_prepared_metadata": metadata, "legacy_archive_sidecar": source}
    link = {"candidate_id": row["candidate_id"], "proposal_id": row["proposal_id"],
            "legacy_document_id": legacy_doc, "legacy_version_id": legacy_version,
            "document_id": doc, "version_id": version, "original_relpath": path,
            "raw_sha256": raw_hash, "source_text_sha256": source_text_sha, "markdown_sha256": digest,
            "provenance_sha256": sha256(_serial(provenance)).hexdigest(), "rights": "needs_review"}
    return link, identity, migrated, markdown, raw, row["prepared_at"], provenance


def _plan(root, archive, database):
    if _past_publication(root):
        raise ValueError("legacy_publication_requires_full_migration")
    _snapshot(root, database)
    database.row_factory = sqlite3.Row
    tables = {name: [tuple(row) for row in database.execute(f"SELECT * FROM {name} ORDER BY rowid")]
              for name in _TABLES}
    counts = {name: len(rows) for name, rows in tables.items()}
    if any(counts[name] for name in ("shared_rights", "shared_builds", "shared_build_events")):
        raise ValueError("legacy_rights_or_build_history_requires_full_migration")
    rows = list(database.execute("""SELECT d.*, p.candidate_raw_sha256 FROM shared_documents d
        JOIN shared_proposals p ON p.proposal_id=d.proposal_id AND p.candidate_id=d.candidate_id
            AND p.document_id=d.document_id AND p.version_id=d.version_id
        JOIN shared_preparations r ON r.candidate_id=p.candidate_id AND r.owner=p.owner_user_id
        WHERE p.state='needs_review' AND p.rights='needs_review' AND r.state='prepared'
            AND coalesce(d.superseded_by,'')='' AND d.source_kind='fetch_paper'
        ORDER BY d.candidate_id LIMIT ?""", (_LIMIT + 1,)))
    if (not 0 < len(rows) <= _LIMIT or any(counts[name] != len(rows) for name in
            ("shared_documents", "shared_proposals", "shared_preparations"))):
        raise ValueError("legacy_non_pending_or_incomplete_catalog")
    required = {row["candidate_raw_sha256"] for row in rows}
    if len(required) != len(rows) or not all(SHA256.fullmatch(digest or "") for digest in required):
        raise ValueError("legacy_original_digest_invalid")
    found = _originals(archive, required)
    records = [_prepare_record(root, row, found[row["candidate_raw_sha256"]]) for row in rows]
    if len({record[1] for record in records}) != len(records):
        raise ValueError("legacy_multiple_versions_require_full_migration")
    if _past_publication(root):
        raise ValueError("legacy_publication_requires_full_migration")
    result = {"legacy_counts": counts, "versions": [record[0] for record in records],
              "catalog_sha256": sha256(_serial(tables)).hexdigest()}
    result["plan_sha256"] = sha256(_serial(result)).hexdigest()
    return result, records


def migrate_pending(legacy_root, archive_root, *, target=None, expected_plan=None):
    root, archive = (Path(p).resolve(strict=True) for p in (legacy_root, archive_root))
    if (target is None) != (expected_plan is None):
        raise ValueError("legacy_apply_requires_target_and_expected_plan")
    fence = _legacy_writer_fence(root) if target is not None else nullcontext()
    with fence, closing(sqlite3.connect(":memory:")) as database:
        plan, records = _plan(root, archive, database)
        if target is None:
            return {"state": "verified_pending_plan", **plan}
        if expected_plan != plan["plan_sha256"]:
            raise ValueError("legacy_plan_changed")
        target = Path(target)
        if (not target.is_absolute() or target.resolve() != target or os.path.lexists(target)
                or any(target.is_relative_to(source) for source in (root, archive))):
            raise ValueError("legacy_target_must_be_fresh_and_separate")
        with TemporaryDirectory(prefix=".pending-migration-", dir=target.parent) as temp:
            store = Store.initialize(Path(temp) / "corpus")
            provenance_root = store.root / "legacy-provenance"
            provenance_root.mkdir(mode=0o700)
            with store.writer(), store.write(timeout=2) as conn:
                for link, identity, metadata, markdown, raw, created, provenance in records:
                    prepare._write_file(provenance_root / (link["raw_sha256"] + ".json"),
                                        _serial(provenance) + b"\n")
                    originals.archive(store.originals, raw)
                    prepare.write_document(store.documents, link["document_id"], link["version_id"], metadata, markdown)
                    conn.execute("""INSERT INTO versions (identity,document_id,version_id,raw_sha256,markdown_sha256,
                        chunk_count,source_format,rights,created) VALUES(?,?,?,?,?,?,?,'needs_review',?)""",
                        (identity, link["document_id"], link["version_id"], link["raw_sha256"], link["markdown_sha256"],
                         metadata["chunk_count"], metadata["source_format"], created))
            migrated_db = store.root / "legacy-catalog.sqlite3"
            migrated_db.touch(mode=0o600, exist_ok=False)
            with closing(sqlite3.connect(migrated_db)) as copied:
                database.backup(copied)
            prepare._write_file(store.root / "legacy-migration.json", _serial(plan) + b"\n")
            prepare._sync_dir(provenance_root)
            prepare._sync_dir(store.root)
            if os.path.lexists(target):
                raise ValueError("legacy_target_must_be_fresh_and_separate")
            os.rename(store.root, target)
            prepare._sync_dir(target.parent)
        return {"state": "imported_pending", **plan}
