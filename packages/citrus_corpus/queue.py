"""Dedicated job lifecycle and fenced leases for single-work intake."""
from hashlib import sha256
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from citrus_refcheck.providers import normalize_doi

from .prepare import public_text


MAX_PENDING = 100
MAX_JOBS_PER_DAY = 200
JOB_TTL = 7 * 86400
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
DOI = re.compile(r"10\.\d{4,9}/[^\s<>\x00-\x20]{1,500}\Z")
_TRANSIENT = {"network_timeout", "network_unavailable", "worker_interrupted", "storage_unavailable"}


def target_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("source_url_invalid")
    if any(ord(char) <= 32 or ord(char) >= 127 or char == "\\" for char in value):
        raise ValueError("source_url_invalid")
    parsed = urlsplit(value)
    host = parsed.hostname
    if (parsed.scheme not in {"http", "https"} or not host or not re.fullmatch(r"[a-z0-9.-]+", host)
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443 if parsed.scheme == "https" else 80)):
        raise ValueError("source_url_invalid")
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


def work_identity(*, doi="", url="", local_key=""):
    if doi:
        doi = normalize_doi(doi)
        if not DOI.fullmatch(doi):
            raise ValueError("doi_invalid")
        return "doi:" + doi
    if url:
        source_url = target_url(url)
        parsed = urlsplit(source_url)
        if parsed.hostname in {"doi.org", "dx.doi.org"} and DOI.fullmatch(normalize_doi(parsed.path[1:])):
            return "doi:" + normalize_doi(parsed.path[1:])
        return "url:" + source_url
    local_key = str(local_key or "").strip()
    if not local_key or len(local_key) > 512 or "\x00" in local_key:
        raise ValueError("work_identity_required")
    return "local:" + local_key


class IntakeQueue:
    def __init__(self, store, *, now=time.time):
        self.store = store
        self.now = now

    def submit(self, identity, request, *, actor, expected_sha256=None, revision="", persist=None, timeout=0.2):
        if expected_sha256 is not None and not SHA256.fullmatch(expected_sha256):
            raise ValueError("source_sha256_invalid")
        if expected_sha256 is None and request["kind"] != "explicit":
            raise ValueError("source_sha256_required")
        if not isinstance(revision, str) or len(revision) > 128:
            raise ValueError("revision_invalid")
        actor = public_text(actor, 128)
        if not actor:
            raise ValueError("actor_required")
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode()) > 8192:
            raise ValueError("intake_request_too_large")
        fingerprint = sha256(json.dumps([identity, expected_sha256 or revision],
                                       ensure_ascii=False).encode()).hexdigest()
        now = self.now()
        with self.store.write(timeout=timeout) as conn:
            existing = conn.execute("SELECT * FROM intake WHERE fingerprint=?", (fingerprint,)).fetchone()
            if existing and existing["state"] not in {"failed", "expired"}:
                return self._view(existing)
            pending = conn.execute("SELECT count(*) FROM intake WHERE state IN ('queued','leased')").fetchone()[0]
            daily = conn.execute("SELECT count(*) FROM intake WHERE created>?", (now - 86400,)).fetchone()[0]
            if pending >= MAX_PENDING or daily >= MAX_JOBS_PER_DAY:
                raise ValueError("corpus_queue_full")
            if persist is not None:
                persist()
            if existing:
                conn.execute("""UPDATE intake SET actor=?, request=?, state='queued', created=?, updated=?,
                             not_before=?, attempts=0, lease=NULL, lease_until=NULL, error=NULL WHERE id=?""",
                             (actor, payload, now, now, now, existing["id"]))
                job_id = existing["id"]
            else:
                job_id = uuid4().hex
                conn.execute("""INSERT INTO intake
                             (id,fingerprint,identity,request,actor,state,expected_sha256,created,updated,not_before)
                             VALUES(?,?,?,?,?,'queued',?,?,?,?)""",
                             (job_id, fingerprint, identity, payload, actor, expected_sha256, now, now, now))
            return self._view(conn.execute("SELECT * FROM intake WHERE id=?", (job_id,)).fetchone())

    def claim(self, *, network=False):
        now = self.now()
        with self.store.write() as conn:
            conn.execute("""UPDATE intake SET state='expired', lease=NULL, lease_until=NULL, updated=?, error='intake_expired'
                         WHERE created<=? AND (state='queued' OR (state='leased' AND lease_until<=?))""",
                         (now, now - JOB_TTL, now))
            conn.execute("""UPDATE intake SET state='failed', lease=NULL, lease_until=NULL, updated=?, error='lease_exhausted'
                         WHERE state='leased' AND lease_until<=? AND attempts>=?""", (now, now, MAX_ATTEMPTS))
            row = conn.execute("""SELECT * FROM intake WHERE ((state='queued' AND not_before<=?)
                         OR (state='leased' AND lease_until<=?))
                         AND (? OR json_extract(request, '$.kind') <> 'explicit')
                         ORDER BY created,id LIMIT 1""", (now, now, int(network))).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            conn.execute("""UPDATE intake SET state='leased', attempts=attempts+1, lease=?, lease_until=?,
                         updated=? WHERE id=?""", (token, now + LEASE_SECONDS, now, row["id"]))
            return {**dict(row), "request": json.loads(row["request"]), "lease": token,
                    "attempts": row["attempts"] + 1}

    def remember_original(self, job, digest):
        with self.store.write(timeout=2) as conn:
            self._fence(conn, job)
            conn.execute("UPDATE intake SET raw_sha256=? WHERE id=?", (digest, job["id"]))

    def _fence(self, conn, job):
        current = conn.execute("SELECT state,lease,lease_until FROM intake WHERE id=?", (job["id"],)).fetchone()
        if (not current or current["state"] != "leased" or current["lease"] != job["lease"]
                or current["lease_until"] <= self.now()):
            raise ValueError("corpus_lease_lost")

    def complete(self, conn, job, *, document, version, digest):
        self._fence(conn, job)
        conn.execute("""UPDATE intake SET state='prepared', lease=NULL, lease_until=NULL, updated=?,
                     document_id=?,version_id=?,raw_sha256=?,error=NULL WHERE id=?""",
                     (self.now(), document, version, digest, job["id"]))

    def fail(self, job, code):
        now = self.now()
        retry = code in _TRANSIENT and job["attempts"] < MAX_ATTEMPTS
        with self.store.write(timeout=2) as conn:
            self._fence(conn, job)
            conn.execute("""UPDATE intake SET state=?, lease=NULL, lease_until=NULL, updated=?,
                         not_before=?,error=? WHERE id=?""",
                         ("queued" if retry else "failed", now, now + 15 * job["attempts"], code, job["id"]))

    def status(self, job_id):
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM intake WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("corpus_intake_unknown")
        return self._view(row)

    @staticmethod
    def _view(row):
        fields = ("id", "state", "attempts", "raw_sha256", "document_id", "version_id", "error")
        return {field: row[field] for field in fields}
