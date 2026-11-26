"""Resolve an exact journal record and optional original, independently of Fetch output."""
from dataclasses import dataclass
from hashlib import md5, sha256
import html
import json
import re
from threading import Lock
from time import monotonic, sleep
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import requests
from urllib3.exceptions import HTTPError

from citrus_refcheck.providers import norm_title

_EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_NCBI = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
_CROSSREF = "https://api.crossref.org/works/"
_CLOUD = "https://pmc-oa-opendata.s3.amazonaws.com"
_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_DOI = re.compile(r"10\.[0-9]{4,9}/[a-z0-9._;:()/+\-]+", re.ASCII)
_PMID = re.compile(r"[1-9][0-9]{0,11}")
_PMCID = re.compile(r"PMC[1-9][0-9]{0,11}")
_MAX_METADATA = 256 * 1024
_MAX_ORIGINAL = 8 * 1024 * 1024
_NETWORK_BUDGET_SECONDS = 22
_NCBI_RATE_LOCK = Lock()
_ncbi_next_request = 0.0
_UNAVAILABLE = (OSError, ValueError, HTTPError, ElementTree.ParseError, RecursionError)
_ARTICLE_TYPES = frozenset({
    "journal article", "research-article", "research article", "review", "review-article",
    "systematic review", "meta-analysis", "clinical trial", "comparative study", "observational study",
    "case reports", "case report", "brief report",
})
_NON_PAPER = re.compile(
    r"retract|withdraw|expression.?of.?concern|editorial|corrigendum|erratum|correction|"
    r"comment|(?:^|\W)(?:news|letter|dataset|preprint|meeting abstract)(?:$|\W)", re.I)
_NOTICE_TITLE = re.compile(
    r"^(?:retract(?:ion|ed)|withdraw(?:al|n)|expression of concern|editorial|corrigendum|erratum|"
    r"correction|author correction|publisher correction)(?=\s*[:\-]|\s+(?:to|of|for)\b|$)", re.I)
# PMC Cloud provides a single license code, not URLs or prose. Do not run the
# broader legacy license normalizer over this security-sensitive assertion.
_LICENSE_CODES = {
    "cc by": "cc-by", "cc-by": "cc-by", "cc by-sa": "cc-by-sa", "cc-by-sa": "cc-by-sa",
    "cc0": "cc0", "cc zero": "cc0", "cc0-1.0": "cc0", "public domain": "public-domain",
    "public-domain": "public-domain",
}


@dataclass(frozen=True)
class Paper:
    doi: str
    pmid: str
    pmcid: str
    title: str

    def registration(self) -> dict:
        link = ("https://doi.org/" + quote(self.doi, safe="/") if self.doi
                else f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/" if self.pmid
                else f"https://pmc.ncbi.nlm.nih.gov/articles/{self.pmcid}/")
        return {"doi": self.doi, "pmid": self.pmid, "pmcid": self.pmcid,
                "title": self.title, "source_url": link}


@dataclass(frozen=True)
class VerifiedOriginal:
    """Internal proof produced only by an authenticated first-party fetch."""
    doi: str
    pmid: str
    pmcid: str
    title: str
    raw: bytes
    sha256: str
    license_id: str
    evidence_url: str
    text_url: str


def verified_original(paper: dict, license_id: str, evidence_url: str, *, doi="", pmid=""):
    """Validate the fixed-host transport's original and official metadata without I/O.

    Only the PMC Cloud Fetch adapter calls this with bytes from its no-redirect
    HTTPS transport. Vendor, model, and public HTTP payloads are not proof.
    This does not decide journal eligibility; the standalone worker does.
    """
    try:
        doi, pmid, _ = public_identifiers(doi, pmid)
        proof_paper, proof_license, text_url, expected = _cloud_metadata(paper["metadata_raw"], evidence_url)
        if ((doi and doi != proof_paper.doi) or (pmid and pmid != proof_paper.pmid)
                or text_url != paper["url"] or proof_license != license_id):
            return None
        raw = paper["raw_bytes"]
        if not _complete_original(raw, expected):
            return None
        return VerifiedOriginal(proof_paper.doi, proof_paper.pmid, proof_paper.pmcid, proof_paper.title,
                                raw, sha256(raw).hexdigest(), proof_license, evidence_url, text_url)
    except (KeyError, TypeError, ValueError):
        return None


