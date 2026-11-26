"""Register one work resolved by the host's trusted literature connector."""
from hashlib import sha256
import json
import re

from . import bibliography, originals, rights
from .prepare import MAX_SOURCE_BYTES, PreparationError, public_text
from .queue import IntakeQueue, target_url, work_identity
from citrus_refcheck.providers import norm_title


class AutoIntake:
    """Host-only boundary. The caller must verify provenance before registration.

    This adapter makes no network requests. For a raw registration the caller
    must obtain bytes and matching open-license evidence from the verified PMC
    Cloud objects themselves, with no untrusted redirects or transformations.
    """

    def __init__(self, store):
        self.store = store
        self.queue = IntakeQueue(store)

    def submit(self, *, owner, epoch, producer, doi="", pmid="", pmcid="", title="", proof=None):
        """Persist public identifiers or a previously authenticated source observation."""
        if (type(owner) is not int or owner <= 0 or type(epoch) is not int or epoch <= 0
                or producer not in {"fetch", "literature", "admin"}):
            raise ValueError("automatic_principal_invalid")
        ids = _identities(doi, pmid, pmcid, required=not title)
        if title:
            if (ids or producer == "fetch" or title != public_text(title, 512)
                    or len(norm_title(title)) < 8 or any(c in title for c in '\"<>')):
                raise ValueError("public_title_invalid")
        request = {"kind": "automatic", "path": "background", "producer": producer, "epoch": epoch,
                   "doi": doi, "pmid": pmid, "pmcid": pmcid, "title": title}
        digest = persist = None
        if proof is not None:
            if producer != "fetch" or title:
                raise ValueError("automatic_proof_invalid")
            raw = proof.raw
            digest = sha256(raw).hexdigest()
            if digest != proof.sha256 or not 0 < len(raw) <= MAX_SOURCE_BYTES:
                raise ValueError("original_hash_mismatch")
            license_id, _ = rights.automatic_evidence(proof.pmcid, proof.license_id, proof.evidence_url, proof.text_url)
            request.update(path="foreground", doi=proof.doi, pmid=proof.pmid, pmcid=proof.pmcid,
                           proof_title=proof.title, license=license_id,
                           evidence_url=proof.evidence_url, text_url=proof.text_url)
            ids = _identities(proof.doi, proof.pmid, proof.pmcid)
            persist = lambda: originals.archive(self.store.originals, raw)
        identity = ids[0] if ids else "title:" + sha256(norm_title(title).encode()).hexdigest()
        fingerprint = sha256(json.dumps([ids, identity], ensure_ascii=False).encode()).hexdigest()
        # Background receipts coalesce repeated discoveries in the same UTC day
        # without preventing a future rediscovery from finding a newer version.
        revision = str(int(self.queue.now() // 86400)) if digest is None else ""
        return self.queue.submit(identity, request, actor=f"account-{owner}", expected_sha256=digest,
                                 revision=revision, persist=persist, timeout=0.1,
                                 fingerprint_namespace=f"automatic:{owner}:{epoch}:{producer}:{request['path']}:{fingerprint}")

    def resolve_job(self, queue, job, record, *, allowed):
        """Promote eligible metadata and bind provenance to original bytes once."""
        identifiers = _identities(record["doi"], record["pmid"], record["pmcid"])
        with self.store.write(timeout=2) as conn:
            if not allowed():
                raise PreparationError("network_scope_denied")
            queue._fence(conn, job)
            _, storage = bibliography.register(conn, identifiers=identifiers, legacy_aliases=_legacy_aliases(identifiers),
                                               title=record["title"], source_url=record["source_url"])
            raw = record.get("raw")
            if raw is None:
                return queue.resolve_automatic(conn, job, identity=storage, request=job["request"],
                                               error="full_original_or_rights_unavailable")
            digest = sha256(raw).hexdigest()
            if job["expected_sha256"] and job["expected_sha256"] != digest:
                raise PreparationError("original_hash_mismatch")
            license_id, _ = rights.automatic_evidence(record["pmcid"], record["license_id"],
                                                      record["evidence_url"], record["text_url"])
            # The foreground and resumed worker already have a hash-verified
            # archive. A newly acquired background source is archived here.
            if not job["expected_sha256"]:
                if not (self.store.originals / digest).exists():
                    originals.archive(self.store.originals, raw)
                elif originals.read_archive(self.store.originals, digest) != raw:
                    raise PreparationError("original_integrity_invalid")
            if not allowed():
                raise PreparationError("network_scope_denied")
            request = {**job["request"], "doi": record["doi"], "pmid": record["pmid"], "pmcid": record["pmcid"],
                       "title": record["title"], "source_url": record["source_url"], "format": "text",
                       "rights_basis": rights.PMC_CLOUD_EVIDENCE, "license": license_id,
                       "evidence_url": record["evidence_url"], "text_url": record["text_url"]}
            request.pop("proof_title", None)
            return queue.resolve_automatic(conn, job, identity=storage, request=request, digest=digest)

    def proven_original(self, evidence_url, license_id):
        """Look up prior exact provenance, even after an operator revoked publication."""
        with self.store.read() as conn:
            rows = conn.execute("""SELECT raw_sha256 FROM rights_events WHERE basis=?
                                   AND json_extract(evidence,'$.locator')=? AND json_extract(evidence,'$.license')=?
                                   UNION SELECT raw_sha256 FROM intake WHERE raw_sha256 IS NOT NULL
                                   AND json_extract(request,'$.rights_basis')=?
                                   AND json_extract(request,'$.evidence_url')=? AND json_extract(request,'$.license')=?
                                   LIMIT 2""", (rights.PMC_CLOUD_EVIDENCE, evidence_url, license_id,
                                                rights.PMC_CLOUD_EVIDENCE, evidence_url, license_id)).fetchall()
        if len(rows) != 1:
            return None
        try:
            return originals.read_archive(self.store.originals, rows[0][0])
        except FileNotFoundError:
            return None

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


def _identities(doi, pmid, pmcid, *, required=True):
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
    if required and not identities:
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
