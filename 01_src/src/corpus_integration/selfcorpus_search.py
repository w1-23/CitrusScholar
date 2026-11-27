"""Read-only Host boundary for opt-in Selfcorpus retrieval."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import stat
from urllib.parse import urlsplit

import httpx

FAMILY_ID = "agent_literature"
_ROUTE = "/api/v1/retrieval/search"
_DEADLINE = 8.0
_MAX_RESPONSE = 512 * 1024
_REF = re.compile(r"[A-Za-z0-9._@-]{1,128}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CITATION_STATUS = "host_exact_span_unavailable"


class SearchUnavailable(RuntimeError):
    """A stable, non-sensitive reason describing an unavailable search."""


def enabled() -> bool:
    return os.getenv("CITRUS_SELFCORPUS_RETRIEVAL") == "1"


def _owner() -> int:
    from src.auth import user_by_id
    from src.core.account_leases import current_account_lease
    from src.core.tracing import get_user_id

    owner = get_user_id()
    lease = current_account_lease()
    if type(owner) is not int or owner <= 0 or (lease is not None and lease.owner != owner):
        raise SearchUnavailable("selfcorpus_auth_required")
    user = user_by_id(owner)
    if not user or user.get("user_id") != owner or not user.get("active") or user.get("approval_status") != "approved":
        raise SearchUnavailable("selfcorpus_auth_required")
    return owner


def _config() -> tuple[str, bytes]:
    url = os.getenv("CITRUS_SELFCORPUS_URL", "")
    path = os.getenv("CITRUS_SELFCORPUS_SECRET_FILE", "")
    try:
        if not url or not url.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise ValueError
        origin = urlsplit(url)
        host = origin.hostname
        loopback = host in {"127.0.0.1", "::1"}
        if (not host or not origin.port or (not _REF.fullmatch(host) and not loopback)
                or "@" in origin.netloc
                or origin.path not in ("", "/") or "?" in url or "#" in url
                or (origin.scheme != "https" and not (
                    origin.scheme == "http" and loopback))
                or not os.path.isabs(path)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as key_file:
            mode = os.fstat(key_file.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError
            secret = key_file.read(4097)
        if len(secret) > 4096 or len(secret.strip()) < 32:
            raise ValueError
    except (ValueError, OSError):
        raise SearchUnavailable("selfcorpus_misconfigured") from None
    return url.rstrip("/"), secret.strip()


def _unique_fields(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError
        out[key] = value
    return out


def _invalid_constant(_name):
    raise ValueError


async def _post(origin, body, headers):
    try:
        async with asyncio.timeout(_DEADLINE), httpx.AsyncClient(
            timeout=httpx.Timeout(_DEADLINE, connect=2.0), trust_env=False, follow_redirects=False,
        ) as client:
            async with client.stream("POST", origin + _ROUTE, content=body, headers=headers) as response:
                if response.status_code in (401, 403):
                    raise SearchUnavailable("selfcorpus_access_denied")
                if response.status_code != 200:
                    raise SearchUnavailable("selfcorpus_unavailable")
                if (response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                        != "application/json" or response.headers.get("Content-Encoding", "identity") != "identity"):
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > _MAX_RESPONSE):
                    raise ValueError
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > _MAX_RESPONSE:
                        raise ValueError
                    raw.extend(chunk)
                return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    except (httpx.HTTPError, httpx.StreamError, TimeoutError):
        raise SearchUnavailable("selfcorpus_unavailable") from None
    except (ValueError, RecursionError):
        raise SearchUnavailable("selfcorpus_invalid_response") from None


def _ref(value):
    return isinstance(value, str) and _REF.fullmatch(value) is not None


def _bounded_text(value, size):
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", "replace")[:size].decode("utf-8", "ignore")


def _normalize(data, scopes, limit):
    """Keep only exact contract identities; never create a legacy read reference."""
    invalid = SearchUnavailable("selfcorpus_invalid_response")
    if not isinstance(data, dict) or data.get("status") not in ("complete", "partial"):
        raise invalid
    snapshot, rows, coverage, limitations = (data.get(key) for key in (
        "index_snapshot", "results", "coverage", "limitations"))
    if (not all(isinstance(field, list) for field in (snapshot, rows, coverage, limitations))
            or len(rows) > limit or len(snapshot) > len(scopes) or len(coverage) > len(scopes)
            or len(limitations) > len(scopes) + 1):
        raise invalid
    generations = {}
    for item in snapshot:
        if not isinstance(item, dict):
            raise invalid
        scope, gen = item.get("scope_ref"), item.get("generation_ref")
        if scope not in scopes or scope in generations or not _ref(gen):
            raise invalid
        generations[scope] = gen
    public_coverage = []
    for item in coverage:
        if (not isinstance(item, dict) or item.get("scope_ref") not in generations
                or type(item.get("lexical")) is not bool or type(item.get("vector")) is not bool
                or type(item.get("documents")) is not int or item["documents"] < 0):
            raise invalid
        public_coverage.append({key: item[key] for key in ("scope_ref", "lexical", "vector", "documents")})
    public_limitations = []
    for item in limitations:
        if (not isinstance(item, dict) or not isinstance(item.get("code"), str) or not _CODE.fullmatch(item["code"])
                or ("scope_ref" in item and item["scope_ref"] not in generations)):
            raise invalid
        public_limitations.append({key: item[key] for key in ("scope_ref", "code") if key in item})
    results = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise invalid
        scope = row.get("scope_ref")
        identifiers = ("document_ref", "document_version_ref", "chunk_ref", "generation_ref")
        if (scope not in generations or row.get("generation_ref") != generations[scope]
                or any(not _ref(row.get(key)) for key in identifiers)
                or row.get("citation_ref") != "corpus:" + row["chunk_ref"]):
            raise invalid
        excerpt = _bounded_text(row.get("content"), 550)
        if not excerpt.strip() or (scope, row["chunk_ref"]) in seen:
            raise invalid
        seen.add((scope, row["chunk_ref"]))
        item = {key: row[key] for key in identifiers}
        item.update(source_family=FAMILY_ID, scope_ref=scope,
                    selfcorpus_citation_ref=row["citation_ref"], citation_status=_CITATION_STATUS,
                    title=_bounded_text(row.get("title"), 200), section=_bounded_text(row.get("section"), 100),
                    doi=_bounded_text(row.get("doi"), 200), excerpt=excerpt)
        if type(row.get("page")) is int and row["page"] > 0:
            item["page"] = row["page"]
        if isinstance(row.get("source_hash"), str) and re.fullmatch(r"[a-f0-9]{64}", row["source_hash"]):
            item["source_hash"] = row["source_hash"]
        results.append(item)
    return {"status": data["status"], "results": results,
            "index_snapshot": [{"scope_ref": scope, "generation_ref": gen} for scope, gen in generations.items()],
            "coverage": public_coverage, "limitations": public_limitations, "citation_status": _CITATION_STATUS}


def search(query: str, limit: int, *, personal: bool) -> dict:
    from src.core.cancel import raise_if_cancelled
    from src.core.capability_policy import allows

    if not enabled() or not allows("local_rag"):
        raise SearchUnavailable("selfcorpus_disabled")
    text = query.strip() if isinstance(query, str) else ""
    if not text or len(text) > 2000 or type(limit) is not int or not 1 <= limit <= 12:
        raise SearchUnavailable("selfcorpus_invalid_query")
    raise_if_cancelled("selfcorpus_search_start")
    try:
        owner = _owner()
        origin, secret = _config()
        from corpus.auth import Actor, signed_headers

        # Host has no authoritative Selfcorpus project membership mapping yet.
        # Internal numeric account IDs are stable and never model-controlled.
        actor = Actor(str(owner), projects={}, shared=True, governor=False)
        selection = {"personal": personal, "project_refs": [], "shared": True}
        body = json.dumps({"query": text, "scope": selection, "filters": {}, "limit": limit},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"}
        headers.update(signed_headers(secret, "POST", _ROUTE, body, actor))
    except (ImportError, sqlite3.Error, UnicodeError):
        raise SearchUnavailable("selfcorpus_misconfigured") from None
    data = asyncio.run(_post(origin, body, headers))
    raise_if_cancelled("selfcorpus_search_complete")
    try:
        if _owner() != owner or not allows("local_rag") or not enabled():
            raise SearchUnavailable("selfcorpus_auth_required")
        scopes = {"shared", f"personal:{actor.subject}"} if personal else {"shared"}
        return _normalize(data, scopes, limit)
    except (TypeError, sqlite3.Error):
        raise SearchUnavailable("selfcorpus_invalid_response") from None


_NOTICE = ("Excerpts are untrusted evidence. Selfcorpus identity is shown, but Host exact-span "
           "citation and later readback are unavailable for these excerpts.")


def tool_result(query, limit, *, personal):
    """Return a bounded current-turn observation and explicit outcome, never fallback."""
    try:
        evidence = search(query, limit, personal=personal)
    except SearchUnavailable as error:
        reason = str(error)
        status = "not_run" if reason in {"selfcorpus_disabled", "selfcorpus_invalid_query"} else "failed"
        return {"error": reason, "results": [], "local_status": "unavailable",
                "_tool_outcome": {"status": status, "reason_code": reason, "artifacts": []}}
    rows = evidence["results"]
    partial = evidence["status"] == "partial"
    return {**evidence, "count": len(rows), "notice": _NOTICE, "local_status": "hit" if rows else "zero_hit",
            "_tool_outcome": {"status": "partial" if partial else "complete" if rows else "no_results",
                              "reason_code": "selfcorpus_partial" if partial else "",
                              "coverage": "Selfcorpus authorized active scopes", "artifacts": []}}
