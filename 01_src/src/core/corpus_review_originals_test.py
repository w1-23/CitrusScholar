from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from src.core import corpus_review as review


class ReviewOriginalTests(TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.db = self.root / "state" / "corpus_review.sqlite3"
        self.data = self.root / "data"
        self.archive = self.data / "_review_queue" / "archive"
        patcher = mock.patch.multiple(review, _db_path=lambda: self.db,
                                     _data_dir=lambda: self.data, _schema_ready=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        review.ensure_schema()
        review._schema_ready = False

    def add_candidate(self, path="", *, source=b"original source", digest=True,
                      raw_bytes=None, archive_state="archived", status="pending"):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(
                "INSERT INTO corpus_candidates (created_at, updated_at, status, stored_path, "
                "raw_sha256, raw_bytes, archive_state) VALUES ('', '', ?, ?, ?, ?, ?)",
                (status, str(path), hashlib.sha256(source).hexdigest() if digest else "",
                 len(source) if raw_bytes is None else raw_bytes, archive_state),
            ).lastrowid

    def add_source(self, candidate="fetch_" + "a" * 64, filename="paper.md", data=b"original source"):
        path = self.archive / candidate / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def legacy(self, path):
        return self.root / "old-host" / path.relative_to(self.data)

    def test_rebases_exact_path_without_catalog_or_archive_writes(self):
        data = "# 原始标题\r\nraw <unsafe>\n".encode()
        target = self.add_source(data=data)
        stale = self.legacy(target)
        archived = self.add_candidate(stale, source=data)
        metadata_only = self.add_candidate(archive_state="fulltext_missing")
        wrong_size = self.add_candidate(stale, source=data, raw_bytes=1)
        no_digest = self.add_candidate(stale, source=data, digest=False)
        before = self.db.read_bytes(), {p for p in self.root.rglob("*")}
        with mock.patch.object(review, "ensure_schema", side_effect=AssertionError("called schema writer")):
            listing = review.list_candidates(status="all", read_only=True)
            by_id = {item["id"]: item for item in listing["candidates"]}
            self.assertEqual(by_id[archived]["raw_status"], "available")
            self.assertEqual(by_id[metadata_only]["raw_status"], "fulltext_missing")
            self.assertTrue(all("stored_path" not in item for item in by_id.values()))
            for candidate_id in (wrong_size, no_digest):
                self.assertEqual(by_id[candidate_id]["raw_status"], "unavailable")
            detail = review.get_candidate(archived, read_only=True)
            self.assertEqual(detail["preview"], data.decode())
            self.assertNotIn("stored_path", detail)
            self.assertEqual(review.raw_file(archived), {"data": data, "mime": "text/markdown", "filename": "paper.md"})
            for candidate_id in (metadata_only, wrong_size, no_digest):
                with self.assertRaises(FileNotFoundError):
                    review.raw_file(candidate_id)
                self.assertEqual(review.get_candidate(candidate_id, read_only=True)["preview"], "")
            with self.assertRaises(KeyError):
                review.raw_file(999)
        self.assertFalse(stale.exists())
        self.assertEqual(self.db.read_bytes(), before[0])
        self.assertEqual({p for p in self.root.rglob("*")}, before[1])
        self.assertEqual(target.read_bytes(), data)

    def test_does_not_follow_paths_to_matching_bytes(self):
        target = self.add_source()
        candidate = self.legacy(target)
        outside = self.root / "elsewhere"
        outside.mkdir()
        external_source = outside / "paper.md"
        external_source.write_bytes(target.read_bytes())
        for recorded in (str(external_source), str(candidate).replace("/archive/", "/archive/../archive/"),
                         str(candidate).replace("/archive/", "/archive/./"), str(candidate)[1:],
                         str(candidate).replace("/archive/", "/archive//"), str(candidate).replace("paper.md", "meta.json")):
            with self.subTest(recorded=recorded):
                with self.assertRaises(FileNotFoundError):
                    review.raw_file(self.add_candidate(recorded))
        candidate_id = self.add_candidate(candidate)
        target.unlink()
        target.symlink_to(external_source)
        with self.assertRaises(FileNotFoundError):
            review.raw_file(candidate_id)
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(FileNotFoundError):
            review.raw_file(candidate_id)
        target.unlink()
        target.parent.rmdir()
        target.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            review.raw_file(candidate_id)
        target.parent.unlink()
        real_archive = self.archive.rename(self.root / "another-archive")
        self.archive.symlink_to(real_archive, target_is_directory=True)
        (real_archive / target.parent.name).mkdir()
        (real_archive / target.parent.name / target.name).write_bytes(external_source.read_bytes())
        with self.assertRaises(FileNotFoundError):
            review.raw_file(candidate_id)

    def test_existing_legacy_originals_and_decisions_still_work(self):
        markdown = self.add_source(candidate="old-title", filename="paper.txt")
        direct = self.add_candidate(markdown, digest=False)
        self.assertEqual(review.raw_file(direct)["data"], b"original source")
        self.assertEqual(review.get_candidate(direct)["stored_path"], str(markdown))
        pdf_data = b"%PDF-1.7 fixture"
        pdf = self.add_source(candidate="old-upload_abc0123456", filename="original.pdf", data=pdf_data)
        uploaded = self.add_candidate(self.legacy(pdf), source=pdf_data)
        self.assertEqual(review.raw_file(uploaded), {"data": pdf_data, "mime": "application/pdf", "filename": "original.pdf"})
        self.assertEqual(review.get_candidate(uploaded, read_only=True)["preview"], "")
        self.assertTrue(review.decide(uploaded, "approve", admin_user_id=1)["changed"])
        self.assertEqual(review.list_candidates(status="approved")["candidates"][0]["id"], uploaded)

    def test_digest_and_size_checks_fail_closed_for_existing_files(self):
        target = self.add_source()
        candidate_id = self.add_candidate(target)
        target.write_bytes(b"faulty content!")
        self.assertEqual(review.get_candidate(candidate_id)["raw_status"], "unavailable")
        with self.assertRaises(FileNotFoundError):
            review.raw_file(candidate_id)
        target.write_bytes(b"original source")
        with mock.patch.object(review, "MAX_ARCHIVE_BYTES", len(b"original source") - 1):
            with self.assertRaises(FileNotFoundError):
                review.raw_file(candidate_id)

    def test_missing_catalog_does_not_create_files(self):
        self.db.unlink()
        self.db.parent.rmdir()
        self.assertEqual(review.list_candidates(), {"candidates": [], "has_more": False, "next_before_id": None, "counts": {}})
        self.assertIsNone(review.get_candidate(1))
        with self.assertRaises(KeyError):
            review.raw_file(1)
        self.assertEqual(list(self.root.iterdir()), [])
