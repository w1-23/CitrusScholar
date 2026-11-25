"""Register one work resolved by the host's trusted literature connector."""
from hashlib import sha256
import re

from . import bibliography, originals, rights
from .prepare import MAX_SOURCE_BYTES, public_text
from .queue import IntakeQueue, target_url, work_identity


class AutoIntake:
    """Host-only boundary. The caller must verify provenance before registration.

    This adapter makes no network requests. For a raw registration the caller
    must obtain bytes and matching open-license evidence from the verified PMC
    Cloud objects themselves, with no untrusted redirects or transformations.
    """

    def __init__(self, store):
        self.store = store
        self.queue = IntakeQueue(store)

    def register(self, *, doi="", pmid="", pmcid="", title, source_url, raw: bytes | None = None,
                 license_id="", evidence_url="", text_url=""):
        identifiers = _identities(doi, pmid, pmcid)
        source_url = target_url(source_url)
        if not source_url.startswith("https://"):
            raise ValueError("source_url_invalid")
        title = public_text(title, 512)
        if not title:
            raise ValueError("bibliography_title_required")
        aliases = _legacy_aliases(identifiers)
        request = None
        if raw is not None:
            if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SOURCE_BYTES:
                raise ValueError("source_size_invalid")
            pmc_identity = next((item[6:] for item in identifiers if item.startswith("pmcid:")), "")
            license_id, _ = rights.automatic_evidence(pmc_identity, license_id, evidence_url, text_url)
            # Keep the deployed queue wire kind so rollback workers safely
            # treat it as a normal observation that still needs rights review.
            request = {"kind": "observed", "format": "text", "title": title, "pmcid": pmc_identity,
                       "rights_basis": rights.PMC_CLOUD_EVIDENCE,
                       "license": license_id, "evidence_url": evidence_url, "text_url": text_url}
            digest = sha256(raw).hexdigest()
        with self.store.write(timeout=2) as conn:
            work, storage = bibliography.register(conn, identifiers=identifiers, legacy_aliases=aliases,
                                                  title=title, source_url=source_url)
            if request is None:
                return {**work, "state": "metadata_only"}
            job = self.queue.submit(storage, request, actor=rights.PMC_CLOUD_EVIDENCE,
                                    expected_sha256=digest, persist=lambda: originals.archive(self.store.originals, raw),
                                    connection=conn, fingerprint_namespace=rights.PMC_CLOUD_EVIDENCE)
            return {**work, **job}

    def lookup(self, *, doi="", pmid="", pmcid=""):
        """Return reviewed bibliography by a known alias, without a write or full text."""
        with self.store.read() as conn:
            return bibliography.lookup(conn, _identities(doi, pmid, pmcid))


def _identities(doi, pmid, pmcid):
    if not all(isinstance(item, str) for item in (doi, pmid, pmcid)):
        raise ValueError("bibliography_identity_invalid")
    pmid, pmcid = pmid.strip(), pmcid.strip().upper()
    if pmid and not re.fullmatch(r"[1-9][0-9]{0,15}", pmid):
        raise ValueError("pmid_invalid")
    if pmcid and not re.fullmatch(r"PMC[1-9][0-9]{0,15}", pmcid):
        raise ValueError("pmcid_invalid")
    identities = tuple(value for value in (work_identity(doi=doi) if doi else "",
                                           f"pmid:{pmid}" if pmid else "",
                                           f"pmcid:{pmcid}" if pmcid else "") if value)
    if not identities:
        raise ValueError("work_identity_required")
    return identities


def _legacy_aliases(identities):
    prefixes = {"pmid": ("https://pubmed.ncbi.nlm.nih.gov/",),
                "pmcid": ("https://pmc.ncbi.nlm.nih.gov/articles/", "https://www.ncbi.nlm.nih.gov/pmc/articles/")}
    aliases = set()
    for item in identities:
        kind, _, value = item.partition(":")
        for prefix in prefixes.get(kind, ()):
            aliases.update((f"url:{prefix}{value}", f"url:{prefix}{value}/"))
    return aliases
