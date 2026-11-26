"""Prepare bounded, trusted registry observations for the external matcher.

This module contains transport and receipt shaping only. It neither adjudicates
matches nor accepts addresses supplied by a user or model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from hashlib import sha256
import re
import time
from urllib.parse import quote, urlencode

from citrus_refcheck.providers import normalize_arxiv_id
from src.core.cancel import raise_if_cancelled
from src.core.capability_policy import allows
from src.core.tracing import web_search_enabled
from src.research import literature

_DOI = re.compile(r"10\.[0-9]{4,9}/[!-~]{1,240}", re.I)
_RECORDS = 24
_MAX_LOOKUPS = 40
_MAX_JOB_LOOKUPS = 450
_MAX_RESPONSE = 24 * 1024
_MAX_OBSERVATIONS = 512 * 1024
_ACTIVE = {"running", "detached"}


def _policy_check():
    raise_if_cancelled("external_literature_observations")
    if not (allows("bibliographic_lookup") and web_search_enabled()):
        raise PermissionError("bibliographic_lookup_disabled")


def _doi_routes(doi):
    value = quote(doi, safe="/")
    if any(segment in {"", ".", ".."} for segment in doi.split("/")):
        return ()
    return (("crossref", f"https://api.crossref.org/works/{value}"),
            ("datacite", f"https://api.datacite.org/dois/{value}"),
            ("openalex", f"https://api.openalex.org/works/doi:{value}"),
            ("openalex", f"https://api.openalex.org/works/doi:{value}?select=is_retracted"))


def _arxiv_route(arxiv):
    return "datacite", "https://api.datacite.org/dois/10.48550/arxiv." + quote(arxiv, safe="/")


def _title_routes(title, author):
    # Match URL identities expected by the pinned upstream observation parser.
    cross = urlencode({"query.bibliographic": f"{title} {author}".strip(), "rows": 3,
                      "select": "DOI,title,author,issued,container-title,type"})
    oa = urlencode({"search": title, "per-page": 3,
                    "select": "id,doi,title,publication_year,authorships,is_retracted"})
    return (("crossref", "https://api.crossref.org/works?" + cross),
            ("openalex", "https://api.openalex.org/works?" + oa))


def _minimal_body(provider, data, searching):
    # Drop abstracts, unrelated embedded records and provider diagnostics.
    def keep(row, keys):
        return {key: row[key] for key in keys if key in row} if isinstance(row, dict) else {}
    if provider == "crossref":
        fields = ("DOI", "title", "author", "issued", "container-title", "type", "updated-by", "relation", "URL")
        msg = data.get("message")
        if not isinstance(msg, dict):
            return None
        if searching:
            return {"message": {"items": [keep(row, fields) for row in (msg.get("items") or [])[:3]]}}
        return {"message": keep(msg, fields)}
    if provider == "datacite":
        record = data.get("data")
        if not isinstance(record, dict) or not isinstance(record.get("attributes"), dict):
            return None
        return {"data": {"attributes": keep(record["attributes"],
            ("doi", "titles", "creators", "publicationYear", "publisher", "relatedIdentifiers"))}}
    fields = ("id", "doi", "title", "display_name", "publication_year", "authorships", "is_retracted", "primary_location")
    if searching:
        return {"results": [keep(row, fields) for row in (data.get("results") or [])[:3]]}
    return keep(data, fields)


def _first_surname(value):
    # URL parameter formatting only, mirroring Kalaris `first_surname`.
    import unicodedata

    first = re.split(r"\s+and\s+", value, maxsplit=1, flags=re.I)[0]
    first = first.split(",", 1)[0] if "," in first else (first.split() or [""])[-1]
    first = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"[^a-z0-9 ]+", " ", first).strip()


def _verify_job(conn, identity):
    job = conn.execute("SELECT owner_user_id,session_id,request_id,status FROM task_jobs WHERE job_id=?",
                       (identity.run_id,)).fetchone()
    if (not job or tuple(job)[:3] != (identity.owner_user_id, identity.session_id, identity.user_turn_id)
            or job[3] not in _ACTIVE):
        raise PermissionError("bibliographic_scope_changed")


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class RegistryReceipts:
    """Job-scoped transport receipts. A dispatch without proof is never repeated."""

    def __init__(self, identity):
        self.identity = identity

    def send(self, key, operation):
        from src.core.task_events import transaction

        digest = sha256(key.encode()).hexdigest()
        identity = self.identity
        with transaction() as conn:
            _verify_job(conn, identity)
            conn.execute("""CREATE TABLE IF NOT EXISTS external_registry_receipts (
                job_id TEXT NOT NULL REFERENCES task_jobs(job_id) ON DELETE CASCADE,
                key TEXT NOT NULL, receipt TEXT, PRIMARY KEY(job_id,key))""")
            row = conn.execute("SELECT receipt FROM external_registry_receipts WHERE job_id=? AND key=?",
                               (identity.run_id, digest)).fetchone()
            if row:
                return json.loads(row[0]) if row[0] else None
            count = conn.execute("SELECT COUNT(*) FROM external_registry_receipts WHERE job_id=?",
                                 (identity.run_id,)).fetchone()[0]
            if count >= _MAX_JOB_LOOKUPS:
                raise LookupError("registry_job_lookup_limit")
            conn.execute("INSERT INTO external_registry_receipts VALUES (?,?,NULL)", (identity.run_id, digest))
        receipt = operation()
        encoded = _encode(receipt)
        if len(encoded.encode()) > _MAX_RESPONSE:
            return None
        with transaction() as conn:
            _verify_job(conn, identity)
            conn.execute("UPDATE external_registry_receipts SET receipt=? WHERE job_id=? AND key=? AND receipt IS NULL",
                         (encoded, identity.run_id, digest))
        return receipt


class RegistryCoverage:
    """Durable CAS for one extraction identity and its observed Worker children."""

    def __init__(self, identity):
        self.identity = identity

    def _schema(self, conn):
        _verify_job(conn, self.identity)
        conn.execute("""CREATE TABLE IF NOT EXISTS external_registry_coverage (
            job_id TEXT NOT NULL REFERENCES task_jobs(job_id) ON DELETE CASCADE,
            execution_id TEXT NOT NULL, report_sha256 TEXT NOT NULL, revision INTEGER NOT NULL,
            child_id TEXT NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY(job_id,execution_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS external_registry_child_coverage (
            job_id TEXT NOT NULL REFERENCES task_jobs(job_id) ON DELETE CASCADE,
            execution_id TEXT NOT NULL, original_id TEXT NOT NULL, state_json TEXT NOT NULL,
            PRIMARY KEY(job_id,execution_id))""")

    def get(self, execution_id, digest):
        from src.core.task_events import transaction
        with transaction() as conn:
            self._schema(conn)
            row = conn.execute("SELECT report_sha256,revision,child_id,state_json FROM external_registry_coverage "
                               "WHERE job_id=? AND execution_id=?", (self.identity.run_id, execution_id)).fetchone()
        if row is None:
            return 0, "", None
        if row[0] != digest:
            raise PermissionError("registry_original_report_changed")
        return row[1], row[2], json.loads(row[3])

    def advance(self, execution_id, digest, revision, state):
        from src.core.task_events import transaction
        encoded = _encode(state)
        if len(encoded.encode()) > _MAX_OBSERVATIONS + (64 << 10):
            raise ValueError("registry_coverage_state_size")
        key = self.identity.run_id, execution_id
        with transaction() as conn:
            self._schema(conn)
            if revision == 0:
                write = conn.execute("INSERT OR IGNORE INTO external_registry_coverage VALUES (?,?,?,1,'',?)",
                                     (*key, digest, encoded))
            else:
                write = conn.execute("UPDATE external_registry_coverage SET revision=revision+1,child_id='',state_json=? "
                                     "WHERE job_id=? AND execution_id=? AND report_sha256=? AND revision=? AND child_id<>''",
                                     (encoded, *key, digest, revision))
            if write.rowcount != 1:
                raise ValueError("registry_coverage_version_changed")

    def bind_child(self, execution_id, digest, revision, child_id, state):
        from src.core.task_events import transaction
        # Child visibility describes only the exact observations that this child
        # received, even when later batches or cancelled attempts exist.
        public = {key: state[key] for key in ("offset", "total", "stop_reason", "coverage")}
        public["original_execution_id"] = execution_id
        public["revision"] = revision
        key = self.identity.run_id, execution_id
        with transaction() as conn:
            self._schema(conn)
            write = conn.execute("UPDATE external_registry_coverage SET child_id=? WHERE job_id=? AND execution_id=? "
                                 "AND report_sha256=? AND revision=? AND child_id IN ('',?)",
                                 (child_id, *key, digest, revision, child_id))
            if write.rowcount != 1:
                raise ValueError("registry_coverage_version_changed")
            prior = conn.execute("SELECT original_id,state_json FROM external_registry_child_coverage "
                                 "WHERE job_id=? AND execution_id=?", (key[0], child_id)).fetchone()
            encoded = _encode(public)
            if prior is not None and tuple(prior) != (execution_id, encoded):
                raise ValueError("registry_child_observation_changed")
            conn.execute("INSERT OR IGNORE INTO external_registry_child_coverage VALUES (?,?,?,?)",
                         (key[0], child_id, execution_id, encoded))

    def child(self, execution_id, *, historical_identity=None):
        from src.core.task_events import transaction
        run_id = self.identity.run_id
        if historical_identity is not None:
            from src.agent.fusion_lineage import prior_report_jobs
            if historical_identity not in prior_report_jobs(self.identity):
                raise PermissionError("registry_historical_scope_denied")
            run_id = historical_identity.run_id
        with transaction() as conn:
            self._schema(conn)
            row = conn.execute("SELECT state_json FROM external_registry_child_coverage WHERE job_id=? AND execution_id=?",
                               (run_id, execution_id)).fetchone()
        return json.loads(row[0]) if row else None


def observation_payload(state):
    if "reference_batch" in state:
        return {"reference_batch": state["reference_batch"]}
    return {"http": state["http"], "refchecker_candidates": state["refchecker_candidates"]}


def batch_observations(report, *, identity, account_epoch, deadline, original_report_sha256):
    """Run the reference kernel once using the accepted extraction and live policy."""
    from citrus_refcheck.audit import BudgetExceeded
    from citrus_refcheck.lookup import AuditStopped
    from src.core.account_admission import AccountDeletionPending, AccountUnavailable
    from src.core.account_leases import acquire_account_lease
    from src.research.fusion_reference_batch import run_reference_batch

    if type(account_epoch) is not int or account_epoch < 0:
        raise PermissionError("bibliographic_account_epoch_missing")

    def check():
        try:
            _policy_check()
            if not allows("skill:literature-verification"):
                raise PermissionError("skill_restricted")
        except PermissionError as exc:
            raise AuditStopped("network_disabled") from exc

    durable = RegistryReceipts(identity)

    def send(key, operation):
        try:
            lease = acquire_account_lease(identity.owner_user_id, expected_epoch=account_epoch)
        except (AccountDeletionPending, AccountUnavailable) as exc:
            raise AuditStopped("lease_lost") from exc
        try:
            return durable.send(key, operation)
        except LookupError as exc:
            if str(exc) != "registry_job_lookup_limit":
                raise
            raise BudgetExceeded("request_budget_exhausted") from exc
        finally:
            lease.release()

    check()
    strict = (report.get("execution", {}).get("options") or {}).get("strict") is True
    batch = run_reference_batch(report["records"], check=check, send=send, deadline=deadline, strict=strict)
    batch["original_report_sha256"] = original_report_sha256
    coverage = []
    for row in batch["records"]:
        attempts = row["attempts"]
        status = ("not_queried" if not attempts else "dispatch_unknown" if any(
            attempt.get("reason") == "dispatch_unknown" for attempt in attempts) else "provider_unavailable" if any(
            attempt["http_status"] not in {200, 404} for attempt in attempts) else "queried")
        coverage.append({"record_index": row["record_index"], "status": status})
    return {"offset": sum(row["status"] != "not_queried" for row in coverage), "total": len(coverage),
            "stop_reason": batch["stop_reason"] or "batch_completed", "coverage": coverage,
            "reference_batch": batch}


def advance_observations(report, previous=None, *, identity=None, deadline=None):
    """Advance a bounded window; explicitly retain every unreached row."""
    _policy_check()
    deadline = min(time.monotonic() + 30, deadline) if deadline is not None else time.monotonic() + 30
    records = report.get("records") or []
    state = {"offset": 0, "total": len(records), "stop_reason": "", "coverage": [], "http": {}, "refchecker_candidates": []}
    if previous:
        if previous["total"] != len(records):
            raise ValueError("registry_original_records_changed")
        state.update(previous)
        state["http"] = dict(state["http"])
        state["coverage"] = list(state["coverage"])
        state["refchecker_candidates"] = list(state["refchecker_candidates"])
    cassette, candidates = state["http"], state["refchecker_candidates"]
    durable = RegistryReceipts(identity) if identity else None
    send = durable.send if durable else lambda key, operation: operation()
    calls = 0

    class StopWindow(Exception):
        pass

    def guarded(key, operation):
        nonlocal calls
        _policy_check()
        if time.monotonic() >= deadline:
            raise StopWindow("time_budget")
        if calls >= _MAX_LOOKUPS:
            raise StopWindow("lookup_window")
        calls += 1
        try:
            return send(key, operation)
        except LookupError as exc:
            if str(exc) != "registry_job_lookup_limit":
                raise
            raise StopWindow("job_lookup_limit") from exc

    def exact_size():
        return len(_encode(observation_payload(state)).encode())

    def query(provider, url, searching):
        status, data, _ = literature._get(url, source=provider)
        body = _minimal_body(provider, data, searching) if status == 200 and isinstance(data, dict) else None
        if status == 200 and not body:
            status = 0
        return {"status": status if type(status) is int else 0, "body": body,
                "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    stop = min(len(records), state["offset"] + _RECORDS)
    state["stop_reason"] = "record_window"
    with literature.bounded_lookup(deadline=deadline, check=_policy_check):
        for position in range(state["offset"], stop):
            record = records[position]
            statuses = []
            def complete(status):
                state["coverage"].append({"record_index": record["record_index"], "status": status})
                state["offset"] = position + 1

            submitted = record.get("submitted") or {}
            arxiv = normalize_arxiv_id(submitted.get("arxiv_id"))
            if record.get("status") in {"verified", "mismatch", "retracted"} and not arxiv:
                complete("previously_observed")
                continue
            doi = str(submitted.get("doi") or "").casefold()
            title = " ".join(str(submitted.get("title") or "").split())[:350]
            author = str(submitted.get("author") or "")[:250]
            if doi:
                routes = _doi_routes(doi) if _DOI.fullmatch(doi) else ()
            else:
                routes = _title_routes(title, _first_surname(author)) if title else ()
            doi_routes = len(routes) if doi else 0
            if arxiv:
                routes = (*routes, _arxiv_route(arxiv)) if doi else (_arxiv_route(arxiv),)
            if not routes:
                complete("no_queryable_identifier")
                continue
            try:
                for index, (provider, url) in enumerate(routes):
                    if index < doi_routes:
                        cross = cassette.get(routes[0][1], {}).get("status")
                        datacite = cassette.get(routes[1][1], {}).get("status")
                        if index == 1 and cross != 404 or index == 2 and datacite != 404 or index == 3 and cross != 200:
                            continue
                    if url not in cassette:
                        receipt = guarded(url, lambda: query(provider, url, not (doi or arxiv)))
                        cassette[url] = receipt or {"status": 0, "body": None, "queried_at": None}
                        if exact_size() > _MAX_OBSERVATIONS:
                            del cassette[url]
                            raise StopWindow("cassette_size_limit")
                    statuses.append(cassette[url]["status"])
                if not doi and not arxiv:
                    # Provider parsers, not model text, produce RefChecker candidates.
                    def supplement():
                        status, rows, _ = literature._crossref_search(title, rows=3)
                        return [{k: row[k] for k in ("doi", "title", "year", "authors") if k in row}
                                for row in rows[:3]] if status == 200 else []
                    marker = len(candidates)
                    for row in guarded("refchecker-candidates:" + title, supplement) or []:
                        received = str(row.get("doi") or "")
                        if not _DOI.fullmatch(received):
                            continue
                        authors = row.get("authors") or []
                        candidates.append({"record_index": record["record_index"], "doi": received,
                            "title": str(row.get("title") or "")[:500], "year": str(row.get("year") or ""),
                            "author": str(authors[0] if isinstance(authors, list) and authors else authors)[:250],
                            "provider": "Crossref", "record_id": received, "status": 200,
                            "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "url": "https://doi.org/" + received})
                    if exact_size() > _MAX_OBSERVATIONS:
                        del candidates[marker:]
                        raise StopWindow("cassette_size_limit")
            except StopWindow as exc:
                state["stop_reason"] = str(exc)
                break
            complete("queried" if statuses and all(status in (200, 404) for status in statuses) else "provider_unavailable")
    if state["offset"] == len(records):
        state["stop_reason"] = "all_records_considered"
    return state


def collect_observations(report, *, identity=None, deadline=None):
    """Compatibility facade for a single bounded observation window."""
    return observation_payload(advance_observations(report, identity=identity, deadline=deadline))
