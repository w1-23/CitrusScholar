"""Dry run of a legacy shared-corpus snapshot and base fingerprints."""
from argparse import ArgumentParser
from contextlib import closing
from hashlib import file_digest
import json
from pathlib import Path
import sqlite3

from citrus_corpus.prepare import read_document_files
from citrus_corpus.pointers import read_pointer

_TABLES = ("shared_proposals", "shared_documents", "shared_rights", "shared_builds")


def _fingerprint(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def dry_run(root, base_inputs, *, limit=10):
    root = Path(root).resolve(strict=True)
    paths = [Path(path).resolve(strict=True) for path in base_inputs]
    if len(set(paths)) != 5 or not 1 <= limit <= 100:
        raise ValueError("migration_requires_five_distinct_base_files_and_bounded_sample")
    base_before = [_fingerprint(path) for path in paths]
    pointer_before = read_pointer(root)
    database = root / "catalog.sqlite3"
    with (closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as source,
          closing(sqlite3.connect(":memory:")) as snapshot):
        source.backup(snapshot, pages=128, sleep=0.01)
        counts = {table: snapshot.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in _TABLES}
        selected = snapshot.execute("""SELECT document_id,version_id,content_sha256 FROM shared_documents
                                     ORDER BY document_id,version_id LIMIT ?""", (limit,)).fetchall()
        sample = []
        for document, version, expected in selected:
            metadata, _ = read_document_files(root / "documents", document, version)
            if metadata["content_sha256"] != expected:
                raise ValueError("migration_sample_hash_mismatch")
            sample.append({"document_id": document, "version_id": version, "markdown_sha256": expected})
    pointer_after = read_pointer(root)
    base_after = [_fingerprint(path) for path in paths]
    if pointer_before != pointer_after or base_before != base_after:
        raise ValueError("migration_source_changed")
    return {"state": "dry_run", "copied_catalog_counts": counts, "sample": sample,
            "legacy_active_generation": pointer_before, "base_sha256": base_before}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--base-file", action="append", required=True)
    parser.add_argument("--sample", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(dry_run(args.legacy_root, args.base_file, limit=args.sample), indent=2))


if __name__ == "__main__":
    main()
