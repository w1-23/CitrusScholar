"""Durable public-paper handoff. The request never resolves or downloads here."""
import logging
import os
import sqlite3

from .settings import durable_intake_enabled

logger = logging.getLogger(__name__)


def submit(*, owner: int | None, doi="", pmid="", pmcid="", winner_url="", proof=None):
    """Dedicated Fetch only. Never accept snippets, raw provider text or titles."""
    from .paper_source import VerifiedOriginal, public_identifiers

    try:
        doi, pmid, pmcid = public_identifiers(doi, pmid, pmcid, winner_url)
    except ValueError:
        return None
    if (type(proof) is not VerifiedOriginal or (doi and proof.doi != doi)
            or (pmid and proof.pmid != pmid) or (pmcid and proof.pmcid != pmcid)):
        proof = None
    if not (doi or pmid or pmcid or proof):
        return None
    return _submit(owner, producer="fetch", doi=doi, pmid=pmid, pmcid=pmcid, proof=proof)


def submit_literature(record):
    """Accept only the scholarly producer's returned, public registry record."""
    from src.core.tracing import get_user_id
    from .paper_source import public_identifiers

    if record.get("source") not in {"Crossref", "Europe PMC", "PubMed"}:
        return None
    try:
        doi, pmid, pmcid = public_identifiers(record.get("doi") or "", record.get("pmid") or "",
                                             record.get("pmcid") or "")
    except ValueError:
        return None
    title = "" if (doi or pmid or pmcid) else record.get("title") or ""
    if not (doi or pmid or pmcid or title):
        return None
    return _submit(get_user_id(), producer="literature", doi=doi, pmid=pmid, pmcid=pmcid, title=title)


def _submit(owner, *, producer, **payload):
    from src.core.account_leases import current_account_lease
    from src.core.capability_policy import allows
    from src.core.tracing import get_user_id
    from .automatic_worker import account_epoch

    if not durable_intake_enabled() or type(owner) is not int or owner <= 0 or get_user_id() != owner:
        return None
    root = os.getenv("CITRUS_ISOLATED_CORPUS_ROOT")
    if not root or not allows("web_fetch") or not allows("bibliographic_lookup"):
        return None
    lease = current_account_lease()
    epoch = account_epoch(owner)
    if epoch is None or (lease is not None and (lease.owner != owner or lease.epoch != epoch)):
        return None
    from citrus_corpus.auto_intake import AutoIntake
    from citrus_corpus.store import Store

    try:
        return AutoIntake(Store(root)).submit(owner=owner, epoch=epoch, producer=producer, **payload)
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.debug("Public-paper intake unavailable: %s", type(exc).__name__)
        return None
