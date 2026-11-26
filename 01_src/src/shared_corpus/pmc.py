"""Version-bound full text and reuse metadata from the official PMC Cloud bucket."""
from hashlib import md5
import json
import re
from time import monotonic
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
import requests

from .rights import TRUSTED_LICENSES, license_token

_BASE = "https://pmc-oa-opendata.s3.amazonaws.com"
_BUCKET_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_PMC_ROUTES = {
    "pmc.ncbi.nlm.nih.gov": r"/articles/(PMC[0-9]+)(?:\.(?P<version>[1-9][0-9]*))?/?",
    "www.ebi.ac.uk": r"/europepmc/webservices/rest/(PMC[0-9]+)/fullTextXML",
}
PMC_CLOUD_EVIDENCE = "ncbi_pmc_cloud"
PMC_CLOUD_METADATA_URL = re.compile(rf"{re.escape(_BASE)}/metadata/PMC[0-9]+\.[1-9][0-9]*\.json")


def _remaining(deadline: float | None) -> float:
    budget = float("inf") if deadline is None else deadline - monotonic()
    if budget <= 0:
        raise requests.Timeout("PMC Cloud deadline exceeded")
    return budget


def _read(path: str, max_bytes: int, *, params=None, deadline: float | None = None) -> bytes:
    from src.core.capability_policy import allows
    if not (allows("web_fetch") and allows("bibliographic_lookup")):
        raise PermissionError("licensed_full_text_disabled")
    budget = _remaining(deadline)
    with requests.get(_BASE + path, params=params, timeout=(min(2, budget), min(3, budget)), stream=True,
                      allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("pmc_cloud_response_unavailable")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            _remaining(deadline)
            if len(data) + len(chunk) > max_bytes:
                raise ValueError("pmc_cloud_response_too_large")
            data.extend(chunk)
        return bytes(data)


def _version_params(pmcid: str) -> dict:
    return {"list-type": "2", "prefix": f"metadata/{pmcid}.", "max-keys": "2"}


def _only_version(data: bytes, pmcid: str) -> str:
    prefix = _version_params(pmcid)["prefix"]
    # S3 listings have no DTD or entity declarations.
    if b"<!" in data:
        return ""
    listing = ElementTree.fromstring(data.decode("utf-8"))
    keys = listing.findall(f"{_BUCKET_NS}Contents/{_BUCKET_NS}Key")
    if (listing.tag != f"{_BUCKET_NS}ListBucketResult"
            or listing.findtext(f"{_BUCKET_NS}Prefix") != prefix
            or listing.findtext(f"{_BUCKET_NS}IsTruncated") != "false"
            or len(keys) != 1):
        return ""
    match = re.fullmatch(rf"{re.escape(prefix)}([1-9][0-9]*)\.json", keys[0].text or "")
    return match[1] if match else ""


def source_identity(source_url: str) -> tuple[str, str]:
    """Return PMCID and version, if this is an allowed public PMC article route."""
    try:
        parsed = urlsplit(source_url)
        route = _PMC_ROUTES.get(parsed.netloc)
        match = re.fullmatch(route, parsed.path) if route and parsed.scheme == "https" else None
        if match:
            return match[1], match.groupdict().get("version") or ""
    except ValueError:
        pass
    return "", ""


def _validate_metadata(data: bytes, pmcid: str, version: str, doi: str, pmid: str) -> tuple:
    from citrus_refcheck.providers import normalize_doi

    record = json.loads(data.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("invalid PMC Cloud metadata")
    license_id = license_token(record.get("license_code"))
    official_doi = normalize_doi(record.get("doi"))
    official_pmid = str(record.get("pmid") or "")
    title = record.get("title")
    if (record.get("pmcid") != pmcid or type(record.get("version")) is not int
            or str(record["version"]) != version or record.get("is_retracted") is not False
            or record.get("is_pmc_openaccess") is not True or license_id not in TRUSTED_LICENSES
            or (doi and official_doi != doi) or (pmid and official_pmid != pmid)
            or not isinstance(title, str) or not title.strip()):
        raise ValueError("unverified PMC Cloud identity or rights")
    key = f"{pmcid}.{version}"
    text_path = f"/{key}/{key}.txt"
    text_uri = record.get("text_url")
    expected = re.fullmatch(rf"{re.escape('s3://pmc-oa-opendata' + text_path)}\?md5=([0-9a-f]{{32}})",
                            text_uri if isinstance(text_uri, str) else "")
    if expected is None:
        raise ValueError("unverified PMC Cloud source URL")
    return text_path, expected[1], title, license_id, official_doi, official_pmid


def _verified_body(raw: bytes, metadata: bytes, metadata_path: str, article: tuple,
                   *, include_original: bool = False) -> tuple[dict, str, str] | None:
    text_path, expected_hash, title, license_id, doi, pmid = article
    if md5(raw, usedforsecurity=False).hexdigest() != expected_hash:
        return None
    body = raw.decode("utf-8")
    if "\x00" in body:
        return None
    paper = {"url": _BASE + text_path, "title": title, "format": "pmc_cloud_text",
             "content": body, "doi": doi, "pmid": pmid}
    if include_original:
        paper.update(raw_bytes=raw, metadata_raw=metadata)
    return paper, license_id, _BASE + metadata_path


_SOURCE_ERRORS = (requests.RequestException, httpx.HTTPError, OSError, ValueError, ElementTree.ParseError)


def licensed_full_text(source_url: str, *, doi: str, pmid: str, max_bytes: int,
                       deadline: float | None = None) -> tuple[dict, str, str] | None:
    """Return the official body, normalized license and exact metadata URL."""
    pmcid, version = source_identity(source_url)
    if not pmcid:
        return None
    try:
        if not version:
            version = _only_version(_read("/", 32768, params=_version_params(pmcid), deadline=deadline), pmcid)
        if not version:
            return None
        path = f"/metadata/{pmcid}.{version}.json"
        metadata = _read(path, 131072, deadline=deadline)
        article = _validate_metadata(metadata, pmcid, version, doi, pmid)
        raw = _read(article[0], max_bytes, deadline=deadline)
        return _verified_body(raw, metadata, path, article)
    except _SOURCE_ERRORS:
        return None


async def _read_async(client: httpx.AsyncClient, path: str, max_bytes: int, *,
                      params=None, deadline: float) -> bytes:
    from src.core.cancel import raise_if_cancelled
    from src.core.capability_policy import allows

    raise_if_cancelled("fetch_paper_pmc_cloud")
    if not (allows("web_fetch") and allows("bibliographic_lookup")):
        raise PermissionError("licensed_full_text_disabled")
    budget = _remaining(deadline)
    timeout = httpx.Timeout(min(3, budget), connect=min(2, budget))
    async with client.stream("GET", _BASE + path, params=params, timeout=timeout,
                             follow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("pmc_cloud_response_unavailable")
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            _remaining(deadline)
            if len(data) + len(chunk) > max_bytes:
                raise ValueError("pmc_cloud_response_too_large")
            data.extend(chunk)
        return bytes(data)


async def licensed_full_text_async(source_url: str, *, doi: str, pmid: str, max_bytes: int,
                                   deadline: float, client: httpx.AsyncClient) -> tuple[dict, str, str] | None:
    """Read one official source with cancellable I/O and detached original-byte proof."""
    pmcid, version = source_identity(source_url)
    if not pmcid:
        return None
    try:
        if not version:
            listing = await _read_async(client, "/", 32768, params=_version_params(pmcid), deadline=deadline)
            version = _only_version(listing, pmcid)
        if not version:
            return None
        path = f"/metadata/{pmcid}.{version}.json"
        metadata = await _read_async(client, path, 131072, deadline=deadline)
        article = _validate_metadata(metadata, pmcid, version, doi, pmid)
        raw = await _read_async(client, article[0], max_bytes, deadline=deadline)
        return _verified_body(raw, metadata, path, article, include_original=True)
    except _SOURCE_ERRORS:
        return None
