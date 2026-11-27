"""Submit official Fetch originals to the independent personal corpus queue."""
from __future__ import annotations

import asyncio
from base64 import b64encode
from hashlib import sha256
import json
import logging
import os
import re
import sqlite3

from . import selfcorpus_search as wire

logger = logging.getLogger(__name__)
_RECEIPT = re.compile(r"req_[0-9a-f]{32}")
_STATES = frozenset({"queued", "fetching", "fetched", "parsing", "parsed", "chunking",
                    "embedding", "indexing", "validating", "published", "already_present",
                    "failed", "unavailable", "unknown", "partial"})


def enabled() -> bool:
    return wire.enabled() and os.getenv("CITRUS_SELFCORPUS_AUTO_INTAKE") == "1"


def _allowed() -> bool:
    from src.core.capability_policy import allows

    return enabled() and all(allows(name) for name in ("web_fetch", "bibliographic_lookup", "local_rag"))


def eligible() -> bool:
    """Fast precheck before preferring the known official URL in the Fetch race."""
    from src.core.account_leases import current_account_lease
    from src.core.tracing import get_user_id

    owner, lease = get_user_id(), current_account_lease()
    return bool(type(owner) is int and owner > 0 and lease is not None and lease.owner == owner and _allowed())


def _principal() -> tuple[int, int] | None:
    from src.core.account_leases import current_account_lease
    from src.core.tracing import get_user_id
    from .automatic_worker import account_epoch

    if not eligible():
        return None
    owner, lease = get_user_id(), current_account_lease()
    epoch = account_epoch(owner)
    return (owner, epoch) if epoch is not None and lease.epoch == epoch else None


def _receipt(http_status, data, scope):
    if isinstance(data, dict):
        error = data.get("error")
        if http_status == 409 and isinstance(error, dict) and "request_ref" not in data:
            if error.get("code") in {"already_present", "already_pending"}:
                return {"status": error["code"]}
            if error.get("code") == "idempotency_conflict":
                return {"status": "unavailable", "reason": "selfcorpus_idempotency_conflict"}
        ref, status = data.get("request_ref"), data.get("status")
        if (http_status == 202 and data.get("scope_ref") == scope and isinstance(ref, str) and _RECEIPT.fullmatch(ref)
                and data.get("operation_id") == ref and isinstance(status, str) and status in _STATES):
            return {"status": status, "request_ref": ref}
    return {"status": "unavailable", "reason": "selfcorpus_invalid_response"}


def submit(official, *, doi: str, pmid: str) -> dict:
    """Only explicit identities independently matched by the checksummed PMC source can enqueue."""
    from src.core.cancel import raise_if_cancelled
    from .paper_source import verified_original

    if not eligible():
        return {"status": "not_run", "reason": "selfcorpus_intake_not_authorized"}
    if not official or not (doi or pmid):
        return {"status": "not_run", "reason": "selfcorpus_verified_source_required"}
    raise_if_cancelled("selfcorpus_intake_prepare")
    proof = verified_original(*official, doi=doi, pmid=pmid)
    if proof is None or not proof.doi:
        return {"status": "not_run", "reason": "selfcorpus_verified_source_required"}
    try:
        principal = _principal()
        if principal is None:
            return {"status": "not_run", "reason": "selfcorpus_intake_not_authorized"}
        from corpus.auth import Actor, INGEST_TARGET, MAX_INGEST_BODY_BYTES, signed_headers

        origin, secret = wire._config()
        actor = Actor(str(principal[0]), projects={}, shared=False, governor=False)
        scope = f"personal:{actor.subject}"
        metadata = official[0]["metadata_raw"]
        identity = (proof.doi, proof.evidence_url, proof.text_url, proof.sha256, sha256(metadata).hexdigest())
        key = "fetch-pmc-v1:" + sha256(json.dumps(identity, separators=(",", ":")).encode("ascii")).hexdigest()
        spec = {"kind": "verified_text", "doi": proof.doi, "pmid": proof.pmid, "pmcid": proof.pmcid,
                "title": proof.title, "license_id": proof.license_id, "evidence_url": proof.evidence_url,
                "text_url": proof.text_url, "sha256": proof.sha256,
                "content_b64": b64encode(proof.raw).decode("ascii"),
                "metadata_b64": b64encode(metadata).decode("ascii")}
        body = json.dumps({"spec": spec, "scope_ref": scope, "idempotency_key": key}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_INGEST_BODY_BYTES:
            return {"status": "not_run", "reason": "selfcorpus_request_too_large"}
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"}
        headers.update(signed_headers(secret, "POST", INGEST_TARGET, body, actor))
        raise_if_cancelled("selfcorpus_intake_enqueue")
        if _principal() != principal:
            return {"status": "not_run", "reason": "selfcorpus_intake_not_authorized"}
        http_status, data = asyncio.run(wire._post(origin, body, headers, route=INGEST_TARGET, accepted=(202, 409),
                                                   deadline=4.0, response_limit=64 * 1024, include_status=True))
        raise_if_cancelled("selfcorpus_intake_complete")
        if _principal() != principal:
            return {"status": "unavailable", "reason": "selfcorpus_intake_access_changed"}
        result = _receipt(http_status, data, scope)
    except wire.SearchUnavailable as exc:
        result = {"status": "unavailable", "reason": str(exc)}
    except (ImportError, sqlite3.Error, UnicodeError):
        result = {"status": "unavailable", "reason": "selfcorpus_intake_misconfigured"}
    logger.info("Selfcorpus automatic Fetch intake status=%s reason=%s", result["status"], result.get("reason", ""))
    return result
