"""The only job handlers for independent, single-work intake."""
from hashlib import sha256

from . import originals
from .auto_intake import AutoIntake
from .catalog import Catalog
from .extract import extract_markdown, source_format
from .prepare import MAX_SOURCE_BYTES, PreparationError, public_text
from .queue import IntakeQueue, target_url, work_identity


class Intake:
    def __init__(self, store, *, queue=None):
        self.store = store
        self.queue = queue or IntakeQueue(store)
        self.catalog = Catalog(store)

    def submit_local(self, path, *, expected_sha256, actor, doi="", url="", title="",
                     local_key="", timeout=0.2):
        path = str(originals.inbox_relative(path))
        identity = work_identity(doi=doi, url=url, local_key=local_key or path)
        return self.queue.submit(identity, {"kind": "saved", "path": path, "format": source_format(path),
                                           "title": public_text(title)}, actor=actor,
                                 expected_sha256=expected_sha256, timeout=timeout)

    def submit_explicit(self, *, actor, doi="", url="", title="", expected_sha256=None, revision=""):
        if bool(doi) == bool(url):
            raise ValueError("single_explicit_source_required")
        identity = work_identity(doi=doi, url=url)
        request = {"kind": "explicit", "doi": identity[4:] if doi else "",
                   "url": target_url(url) if url else "", "title": public_text(title)}
        return self.queue.submit(identity, request, actor=actor, expected_sha256=expected_sha256,
                                 revision=revision)

    def submit_observed(self, content, *, fmt, actor, doi="", url="", title=""):
        """Persist already-returned Fetch content outside the request thread."""
        identity = work_identity(doi=doi, url=url)
        if not isinstance(content, str) or len(content) > MAX_SOURCE_BYTES:
            raise ValueError("source_size_invalid")
        raw = content.encode("utf-8")
        if not raw or len(raw) > MAX_SOURCE_BYTES:
            raise ValueError("source_size_invalid")
        digest = sha256(raw).hexdigest()
        return self.queue.submit(identity, {"kind": "observed", "format": fmt, "title": public_text(title)},
                                 actor=actor, expected_sha256=digest,
                                 persist=lambda: originals.archive(self.store.originals, raw))

    def process_one(self, *, acquire=None, automatic=None):
        with self.store.writer():
            job = self.queue.claim(network=acquire is not None, automatic=automatic is not None)
            if job is None:
                return None
            try:
                if job["request"]["kind"] == "automatic":
                    self._prepare_automatic(job, automatic)
                else:
                    self._prepare(job, acquire)
            except PreparationError as exc:
                self.queue.fail(job, exc.code)
            except (FileNotFoundError, NotADirectoryError):
                self.queue.fail(job, "source_missing")
            except OSError:
                self.queue.fail(job, "storage_unavailable")
            except (ValueError, KeyError) as exc:
                if str(exc) != "corpus_lease_lost":
                    self.queue.fail(job, "source_invalid")
            return self.queue.status(job["id"])

    def _prepare_automatic(self, job, automatic):
        adapter = AutoIntake(self.store)
        allowed = lambda: automatic.allowed(job)
        if not allowed():
            raise PreparationError("network_scope_denied")
        expected = job["expected_sha256"]
        raw = originals.read_archive(self.store.originals, expected) if expected else None
        record = automatic.resolve(job, raw=raw, reuse=adapter.proven_original, allowed=allowed)
        resolved = adapter.resolve_job(self.queue, job, record, allowed=allowed)
        if resolved is not None:
            self._prepare(resolved, None, allowed=allowed)

    def _prepare(self, job, acquire, *, allowed=lambda: True):
        request = job["request"]
        expected = job["expected_sha256"]
        if expected and self.catalog.complete_duplicate(self.queue, job, expected, allowed=allowed):
            return
        if request["kind"] == "explicit":
            raw, fmt = originals.capture(self.store.originals, job["id"], lambda: acquire(request))
        elif request["kind"] in {"observed", "automatic"}:
            raw, fmt = originals.read_archive(self.store.originals, expected), request["format"]
        else:
            archived = self.store.originals / expected
            raw = (originals.read_archive(self.store.originals, expected) if archived.exists()
                   else originals.read_inbox(self.store.inbox, request["path"]))
            fmt = request["format"]
        digest = sha256(raw).hexdigest()
        if expected and digest != expected:
            raise PreparationError("original_hash_mismatch")
        if request["kind"] != "automatic":
            originals.archive(self.store.originals, raw)
            self.queue.remember_original(job, digest)
            if not expected and self.catalog.complete_duplicate(self.queue, job, digest, allowed=allowed):
                return
        markdown = extract_markdown(raw, fmt)
        self.catalog.record(self.queue, job, digest, fmt, markdown, allowed=allowed)
