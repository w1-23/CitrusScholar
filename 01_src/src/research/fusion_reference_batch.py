"""Adapt pinned bibliography extraction and host registry receipts to citrus_refcheck."""
from concurrent.futures import Future, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from threading import Lock
import time
from urllib.parse import quote

from citrus_refcheck import providers
from citrus_refcheck.audit import BudgetExceeded
from citrus_refcheck.inventory import digest
from citrus_refcheck.lookup import AuditStopped
from citrus_refcheck.verification import BatchLimits, Capabilities, Observation, verify_batch
from src.research import literature

SCHEMA = "citrus.fusion-reference-batch/1"
METHOD = "citrus_refcheck/reference-verification/1"
MAX_REQUESTS = 120
MAX_RECORDS = 100
_MAX_REPORT_DETAIL_BYTES = 480 * 1024
_KNOWN_FIELDS = ("title", "doi", "arxiv_id", "pmid", "authors", "year", "journal", "venue", "volume", "issue", "pages")
_SAFE_ERROR = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


def _text(value, limit=350):
    if isinstance(value, list):
        value = "; ".join(str(item) for item in value[:16])
    return " ".join(str(value or "").split())[:limit]


def _inventory(records):
    entries = []
    for index, record in enumerate(records, 1):
        if record.get("record_index") != index or not isinstance(record.get("submitted"), dict):
            raise ValueError("fusion_reference_extraction_identity_invalid")
        submitted = record["submitted"]
        claims = {field: _text(submitted.get(field)) for field in _KNOWN_FIELDS if submitted.get(field)}
        for field, aliases in (("authors", ("author",)), ("arxiv_id", ("eprint", "arxiv")),
                               ("pmid", ("pubmed_id", "pubmed"))):
            if field not in claims:
                value = next((submitted[name] for name in aliases if submitted.get(name)), "")
                if value:
                    claims[field] = _text(value)
        citation = _text(submitted.get("raw_reference") or submitted.get("citation"), 1000)
        if citation and "pmid" not in claims:
            match = re.search(r"\bPMID\s*:?\s*([1-9][0-9]{0,9})\b", citation, re.I)
            if match:
                claims["pmid"] = match[1]
        entries.append({"record_index": index, "record_id": str(index), "citation": citation, "claims": claims,
                        "submitted_sha256": digest(submitted)})
    return {"entries": entries}


