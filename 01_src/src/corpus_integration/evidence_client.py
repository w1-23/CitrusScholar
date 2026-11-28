"""Host ACL and bounded HTTP client for the unified Corpus v1 contract."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3

import httpx

from .selfcorpus_search import SearchUnavailable, _config, _owner, _post
from .settings import unified_enabled

BASE_SOURCES = frozenset({"cqa_literature", "cqa_methods", "cqa_deng_book", "cqa_translated", "cqa_ucr_varieties"})
SOURCE_IDS = BASE_SOURCES | {"evolution"}
SEARCH_PATH = "/api/v1/retrieval/evidence-set"
READ_PATH = "/api/v1/retrieval/evidence/read"
_HASH = re.compile(r"[a-f0-9]{64}")
_STATUS = {"hit", "empty", "partial", "unavailable"}
IDENTITY = ("source_id", "generation_ref", "document_ref", "version_ref", "passage_ref", "passage_hash")


def _ref(value, limit=240):
    return isinstance(value, str) and 0 < len(value) <= limit and value.isascii() and all(32 < ord(c) < 127 for c in value)


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _expect(condition):
    if not condition:
        raise SearchUnavailable("corpus_invalid_response")


def service_available():
    """Probe only delegated-service liveness, without opening an index or searching."""
    from dataclasses import fields

    try:
        from corpus.auth import Actor

        if "base_sources" not in {f.name for f in fields(Actor)}:
            return False
        origin, _secret = _config()
        with httpx.Client(timeout=2.0, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", origin + "/health") as response:
                return response.status_code == 200
    except (SearchUnavailable, ImportError, TypeError, httpx.HTTPError):
        return False


def _transport(route, payload, user):
    from src.auth import user_by_id
    from src.core.cancel import raise_if_cancelled
    from src.core.capability_policy import allows

    def principal():
        if user is None:
            return _owner()
        owner = user.get("user_id")
        current = user_by_id(owner) if type(owner) is int and owner > 0 else None
        if (not current or current.get("user_id") != owner or not current.get("active")
                or current.get("approval_status") != "approved"):
            raise SearchUnavailable("corpus_auth_required")
        return owner

    if not unified_enabled() or not allows("local_rag"):
        raise SearchUnavailable("corpus_disabled")
    raise_if_cancelled("unified_corpus_start")
    try:
        owner = principal()
        origin, secret = _config()
        from corpus.auth import Actor, signed_headers

        actor = Actor(str(owner), projects={}, shared=True, governor=False, base_sources=tuple(sorted(BASE_SOURCES)))
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Content-Type": "application/json"}
        headers.update(signed_headers(secret, "POST", route, body, actor))
        data = asyncio.run(_post(origin, body, headers, route=route))
        raise_if_cancelled("unified_corpus_complete")
        if principal() != owner or not allows("local_rag") or not unified_enabled():
            raise SearchUnavailable("corpus_auth_required")
        return owner, data
    except (ImportError, TypeError, ValueError, sqlite3.Error):
        raise SearchUnavailable("corpus_misconfigured") from None


def _normalize(data, sources, owner, budget):
    _expect(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1)
    status = data.get("status")
    _expect(isinstance(status, str) and status in _STATUS)
    snapshot, outcomes, used, rows = (data.get(key) for key in ("index_snapshot", "source_outcomes", "budget_used", "results"))
    _expect(isinstance(snapshot, dict) and _ref(snapshot.get("snapshot_ref")) and isinstance(snapshot.get("sources"), list)
            and len(snapshot["sources"]) == len(sources) and isinstance(outcomes, list) and len(outcomes) == len(sources)
            and isinstance(rows, list) and len(rows) <= budget["max_results"] and isinstance(used, dict))
    for key in ("candidates", "results", "chars"):
        _expect(type(used.get(key)) is int and 0 <= used[key] <= budget[f"max_{key}"])
    _expect(used["results"] == len(rows))
    generations = {}
    for item in snapshot["sources"]:
        _expect(isinstance(item, dict) and isinstance(item.get("source_id"), str))
        source = item["source_id"]
        entries = item.get("generations")
        _expect(source in sources and source not in generations and isinstance(entries, list) and len(entries) <= 2)
        by_scope = generations[source] = {}
        allowed = {None} if source in BASE_SOURCES else {"shared", f"personal:{owner}"}
        for entry in entries:
            _expect(isinstance(entry, dict))
            scope, ref = entry.get("scope_ref"), entry.get("generation_ref")
            _expect((scope is None or isinstance(scope, str)) and scope in allowed and scope not in by_scope
                    and (ref is None or _ref(ref)))
            by_scope[scope] = ref
    counts, clean_outcomes = {}, []
    for item in outcomes:
        _expect(isinstance(item, dict) and isinstance(item.get("source_id"), str))
        source = item["source_id"]
        reason_codes = item.get("reason_codes")
        _expect(source in sources and source not in counts and isinstance(item.get("status"), str)
                and item["status"] in _STATUS and isinstance(reason_codes, list) and len(reason_codes) <= 16
                and all(isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", v) for v in reason_codes)
                and type(item.get("candidate_count")) is int and 0 <= item["candidate_count"] <= used["candidates"]
                and type(item.get("returned_count")) is int and 0 <= item["returned_count"] <= len(rows))
        counts[source] = item["returned_count"]
        clean_outcomes.append({k: item[k] for k in ("source_id", "status", "reason_codes", "candidate_count", "returned_count")})
    actual = dict.fromkeys(counts, 0)
    references, chars, clean_rows = set(), 0, []
    for row in rows:
        _expect(isinstance(row, dict) and isinstance(row.get("source_id"), str))
        source, scope = row["source_id"], row.get("scope_ref")
        _expect(source in generations and (scope is None or isinstance(scope, str)) and scope in generations[source]
                and all(_ref(row.get(key)) for key in IDENTITY[1:-1]) and _ref(row.get("evidence_ref"), 4096)
                and row["generation_ref"] == generations[source][scope] and row["evidence_ref"] not in references
                and row.get("readback") in ("exact", "excerpt_only") and _text(row.get("title"), 2048)
                and _text(row.get("excerpt"), budget["max_chars"]) and row["excerpt"].strip()
                and (row["readback"] != "exact" or _hash(row.get("passage_hash")))
                and ("source_hash" not in row or row["source_hash"] is None or _hash(row["source_hash"])))
        references.add(row["evidence_ref"])
        chars += len(row["excerpt"])
        actual[source] += 1
        clean = {key: row[key] for key in (*IDENTITY[:-1], "evidence_ref", "title", "excerpt", "readback")}
        clean.update({key: row[key] for key in ("source_hash", "passage_hash", "scope_ref") if row.get(key) is not None})
        if _text(row.get("doi"), 256):
            clean["doi"] = row["doi"]
        if type(row.get("year")) is int and 0 < row["year"] < 10000:
            clean["year"] = row["year"]
        clean_rows.append(clean)
    states = {v["status"] for v in clean_outcomes}
    expected = ("unavailable" if states == {"unavailable"} else "partial" if states & {"partial", "unavailable"}
                else "hit" if rows else "empty")
    _expect(actual == counts and chars == used["chars"] and sum(v["candidate_count"] for v in clean_outcomes) == used["candidates"]
            and status == expected and (status not in {"empty", "unavailable"} or not rows)
            and all(v["status"] == "partial" or (v["status"] == "hit") == bool(v["returned_count"])
                    for v in clean_outcomes))
    return {"status": status, "results": clean_rows, "source_outcomes": clean_outcomes,
            "snapshot_ref": snapshot["snapshot_ref"], "budget_used": {k: used[k] for k in ("candidates", "results", "chars")}}


def search(query, families, limit, topics=None):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000 or type(limit) is not int or not 1 <= limit <= 12:
        raise SearchUnavailable("corpus_invalid_query")
    if (not isinstance(families, list) or len(families) > len(SOURCE_IDS)
            or any(not isinstance(x, str) or x not in SOURCE_IDS for x in families)
            or len(set(families)) != len(families)):
        raise SearchUnavailable("corpus_invalid_source")
    families = families or sorted(SOURCE_IDS)
    if topics is not None and (not isinstance(topics, list) or len(topics) > 32
            or any(not isinstance(x, str) or not x.strip() or len(x) > 120 for x in topics)
            or (topics and not set(families) & BASE_SOURCES)):
        raise SearchUnavailable("corpus_invalid_topic")
    budget = {"max_candidates": 240, "max_results": limit, "max_chars": 24000, "timeout_ms": 4000}
    selectors = [{"source_id": alias, **({"topics": topics} if topics and alias in BASE_SOURCES else {})} for alias in families]
    payload = {"query": query, "sources": selectors, "budget": budget}
    if "evolution" in families:
        payload["evolution_scope"] = {"personal": True, "shared": True, "projects": []}
    import os
    from time import monotonic

    translated = None
    degraded = False
    if os.getenv("CITRUS_CORPUS_BILINGUAL_RETRIEVAL") == "1":
        from src.retrieval.local_query_language import normalize_search_query

        started = monotonic()
        translated, info = normalize_search_query(query, timeout_s=1.2)
        budget["timeout_ms"] = max(1, 4000 - int((monotonic() - started) * 1000))
        if translated and info.get("translation_used"):
            # Keep stable Latin terms, accessions, names and numeric handles verbatim.
            original_terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]*", query)
            present = {m.casefold() for m in re.findall(r"[A-Za-z0-9][A-Za-z0-9._/-]*", translated)}
            terms = list(dict.fromkeys(t for t in original_terms if t.casefold() not in present))
            variant = " ".join([translated, *terms])
            if len(variant) <= 4096:
                payload["english_query"] = variant
            else:
                degraded = True
        else:
            degraded = translated is None
    owner, data = _transport(SEARCH_PATH, payload, None)
    result = _normalize(data, set(families), owner, budget)
    if degraded and result["status"] != "unavailable":
        result["status"] = "partial"
        result["query_translation_degraded"] = True
    return result


def read_exact(binding, offset, max_chars, *, user=None):
    if type(offset) is not int or offset < 0 or type(max_chars) is not int or not 1 <= max_chars <= 30_000:
        raise ValueError("invalid_evidence_read")
    _, data = _transport(READ_PATH, {"evidence_ref": binding["evidence_ref"], "offset": offset, "max_chars": max_chars}, user)
    _expect(isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 1
            and all(data.get(k) == binding.get(k) for k in (*IDENTITY, "evidence_ref")))
    text, total, next_offset = (data.get(k) for k in ("text", "total_chars", "next_offset"))
    _expect(_text(text, max_chars) and type(total) is int
            and type(data.get("offset")) is int and data["offset"] == offset and offset + len(text) <= total
            and len(text) == min(max_chars, total - offset) and "next_offset" in data
            and next_offset == (None if offset + len(text) == total else offset + len(text))
            and (next_offset is None or type(next_offset) is int))
    return text, total, next_offset
