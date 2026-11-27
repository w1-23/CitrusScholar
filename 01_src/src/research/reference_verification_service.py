"""One bounded, read-only registry verification for explicit bibliography claims."""
from __future__ import annotations

import time

from citrus_refcheck.lookup import AuditStopped
from citrus_refcheck.providers import normalize_arxiv_id, normalize_pmid
from src.core.cancel import ToolCancelled, raise_if_cancelled
from src.core.capability_policy import allows
from src.research.fusion_reference_batch import run_reference_batch

SCHEMA = "citrus.reference-verification/1"
MAX_REFERENCES = 10
MAX_QUERY_SECONDS = 12
_IDENTIFIERS = frozenset({"doi", "arxiv_id", "pmid"})
_FIELDS = _IDENTIFIERS | {"title", "authors", "year", "journal", "venue", "volume", "issue", "pages"}
_REGISTRY_GAPS = frozenset({"source_unavailable", "not_processed"})


def _normalize(references):
    if not isinstance(references, list) or not 1 <= len(references) <= MAX_REFERENCES:
        raise ValueError("reference_batch_size_invalid")
    normalized = []
    for index, ref in enumerate(references, 1):
        if not isinstance(ref, dict) or ref.keys() - _FIELDS:
            raise ValueError("reference_fields_invalid")
        row = {}
        for key, value in ref.items():
            if key in {"year", "pmid", "volume", "issue"} and type(value) is int:
                value = str(value)
            if key == "authors" and isinstance(value, list):
                if len(value) > 16 or any(not isinstance(name, str) or len(name) > 350 for name in value):
                    raise ValueError("reference_field_invalid")
                value = "; ".join(value)
            if not isinstance(value, str) or len(value) > 350:
                raise ValueError("reference_field_invalid")
            if text := " ".join(value.split()):
                row[key] = text
        if not (_IDENTIFIERS | {"title"}).intersection(row):
            raise ValueError("reference_identity_required")
        normalized.append({"record_index": index, "submitted": row})
    return normalized


def _check():
    try:
        raise_if_cancelled("verify_references")
    except ToolCancelled as exc:
        raise AuditStopped("cancelled") from exc
    if not (allows("skill:literature-verification") and allows("bibliographic_lookup")):
        raise AuditStopped("network_disabled")


def _direct(_receipt_key, dispatch):
    """Use the shared bounded registry transport without a Worker or cassette."""
    return dispatch()


def _refine(record, submitted):
    checks = record["field_checks"]
    if record["status"] == "verified" and not _IDENTIFIERS.intersection(submitted):
        if any(checks.get(key, {}).get("status") != "matched" for key in ("title", "authors", "year")):
            record.update(status="unconfirmed", reason="title_metadata_not_corroborated")
    if record["status"] == "source_unavailable" and record["attempts"]:
        reasons = {row["reason"] for row in record["attempts"]}
        if any(row["http_status"] == 429 for row in record["attempts"]) or "rate_limit_cooldown" in reasons:
            record["reason"] = "rate_limited"
        elif "timeout" in reasons or "deadline" in reasons:
            record["reason"] = "deadline" if "deadline" in reasons else "timeout"
    for kind, key, normalize in (("arxiv", "arxiv_id", normalize_arxiv_id), ("pmid", "pmid", normalize_pmid)):
        if key not in submitted:
            continue
        identifier = record["identifier_checks"].get(kind, record)
        registered = identifier["registered"].get(key, "")
        observed = normalize(registered)
        check_status = ("not_checked" if not observed else
                        "matched" if observed == normalize(submitted[key]) else "conflicting")
        checks[key] = {"status": check_status, "submitted": submitted[key], "registered": registered,
                       "reason": identifier["reason"] if check_status == "not_checked" else ""}
    # A confirmed identity does not imply all requested metadata was checked.
    record["fields_checked"] = [key for key, row in checks.items() if row["status"] in {
        "matched", "conflicting", "uncertain"}]
    if record["status"] == "metadata_conflict" and record["reason"] == "identifier_match":
        record["reason"] = "submitted_metadata_conflict"
    return record


def verify_references(references: list[dict]) -> dict:
    """Check numbered public bibliographic records, with no Worker or file I/O.

    Coverage counts rows with usable registry evidence, not the number of
    matches. A completed `not_found` is distinct from a lookup outage or a gap.
    """
    records = _normalize(references)
    query_budget = sum(len(_IDENTIFIERS.intersection(row["submitted"])) or 1 for row in records)
    batch = run_reference_batch(records, check=_check, send=_direct,
        deadline=time.monotonic() + MAX_QUERY_SECONDS, query_budget=query_budget, include_field_checks=True)
    results = [_refine(item, source["submitted"]) for item, source in zip(batch["records"], records, strict=True)]
    usable = sum(item["status"] not in _REGISTRY_GAPS and not any(
        check["status"] in _REGISTRY_GAPS for check in item["identifier_checks"].values()) for item in results)
    requested = len(results)
    reason = batch["stop_reason"]
    status = ("cancelled" if reason in {"cancelled", "canceled"} else
              "not_run" if reason == "network_disabled" and not batch["provider_requests"] else
              "partial" if usable < requested or reason else "complete")
    return {"schema_version": SCHEMA, "status": status, "reason": reason or
            ("registry_coverage_incomplete" if usable < requested else ""), "records": results,
            "coverage": {"requested": requested, "actual": usable, "gap": requested - usable,
                         "processed": batch["coverage"]["actual"]},
            "budget": {"max_references": MAX_REFERENCES, "timeout_seconds": MAX_QUERY_SECONDS,
                       "query_limit": query_budget, "distinct_queries": batch["distinct_queries"],
                       "provider_requests": batch["provider_requests"]}}