class ExcludedPaper(ValueError):
    """The authoritative result exists but cannot be registered."""


def _doi(value) -> str:
    if type(value) is not str or len(value) > 520:
        return ""
    value = value.strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value)
    if not _DOI.fullmatch(value) or len(value) > 500 or any(p in {"", ".", ".."} for p in value.split("/")):
        return ""
    return value


def _id(value, pattern) -> str:
    if type(value) is int:
        value = str(value)
    return value if type(value) is str and pattern.fullmatch(value) else ""


def identifiers(doi: str, pmid: str, winner_url: str = "") -> tuple[str, str]:
    """Keep exact identifiers, never extract guesses from page text or titles."""
    exact = _doi(doi), _id(pmid.strip(), _PMID) if type(pmid) is str else ""
    if doi or pmid:
        return exact if all(not arg or match for arg, match in zip((doi, pmid), exact)) else ("", "")
    if type(winner_url) is not str or len(winner_url) > 2048:
        return "", ""
    try:
        parts = urlsplit(winner_url)
    except ValueError:
        return "", ""
    if parts.scheme != "https" or parts.query or parts.fragment:
        return "", ""
    if parts.netloc == "doi.org":
        return _doi(unquote(parts.path[1:])), ""
    if parts.netloc == "pubmed.ncbi.nlm.nih.gov":
        match = re.fullmatch(r"/([1-9][0-9]{0,11})/?", parts.path)
        return "", match[1] if match else ""
    return "", ""


def public_identifiers(doi="", pmid="", pmcid="", winner_url=""):
    """Normalize exact inputs and allow only known identifier URL routes."""
    if doi or pmid or pmcid:
        clean_doi = _doi(doi)
        clean_pmid = _id(pmid.strip(), _PMID) if isinstance(pmid, str) else ""
        clean_pmc = _id(pmcid.strip().upper(), _PMCID) if isinstance(pmcid, str) else ""
        if any(raw and not clean for raw, clean in zip((doi, pmid, pmcid), (clean_doi, clean_pmid, clean_pmc))):
            raise ValueError("identifier_invalid")
        return clean_doi, clean_pmid, clean_pmc
    doi, pmid = identifiers("", "", winner_url)
    if doi or pmid:
        return doi, pmid, ""
    if not isinstance(winner_url, str) or len(winner_url) > 2048:
        return "", "", ""
    parts = urlsplit(winner_url)
    if parts.scheme != "https" or parts.query or parts.fragment:
        return "", "", ""
    if parts.netloc == "pmc.ncbi.nlm.nih.gov":
        match = re.fullmatch(r"/articles/(PMC[1-9][0-9]{0,11})(?:\.[1-9][0-9]{0,8})?/?", parts.path)
    elif parts.netloc == "www.ebi.ac.uk":
        match = re.fullmatch(r"/europepmc/webservices/rest/(PMC[1-9][0-9]{0,11})/fullTextXML", parts.path)
    else:
        match = None
    return "", "", match[1] if match else ""


def _read(url, deadline, *, allowed, max_bytes=_MAX_METADATA, params=None):
    if url.startswith(_NCBI):
        _pace_ncbi(deadline)
    remaining = deadline - monotonic()
    if remaining <= 0 or not allowed():
        raise TimeoutError("paper_source_not_available")
    with requests.get(url, params=params, timeout=(min(3, remaining), min(8 if url == _EUROPE_PMC else 4, remaining)), stream=True,
                      headers={"Accept-Encoding": "identity"}, allow_redirects=False) as response:
        length = response.headers.get("Content-Length")
        if response.status_code in {408, 429, 500, 502, 503, 504}:
            raise OSError("paper_source_temporarily_unavailable")
        if (response.status_code != 200 or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                or (length is not None and (not length.isdecimal() or int(length) > max_bytes))):
            raise ValueError("paper_source_invalid_response")
        body = bytearray()
        while len(body) <= max_bytes:
            if monotonic() >= deadline:
                raise TimeoutError("paper_source_deadline")
            chunk = response.raw.read1(min(65536, max_bytes + 1 - len(body)))
            if not chunk:
                if (not body or (length is not None and len(body) != int(length))
                        or monotonic() >= deadline or not allowed()):
                    raise ValueError("paper_source_incomplete")
                return bytes(body)
            body.extend(chunk)
    raise ValueError("paper_source_too_large")


