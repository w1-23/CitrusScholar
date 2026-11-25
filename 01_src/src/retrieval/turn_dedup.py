"""Exact read-only retrieval deduplication scoped to one authenticated turn.

The tool node/host still owns authorization, execution and receipts. A hit
only points to the existing call, never copies conclusions or artifacts.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import unicodedata
from urllib.parse import urlsplit, urlunsplit


_DEFAULTS = {
    "deepseek_search": {"count": 5, "need_content": False},
    "deepseek_fetch": {},
    "deepseek_fetch_paper": {"doi": "", "pmid": "", "title": "", "urls": None},
    "local_hybrid_search": {"limit": 5, "l1_categories": None},
    "search_candidates": {"limit": 6},
    "search_agent_corpus": {"limit": 6},
    "search_literature": {"query": "", "doi": "", "arxiv_id": "", "authors": "", "year": "", "limit": 8,
                          "sources": "", "web_fallback": False, "claimed_title": ""},
    "read_candidate": {"candidate_id": "", "source_ref": "", "turn_id": "", "generation": "",
                       "offset": 0, "max_chars": 4000, "gap": "", "limit": 6, "lang": "", "family": ""},
    "read_local_evidence": {"offset": 0, "max_chars": 4000},
    "read_tool_result": {"start_char": 0, "max_chars": 6000, "fields": None, "cursor": ""},
    "read_context_evidence": {"start_char": 0, "max_chars": 8000},
}


def _query(value):
    if not isinstance(value, str):
        return value
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _ordered_set(value, *, normalize=False):
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted({_query(item) if normalize else item for item in value})
    return value


def _url(value):
    if not isinstance(value, str):
        return value
    raw = value.strip()
    try:
        parts = urlsplit(raw)
        # Do not collapse paths, queries, case-sensitive credentials, or
        # different schemes. Drop only the fragment and default port.
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return raw
        port = parts.port
        host = parts.hostname.casefold()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host = f"{host}:{port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except ValueError:
        return raw


def _candidate_read(name, args):
    # An explicit evidence ref identifies the same read across two old tool
    # spellings. Candidate IDs without stable refs retain their own namespace.
    if name == "read_local_evidence":
        args["source_ref"] = args.pop("ref", "")
    if args.get("source_ref") and (name == "read_local_evidence" or not args.get("candidate_id")):
        for item in ("candidate_id", "gap", "limit", "lang", "family"):
            args.pop(item, None)  # listing-only arguments
        name = "read_local_evidence"
    return name, args


def retrieval_key(name: str, args: Mapping) -> str | None:
    """Return only a fingerprint, preserving distinct targets and read pages."""
    if name not in _DEFAULTS or not isinstance(args, Mapping):
        return None
    params = _DEFAULTS[name] | dict(args)
    if "query" in params:
        params["query"] = _query(params["query"])
    if "gap" in params:
        params["gap"] = _query(params["gap"])
    for key in ("corpus_types", "l1_categories", "fields"):
        if key in params:
            params[key] = _ordered_set(params[key], normalize=key != "fields")
    if name == "deepseek_fetch":
        params["url"] = _url(params.get("url"))
    if isinstance(params.get("doi"), str):
        params["doi"] = _query(params["doi"]).removeprefix("https://doi.org/").rstrip(".,;) ")
    if name == "deepseek_fetch_paper":
        if isinstance(params["pmid"], str):
            params["pmid"] = params["pmid"].strip()
        params["title"] = _query(params["title"])
        if isinstance(params["urls"], list):
            params["urls"] = [_url(url) for url in params["urls"]]
        elif params["urls"] is None:
            params["urls"] = []
    if name in {"read_candidate", "read_local_evidence"}:
        name, params = _candidate_read(name, params)
    try:
        data = json.dumps([name, params], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError):
        return None


@dataclass
class RetrievalLedger:
    """One instance per user turn/run, never process-wide or thread-wide."""

    _first_calls: dict[str, str] = field(default_factory=dict)

    def claim(self, name: str, args: Mapping, call_id: str) -> str | None:
        """Call on the async tool node before any await of the actual handler."""
        if not call_id or not (key := retrieval_key(name, args)):
            return None
        original = self._first_calls.setdefault(key, call_id)
        return original if original != call_id else None

    def restore_key(self, key: str, call_id: str) -> None:
        """Adopt a private fingerprint from a verified existing observation."""
        if (isinstance(key, str) and len(key) == 64
                and all(char in "0123456789abcdef" for char in key) and call_id):
            self._first_calls.setdefault(key, call_id)

    def restore(self, name: str, args: Mapping, call_id: str) -> None:
        """Restore only authenticated current-turn attempted/observed calls."""
        self.claim(name, args, call_id)