class _Registries:
    def __init__(self, check, send, strict):
        self.check = check
        self.send = send
        self.strict = strict
        self.lock = Lock()
        self.requests = 0
        self.attempts = {}
        self.pending = {}
        self.limited = set()

    def _request(self, provider, value, lookup, deadline, attempts):
        self.check()
        receipt_key = json.dumps(["reference-batch/1", provider, value], ensure_ascii=False)

        def dispatch():
            self.check()
            if time.monotonic() >= deadline:
                raise AuditStopped("item_deadline")
            with self.lock:
                if provider in self.limited:
                    return {"status": 0, "records": [], "error": "rate_limit_cooldown", "queried_at": None}
                if self.requests >= MAX_REQUESTS:
                    raise BudgetExceeded("request_budget_exhausted")
                self.requests += 1
            try:
                with literature.bounded_lookup(deadline=deadline, check=self.check):
                    status, records, error = lookup()
            except (AuditStopped, BudgetExceeded):
                raise
            except (AttributeError, KeyError, TypeError, ValueError, OSError):
                status, records, error = 0, [], "provider_error"
            error = str(error or "")
            status = status if type(status) is int else 0
            return {"status": status, "records": records[:3] if status == 200 else [],
                    "error": error if not error or _SAFE_ERROR.fullmatch(error) else "provider_error",
                    "queried_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        with self.lock:
            future = self.pending.get(receipt_key)
            owner = future is None
            if owner:
                future = self.pending[receipt_key] = Future()
        if owner:
            try:
                receipt = self.send(receipt_key, dispatch)
                if receipt and receipt["status"] == 429:
                    with self.lock:
                        self.limited.add(provider)
            except BaseException as exc:
                future.set_exception(exc)
                raise
            future.set_result(receipt)
        else:
            try:
                receipt = future.result(timeout=max(0, deadline - time.monotonic()))
            except FutureTimeout as exc:
                raise AuditStopped("item_deadline") from exc
        ref = "registry:" + sha256(receipt_key.encode()).hexdigest()
        if receipt is None:
            receipt = {"status": 0, "records": [], "error": "dispatch_unknown", "queried_at": None}
        attempts.append({"provider": provider, "http_status": receipt["status"],
                         "reason": receipt["error"], "queried_at": receipt["queried_at"], "record_id": ref})
        return receipt

    def for_query(self, query, deadline):
        value = query.value
        attempts = []
        records = []

        def add(provider, key, lookup):
            receipt = self._request(provider, key, lookup, deadline, attempts)
            records.extend(receipt["records"])

        def exact_doi_records():
            return [row for row in records if providers.normalize_doi(row.get("doi")) == value]

        if query.kind == "doi":
            # The parser sees registries only, even if an identifier contains punctuation.
            if not re.fullmatch(r"10\.[0-9]{4,9}/[!-~]{1,240}", value) or any(
                    part in {"", ".", ".."} for part in value.split("/")):
                return Observation("DOI registries", 0, error="invalid_identifier")
            encoded = quote(value, safe="/")
            add("Crossref", ["doi", value], lambda: literature._crossref_work(encoded))
            if not exact_doi_records():
                # An unknown or failed dispatch does not rule out an independent registry.
                add("DataCite", ["doi", value], lambda: literature._datacite_work(encoded))
            if not any(row.get("retracted") is True for row in exact_doi_records()):
                add("OpenAlex", ["doi", value], lambda: literature._openalex_work(encoded))
        elif query.kind == "arxiv":
            exact = f"10.48550/arxiv.{value}"
            add("DataCite (arXiv DOI)", ["arxiv", value], lambda: literature._datacite_work(quote(exact, safe="/")))
            records[:] = [{**row, "arxiv_id": value, "source": "DataCite (arXiv DOI)"}
                          if providers.normalize_doi(row.get("doi")) == exact else row for row in records]
        elif query.kind == "pmid":
            add("Europe PMC", ["pmid", value], lambda: literature._epmc_search(f"EXT_ID:{value} AND SRC:MED", 3))
        else:
            terms = " ".join(filter(None, (value, query.authors)))
            add("Crossref", ["title", terms], lambda: literature._crossref_search(terms, 3))
            if self.strict or not any(providers.norm_title(row.get("title")) == providers.norm_title(value) for row in records):
                add("OpenAlex", ["title", value], lambda: literature._openalex_search(value, 3))
        statuses = [attempt["http_status"] for attempt in attempts]
        error = ""
        if records and (query.kind != "doi" or exact_doi_records()):
            status = 200
        elif 429 in statuses:
            # The adapter already cools down the affected provider. A partial multi-source
            # failure must not cool down the complete DOI or title lookup route.
            status, error = (0, "rate_limited") if query.kind in {"doi", "search"} else (429, "")
        elif any(status not in (200, 404) for status in statuses):
            status = next(status for status in statuses if status not in (200, 404))
            error = next((row["reason"] for row in attempts if row["http_status"] == status and row["reason"]), "provider_error")
        else:
            status = 200 if records else 404
        with self.lock:
            self.attempts[query] = attempts
        return Observation(" + ".join(dict.fromkeys(row["provider"] for row in attempts)), status,
                           tuple(records), tuple(row["record_id"] for row in attempts), error=error)


def _status(row):
    reason = row["reason"]
    if row.get("retraction_status") == "retracted":
        return "retracted"
    if row["match_status"] == "conflict" or any(item["status"] == "conflicting" for item in row["differences"]):
        return "metadata_conflict"
    if row["processing_status"] == "unprocessed":
        return "not_processed"
    if row["match_status"] == "matching":
        return "verified"
    if reason == "not_found":
        return "not_found"
    checks = row.get("identifier_checks", {}).values()
    if reason in {"provider_error", "rate_limited"} or any(
            check["reason"] in {"provider_error", "rate_limited"} for check in checks):
        return "source_unavailable"
    return "unconfirmed"


def _project_match(row):
    match = row.get("match") or {}
    fields = (*_KNOWN_FIELDS, "source", "url", "retracted")
    candidates = row.get("candidates") or []
    return {"status": _status(row), "reason": row["reason"],
            "registered": {field: match[field] for field in fields if match.get(field)},
            "differences": [{field: _text(difference[field], 180) for field in ("field", "status", "submitted", "registered")
                             if field in difference} for difference in row["differences"][:12]],
            "candidates": [{field: item[field] for field in fields if item.get(field)} for item in candidates[:3]]}


def run_reference_batch(records, *, check, send, deadline, strict=False):
    if not 1 <= len(records) <= MAX_RECORDS:
        raise ValueError("fusion_reference_batch_size_invalid")
    registry = _Registries(check, send, strict)
    timeout = max(0.001, min(30.0, deadline - time.monotonic()))
    capabilities = Capabilities(check, lambda _query: None, lookup_doi=registry.for_query,
        lookup_arxiv=registry.for_query, lookup_pmid=registry.for_query, search=registry.for_query)
    start = time.monotonic()
    outcome = verify_batch(_inventory(records), capabilities=capabilities,
                           limits=BatchLimits(request_budget=MAX_RECORDS * 3, timeout=timeout, item_timeout=min(timeout, 8)))
    attempts_by_ref = {attempt["record_id"]: attempt for attempts in registry.attempts.copy().values() for attempt in attempts}
    projected = []
    for row in outcome["entries"]:
        result = row["evidence"]
        record = _project_match(result)
        ids = dict.fromkeys(ref for source in result["sources"] for ref in source["evidence_refs"])
        attempts = [attempts_by_ref[ref] for ref in ids if ref in attempts_by_ref]
        checks = {kind: _project_match(value) for kind, value in result.get("identifier_checks", {}).items()}
        record.update(record_index=row["record_index"], submitted_sha256=row["submitted_sha256"],
                      attempts=attempts, identifier_checks=checks,
                      fields_checked=list(row["claims"]), method=METHOD,
                      retraction_status=result.get("retraction_status", "not_established"))
        if not any(row["claims"].get(field) for field in ("doi", "arxiv_id", "pmid")):
            # A title-only candidate is a lead. Require corroborating submitted metadata.
            if record["status"] == "verified" and not all(row["claims"].get(field) for field in ("authors", "year")):
                record.update(status="unconfirmed", reason="title_only_candidate")
            if record["status"] in {"verified", "not_found"} and "openreview" in " ".join(
                    row["claims"].get(field, "").casefold() for field in ("journal", "venue")):
                record.update(status="unconfirmed", reason="openreview_direct_source_not_checked")
        if result["reason"] == "provider_error" and not attempts and row["claims"].get("doi"):
            record.update(status="not_processed", reason="invalid_identifier")
        projected.append(record)
    record_size = sum(len(json.dumps(row, ensure_ascii=False).encode()) for row in projected)
    if record_size > _MAX_REPORT_DETAIL_BYTES:
        # Preserve every status and source receipt if unusually long metadata exhausts the cassette budget.
        for row in projected:
            row["details_omitted"] = True
            row["registered"] = {field: _text(row["registered"][field], 140)
                                 for field in ("doi", "pmid", "arxiv_id", "title") if field in row["registered"]}
            row["differences"] = row["differences"][:2]
            row["candidates"] = []
            for check in row["identifier_checks"].values():
                check["registered"] = {field: _text(check["registered"][field], 100)
                                       for field in ("doi", "pmid", "arxiv_id", "title") if field in check["registered"]}
                check["differences"] = []
                check["candidates"] = []
    return {"schema": SCHEMA, "records": projected,
            "stop_reason": outcome["stop_reason"] or ("report_detail_budget" if record_size > _MAX_REPORT_DETAIL_BYTES else ""),
            "coverage": outcome["coverage"], "distinct_queries": outcome["budget"]["dispatched"],
            "provider_requests": registry.requests, "elapsed_ms": int((time.monotonic() - start) * 1000)}