def _pace_ncbi(deadline):
    # The public E-utilities endpoint permits 3 requests/sec without an API key.
    global _ncbi_next_request
    with _NCBI_RATE_LOCK:
        now = monotonic()
        earliest = max(now, _ncbi_next_request)
        if earliest >= deadline:
            raise TimeoutError("paper_source_deadline")
        _ncbi_next_request = earliest + 0.4
    if earliest > now:
        sleep(earliest - now)


def _unique_object(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate_paper_metadata_key")
    return values


def _record(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if type(value) is not dict:
        raise ValueError("paper_source_invalid_record")
    return value


def _title(value):
    if type(value) is not str or len(value) > 1024:
        raise ExcludedPaper("title_unavailable")
    value = " ".join(html.unescape(re.sub(r"<[^>]*>", "", value)).split())
    if (not value or len(value) > 512 or not norm_title(value) or _NOTICE_TITLE.search(value)
            or any(ord(char) < 32 or char in "<>" for char in value)):
        raise ExcludedPaper("invalid_paper_title")
    return value


def _europe_pmc(read, doi, pmid, pmcid="", title=""):
    # A PMID search also checks a supplied DOI, rather than losing evidence of
    # disagreement in a query that already intersects both fields.
    if pmid:
        term = f"EXT_ID:{pmid} AND SRC:MED"
    elif doi:
        term = f'DOI:"{doi}"'
    elif pmcid:
        term = f"PMCID:{pmcid} AND SRC:MED"
    else:
        term = f'TITLE:"{title}" AND SRC:MED'
    data = _record(read(_EUROPE_PMC, params={"query": term, "format": "json", "resultType": "core", "pageSize": 3}))
    result_list = data.get("resultList")
    rows = result_list.get("result") if isinstance(result_list, dict) else None
    if rows == [] and data.get("hitCount") == 0:
        return None
    if not isinstance(rows, list) or len(rows) != 1 or data.get("hitCount") != 1 or not isinstance(rows[0], dict):
        raise ExcludedPaper("exact_record_ambiguous")
    item = rows[0]
    found_doi, found_pmid = _doi(item.get("doi")), _id(item.get("pmid"), _PMID)
    found_pmcid = _id(item.get("pmcid"), _PMCID)
    if (doi and doi != found_doi) or (pmid and pmid != found_pmid) or (pmcid and pmcid != found_pmcid):
        raise ExcludedPaper("identifier_mismatch")
    types = item.get("pubTypeList")
    types = types.get("pubType") if isinstance(types, dict) else None
    journal = item.get("journalInfo")
    journal = journal.get("journal") if isinstance(journal, dict) else None
    venue = journal.get("title") if isinstance(journal, dict) else None
    corrections = item.get("commentCorrectionList") or {}
    corrections = corrections.get("commentCorrection", []) if isinstance(corrections, dict) else [None]
    if (item.get("source") != "MED" or not found_pmid or str(item.get("id")) != found_pmid
            or not _is_journal_article(types, venue)
            or item.get("isRetracted") not in (None, False, "N") or not isinstance(corrections, list)
            or any(not isinstance(c, dict) or re.search(r"retract|withdraw|expression.?of.?concern",
                   str(c.get("type")), re.I) for c in corrections)):
        raise ExcludedPaper("not_a_current_journal_article")
    published_title = _title(item.get("title"))
    if title and norm_title(title) != norm_title(published_title):
        raise ExcludedPaper("public_title_mismatch")
    return Paper(found_doi, found_pmid, found_pmcid, published_title)


def _is_journal_article(types, venue):
    return (isinstance(venue, str) and bool(venue.strip()) and isinstance(types, list)
            and all(isinstance(t, str) for t in types) and any(t.casefold() in _ARTICLE_TYPES for t in types)
            and not any(_NON_PAPER.search(t) for t in types))


def _pubmed(read, doi, pmid):
    params = {"db": "pubmed", "retmode": "json"}
    if not pmid:
        data = _record(read(_NCBI + "esearch.fcgi", params={**params, "term": f'"{doi}"[AID]', "retmax": 2}))
        hit = data.get("esearchresult")
        if not isinstance(hit, dict):
            raise ValueError("pubmed_search_unavailable")
        uids = hit.get("idlist")
        if uids == [] and hit.get("count") == "0":
            return None
        if (not isinstance(uids, list) or len(uids) != 1 or hit.get("count") != "1"
                or not (pmid := _id(uids[0], _PMID))):
            raise ExcludedPaper("exact_record_ambiguous")
    data = _record(read(_NCBI + "esummary.fcgi", params={**params, "id": pmid}))
    result = data.get("result")
    if not isinstance(result, dict):
        raise ValueError("pubmed_summary_unavailable")
    item = result.get(pmid)
    if (result.get("uids") != [pmid] or not isinstance(item, dict) or item.get("uid") != pmid
            or not _is_journal_article(item.get("pubtype"), item.get("fulljournalname"))):
        raise ExcludedPaper("not_a_current_journal_article")
    ids = item.get("articleids")
    if not isinstance(ids, list) or not all(isinstance(x, dict) for x in ids):
        raise ExcludedPaper("identifier_mismatch")
    identified = {kind: [x.get("value") for x in ids if x.get("idtype") == kind]
                  for kind in ("pubmed", "doi", "pmc")}
    if identified["pubmed"] != [pmid] or any(len(values) > 1 for values in identified.values()):
        raise ExcludedPaper("identifier_mismatch")
    found_doi = _doi(next(iter(identified["doi"]), None))
    if doi and found_doi != doi:
        raise ExcludedPaper("identifier_mismatch")
    return Paper(found_doi, pmid, _id(next(iter(identified["pmc"]), None), _PMCID), _title(item.get("title")))


def _crossref(read, doi):
    data = _record(read(_CROSSREF + quote(doi, safe="")))
    item = data.get("message")
    if not isinstance(item, dict) or _doi(item.get("DOI")) != doi:
        return None
    venue, titles = item.get("container-title"), item.get("title")
    relations = item.get("relation") or {}
    updates = item.get("updated-by", [])
    if (item.get("type") != "journal-article" or not isinstance(venue, list)
            or not any(isinstance(name, str) and name.strip() for name in venue)
            or not isinstance(titles, list) or not titles or item.get("subtype")
            or item.get("update-to") or not isinstance(updates, list)
            or any(not isinstance(u, dict) or _NON_PAPER.search(str(u.get("type"))) for u in updates)
            or not isinstance(relations, dict) or any(_NON_PAPER.search(key) for key in relations)):
        raise ExcludedPaper("not_a_current_journal_article")
    return Paper(doi, "", "", _title(titles[0]))


def _version(read, pmcid):
    prefix = f"metadata/{pmcid}."
    data = read(_CLOUD + "/", max_bytes=32768, params={"list-type": "2", "prefix": prefix, "max-keys": "2"})
    if b"<!" in data:
        return ""
    listing = ElementTree.fromstring(data.decode("utf-8"))
    keys = listing.findall(f"{_S3_NS}Contents/{_S3_NS}Key")
    if (listing.tag != f"{_S3_NS}ListBucketResult" or listing.findtext(f"{_S3_NS}Prefix") != prefix
            or listing.findtext(f"{_S3_NS}IsTruncated") != "false" or len(keys) != 1):
        return ""
    match = re.fullmatch(rf"{re.escape(prefix)}([1-9][0-9]{{0,8}})\.json", keys[0].text or "")
    return match[1] if match else ""


def _cloud_metadata(raw, evidence_url):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_METADATA:
        raise ValueError("pmc_metadata_invalid")
    match = re.fullmatch(rf"{re.escape(_CLOUD)}/metadata/(PMC[1-9][0-9]{{0,11}})\.([1-9][0-9]{{0,8}})\.json",
                         evidence_url)
    if not match:
        raise ValueError("pmc_metadata_invalid")
    pmcid, version = match.groups()
    key = f"{pmcid}.{version}"
    metadata = _record(raw)
    if (metadata.get("pmcid") != pmcid or type(metadata.get("version")) is not int
            or str(metadata["version"]) != version):
        raise ValueError("pmc_metadata_identity_mismatch")
    if metadata.get("is_retracted") is True:
        raise ExcludedPaper("pmc_retracted")
    raw_code = metadata.get("license_code")
    license_id = _LICENSE_CODES.get(raw_code.strip().casefold()) if type(raw_code) is str else None
    if (metadata.get("is_retracted") is not False or metadata.get("is_pmc_openaccess") is not True
            or not license_id):
        raise ValueError("pmc_license_unavailable")
    path = f"/{key}/{key}.txt"
    text_uri = metadata.get("text_url")
    expected = re.fullmatch(rf"{re.escape('s3://pmc-oa-opendata' + path)}\?md5=([0-9a-f]{{32}})",
                            text_uri) if isinstance(text_uri, str) else None
    if not expected:
        raise ValueError("pmc_original_unavailable")
    paper = Paper(_doi(metadata.get("doi")), _id(metadata.get("pmid"), _PMID), pmcid, _title(metadata.get("title")))
    return paper, license_id, _CLOUD + path, expected[1]


def _complete_original(raw, expected):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_ORIGINAL or b"\x00" in raw:
        return False
    text = raw.decode("utf-8").lstrip("\ufeff \t\r\n")
    return (len(text.rstrip()) >= 1500 and not text.startswith(("<", "%PDF-"))
            and md5(raw, usedforsecurity=False).hexdigest() == expected)


def _original(read, paper, reuse=lambda *_: None):
    version = _version(read, paper.pmcid)
    if not version:
        return {}
    key = f"{paper.pmcid}.{version}"
    evidence_url = f"{_CLOUD}/metadata/{key}.json"
    try:
        known, license_id, text_url, expected = _cloud_metadata(read(evidence_url), evidence_url)
    except ExcludedPaper:
        raise
    except ValueError:
        return {}
    if ((paper.pmid and known.pmid != paper.pmid) or (paper.doi and known.doi != paper.doi)
            or norm_title(known.title) != norm_title(paper.title)):
        return {}
    raw = reuse(evidence_url, license_id)
    if not _complete_original(raw, expected):
        raw = read(text_url, max_bytes=_MAX_ORIGINAL)
        if not _complete_original(raw, expected):
            return {}
    return {"raw": raw, "license_id": license_id, "evidence_url": evidence_url, "text_url": text_url}


def resolve(doi: str, pmid: str, *, allowed, pmcid="", title="", cached=None, reuse=lambda *_: None,
            strict=False) -> dict | None:
    """Resolve one exact current journal record and, when proven, its original."""
    if not (doi or pmid or pmcid or title) or public_identifiers(doi, pmid, pmcid) != (doi, pmid, pmcid):
        return None
    if title and (any((doi, pmid, pmcid)) or _title(title) != title or any(c in title for c in '\"<>')):
        return None
    deadline = monotonic() + _NETWORK_BUDGET_SECONDS

    def read(url, **kwargs):
        return _read(url, deadline, allowed=allowed, **kwargs)

    try:
        paper = None
        temporary_failure = None
        def europe_pmc(read, doi, pmid):
            return _europe_pmc(read, doi, pmid, pmcid, title)

        if title or (pmcid and not (doi or pmid)):
            registries = (europe_pmc,)
        else:
            registries = (_pubmed, europe_pmc) if pmid else (europe_pmc, _pubmed)
        for registry in registries:
            try:
                paper = registry(read, doi, pmid)
            except ExcludedPaper:
                raise
            except _UNAVAILABLE as exc:
                temporary_failure = exc
                continue
            if paper is not None:
                break
        if paper is None and doi and not (pmid or pmcid):
            paper = _crossref(read, doi)
        if paper is None:
            if strict and temporary_failure is not None:
                raise temporary_failure
            return None
        if pmcid and paper.pmcid != pmcid:
            raise ExcludedPaper("identifier_mismatch")
        result = paper.registration()
        if cached is not None:
            return {**result, **cached} if norm_title(cached["proof_title"]) == norm_title(paper.title) else None
        if paper.pmcid:
            try:
                result.update(_original(read, paper, reuse))
            except ExcludedPaper:
                raise
            except _UNAVAILABLE as exc:
                if strict and isinstance(exc, (OSError, HTTPError)):
                    raise
        return result
    except ExcludedPaper:
        if strict:
            raise
        return None
    except _UNAVAILABLE:
        if strict:
            raise
        return None
