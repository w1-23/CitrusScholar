"""Serve granted old citations from a read-only legacy store."""
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from src.shared_corpus.journal import read_pointer, valid_generation
from src.shared_corpus.prepare import read_document_files


class LegacyReader:
    def __init__(self, root):
        self.root = Path(root)
        self.db = self.root / "catalog.sqlite3"

    def _members(self, generation, document, version):
        if not valid_generation(generation):
            return []
        try:
            with closing(sqlite3.connect(self.db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as conn:
                conn.execute("BEGIN")
                record = conn.execute("SELECT documents FROM shared_builds WHERE state='active' AND generation=?",
                                      (generation,)).fetchone()
                if not record:
                    return []
                members = [candidate for key, candidate in json.loads(record[0])
                           if key == document and (version is None or candidate == version)]
                return [candidate for candidate in members if conn.execute(
                    "SELECT 1 FROM shared_rights WHERE document_id=? AND version_id=? AND status='granted'",
                    (document, candidate)).fetchone()]
        except (sqlite3.Error, ValueError):
            return []

    def preview(self, document, *, generation=None, version=None, start=0, length=12000,
                digest=None, full=False):
        generation = generation or read_pointer(self.root)
        members = self._members(generation, document, version)
        if len(members) != 1:
            raise LookupError("shared_document_unavailable")
        version = members[0]
        metadata, text = read_document_files(self.root / "documents", document, version)
        actual = sha256(text.encode()).hexdigest()
        if ((digest is not None and digest != actual) or
                not 0 <= start < len(text) or not 1 <= length <= 12000 or
                not self._members(generation, document, version)):
            raise LookupError("shared_document_unavailable")
        end = min(start + length, len(text))
        if full and end - start != length:
            raise LookupError("shared_document_unavailable")
        return {"document_id": document, "version_id": version, "index_generation": generation,
                "title": metadata.get("title", ""), "doi": metadata.get("doi", ""),
                "section_path": metadata.get("section_path", []), "kind": "stored_markdown",
                "md_sha256": actual, "start": start, "end": end, "total_chars": len(text),
                "next_start": end if end < len(text) else None, "markdown": text if full else text[start:end]}


def archived_reader():
    from src.config import settings
    from src.shared_corpus.runtime import CORPUS_ID
    return LegacyReader(Path(settings.DATA_DIR) / CORPUS_ID)
