"""Project trusted retrieval receipts without copying bodies or granting access.

The existing per-tool artifact remains authoritative for content and readback.
Do not call this on arbitrary tool or model output.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
from urllib.parse import urlsplit, urlunsplit

_LOCAL = {"local_hybrid_search", "search_candidates"}
_WEB = {
    "deepseek_search": "web_search",
    "deepseek_fetch": "web_fetch",
    "deepseek_fetch_paper": "paper_fetch",
    "browse_web": "browser",
}
_MAX_RESULTS = 24


def _string(value, limit: int = 512) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()[:limit] or None
    return None


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def canonical_http_url(value) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ch.isspace() for ch in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        port = parts.port
        host = parts.hostname.lower()
        netloc = f"[{host}]" if ":" in host else host
        default = 443 if parts.scheme == "https" else 80
        if port is not None and port != default:
            netloc += f":{port}"
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    except ValueError:
        return None


def _local(row: Mapping, generation: str | None) -> dict | None:
    locator = _mapping(row.get("locator"))
    read_entry = _mapping(row.get("read_entry"))
    ref = _string(row.get("source_ref"))
    corpus = _string(locator.get("corpus_id"))
    bound_generation = _string(locator.get("index_generation"))
    # A raw retrieval row (e.g. from a failed storage operation) is not a
    # scoped citation. Only mirror stable refs minted by the evidence adapter.
    if (not ref or not ref.startswith(("ev:", "ce1:")) or not corpus or not bound_generation
            or read_entry.get("ref") != ref or not read_entry.get("turn_id")
            or read_entry.get("generation") != bound_generation
            or (generation is not None and generation != bound_generation)):
        return None
    return {
        "origin": "local", "title": _string(row.get("title"), 240),
        "scope": "authorized_corpus" if ref.startswith("ce1:") else "shared_published",
        "corpus_ref": corpus, "generation": bound_generation,
        "document_ref": _string(locator.get("paper_id")),
        "chunk_ref": _string(locator.get("chunk_id")),
        "source_hash": _string(locator.get("source_hash")),
        "text_sha256": _string(locator.get("input_sha256")),
        "citation_ref": _string(row.get("citation_ref")), "source_ref": ref,
        "retrieved_at": _string(row.get("retrieved_at")),
        "source_uri": canonical_http_url(row.get("url")),
        "source_status": _string(row.get("evidence_level")) or "passage",
    }


def _web(tool_name: str, row: Mapping, artifact: Mapping) -> dict | None:
    url = canonical_http_url(row.get("url"))
    if url is None:
        return None
    browser = _mapping(artifact.get("_browse_evidence")) if tool_name == "browse_web" else {}
    if tool_name == "deepseek_search":
        # Providers may put a summary in the content field. Do not claim fetch.
        source_status = "discovered"
    elif tool_name == "deepseek_fetch_paper" and artifact.get("fetch_status") != "full_text":
        source_status = "partial" if artifact.get("fetch_status") == "abstract_only" else "discovered"
    elif tool_name == "browse_web":
        source_status = "rendered" if browser.get("rendered_text_sha256") else "discovered"
    else:
        source_status = "fetched" if row.get("text") or row.get("content") else "discovered"
    body = row.get("text") or row.get("content")
    text_hash = _string(browser.get("rendered_text_sha256"))
    if text_hash is None and source_status in {"fetched", "partial"} and isinstance(body, str):
        text_hash = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()
    return {
        "origin": "web", "title": _string(row.get("title"), 240), "scope": "public_web",
        "source_uri": url, "url": url, "source_status": source_status,
        "retrieved_at": _string(row.get("retrieved_at") or browser.get("retrieved_at")),
        "published_at": _string(row.get("published_at") or row.get("date")),
        "citation_ref": _string(row.get("citation_ref")),
        "text_sha256": text_hash,
    }


def retrieval_limitation(artifact: Mapping, status: str, reason: str) -> str | None:
    """Classify receipt failures without emitting provider exception strings."""
    local_status = _string(artifact.get("local_status")) or ""
    outcome = _mapping(artifact.get("_tool_outcome"))
    status = status or outcome.get("status")
    code = str(reason or outcome.get("reason_code") or "").casefold()
    if (local_status == "no_authorized_corpus" or outcome.get("reason_class") == "scope_limited"
            or any(key in code for key in ("forbidden", "permission", "not_authorized", "scope_unavailable", "denied"))):
        return "permission_denied"
    if "timeout" in code or "timed_out" in code:
        return "timeout"
    if local_status.startswith("unavailable") or artifact.get("fetch_status") == "failed" or status in {"failed", "unavailable", "not_run", "unknown", "cancelled"}:
        return "source_unavailable"
    if status == "partial" or artifact.get("fetch_status") == "abstract_only" or artifact.get("base_degradation_reason"):
        return "partial_source"
    return None


def evidence_contract(tool_name: str, artifact, *, provider: str = "", status: str = "",
                      reason: str = "") -> dict | None:
    """Compact LocalEvidence/WebEvidence metadata over a Host-owned receipt."""
    if not isinstance(artifact, Mapping):
        return None
    if tool_name in _LOCAL and "main_results" in artifact:
        origin, field = "local", "main_results"
        provider_name = "unified_corpus" if artifact.get("corpus_contract") == 1 else "local_index"
    elif tool_name in _WEB and (
            tool_name != "browse_web" or "web_results" in artifact
            or (artifact.get("error") and artifact.get("action") not in {"close", "close_tab", "list_tabs", "download"})):
        origin, provider_name, field = "web", _WEB[tool_name], "web_results"
        if tool_name == "deepseek_search" and provider:
            provider_name += ":" + provider[:120]
    else:
        return None
    generation = _string(artifact.get("base_generation"))
    rows = artifact.get(field)
    rows = rows if isinstance(rows, (list, tuple)) else ()
    results = []
    for row in rows[:_MAX_RESULTS]:
        if isinstance(row, Mapping):
            item = _local(row, generation) if origin == "local" else _web(tool_name, row, artifact)
            if item is not None:
                results.append(item)
    limitation = retrieval_limitation(artifact, status, reason)
    limitations = [limitation] if limitation else []
    if len(rows[:_MAX_RESULTS]) > len(results):
        limitations.append("scoped_evidence_unavailable" if origin == "local" else "invalid_source_metadata")
    if len(rows) > _MAX_RESULTS:
        limitations.append("evidence_metadata_limit")
    snapshots = sorted({(row["corpus_ref"], row["generation"]) for row in results if origin == "local"})
    if origin == "local" and results and any(row["source_hash"] is None for row in results):
        limitations.append("source_hash_unavailable")
    result_status = ("partial" if results and any(item != "source_hash_unavailable" for item in limitations)
                     else "complete" if results else limitation or ("unavailable" if limitations else "no_results"))
    scopes = {row["scope"] for row in results}
    if not scopes and result_status == "no_results":
        if origin == "web" and tool_name != "browse_web":
            scopes.add("public_web")
        elif origin == "local" and generation and artifact.get("local_status") == "zero_hit":
            scopes.add("shared_published")
    return {
        "schema_version": "retrieval-evidence/1", "origin": origin, "provider": provider_name,
        "status": result_status, "scope": sorted(scopes),
        "index_snapshot": [{"corpus_ref": corpus, "generation": version} for corpus, version in snapshots],
        "coverage": {"sources": len(results)}, "results": results, "limitations": limitations,
    }
