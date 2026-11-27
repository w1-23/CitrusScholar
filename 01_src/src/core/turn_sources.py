# -*- coding: utf-8 -*-
"""本轮「来源」解析（v9.8, R16）。

为什么是"按轮次回查"而不是"从回答里识别编号"：
回答正文与工具摘要都经过 ``scrub_internal_keys`` 清洗，模型看不到 ``paper_...``
内部键；同一份工具原始输出仍完整留在 ``tool_results.result_text`` 里。于是
前端按 ``request_id`` 回查这一轮的原始输出，自己解析出"本地文献"和"联网结果"。

两类来源的形状（解析自真实工具输出）::

    [1] 517_paper_65bfd947_000517_F_2014_BMC_microbiology
    Corpus: literature; source_type: literature; ... ; record_id: paper_...; RRF: ...
    <untrusted_local_evidence>
    Background: Huanglongbing (HLB) or citrus greening ...
    </untrusted_local_evidence>

    [1] 标题文本
    URL: https://example.org/paper
    Date: 2024; authority: 2
    Summary: 摘要片段……

要点：
* 本地来源只回 ``record_key`` 与可读标签，键只作请求参数存在；
* 联网来源回 ``url``，前端直接在新标签页打开（不经过本地语料）；
* ``snippet`` 是命中片段的前若干字符，供原文预览高亮，不参与模型上下文。
"""
from __future__ import annotations

import json
import re
import sqlite3
from hashlib import sha256
from typing import Any

from src.core import tool_results, turn_source_ledger

__all__ = ["parse_sources", "sources_for_request", "MAX_SOURCES",
           "sources_payload", "MAX_LOCAL", "MAX_WEB"]

MAX_SOURCES = 12
SNIPPET_CHARS = 220

_ENTRY = re.compile(r"^\s*\[(\d{1,2})\]\s*(.+)$")
_RECORD_KEY = re.compile(r"(?:paper|doc)_[A-Za-z0-9][A-Za-z0-9_.:-]{6,180}")
_RECORD_ID = re.compile(r"record_id:\s*((?:paper|doc)_[A-Za-z0-9][A-Za-z0-9_.:-]{6,180})")
_URL = re.compile(r"^\s*URL:\s*(\S+)", re.M)
_SUMMARY = re.compile(r"^\s*Summary:\s*(.+)$", re.M)
_EVIDENCE = re.compile(r"<untrusted_local_evidence>(.*?)</untrusted_local_evidence>", re.S)
_TAG = re.compile(r"<[^>]{1,80}>")
_TRAILING = "._:-"


def _split_entries(text: str) -> list[tuple[int, str, str]]:
    """把工具输出切成 ``(n, 标题行, 整段)``。"""
    out: list[tuple[int, str, str]] = []
    current_n: int | None = None
    current_head = ""
    buf: list[str] = []
    for line in str(text or "").splitlines():
        match = _ENTRY.match(line)
        if match:
            if current_n is not None:
                out.append((current_n, current_head, "\n".join(buf)))
            current_n = int(match.group(1))
            current_head = match.group(2).strip()
            buf = [line]
            continue
        if current_n is not None:
            buf.append(line)
    if current_n is not None:
        out.append((current_n, current_head, "\n".join(buf)))
    return out


def _clean_snippet(value: str) -> str:
    text = " ".join(_TAG.sub(" ", str(value or "")).replace("...", " ").split())
    return text[:SNIPPET_CHARS]


def _local_key(block: str, head: str) -> str:
    match = _RECORD_ID.search(block)
    if match:
        return match.group(1).strip(_TRAILING)
    found = _RECORD_KEY.findall(head)
    return found[0].strip(_TRAILING) if found else ""


def parse_sources(text: str, limit: int = MAX_SOURCES) -> list[dict[str, Any]]:
    """解析一轮工具原始输出里的本地/联网来源（保序、去重、限量）。"""
    cap = max(1, min(int(limit or MAX_SOURCES), MAX_SOURCES))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, head, block in _split_entries(text):
        url_match = _URL.search(block)
        if url_match:
            url = turn_source_ledger.safe_http_url(url_match.group(1).strip())
            if not url or url in seen:
                continue
            summary = _SUMMARY.search(block)
            from src.core.web_output_separator import clean_web_title
            title = clean_web_title(_TAG.sub(" ", head), url)
            item = {
                "kind": "web",
                "n": number,
                "title": title[:180],
                "url": url,
                "snippet": _clean_snippet(summary.group(1) if summary else ""),
            }
            seen.add(url)
            out.append(item)
            if len(out) >= cap:
                break
            continue

        evidence = _EVIDENCE.search(block)
        key = _local_key(block, head)
        if not key and not evidence:
            continue
        if key:
            if key in seen:
                continue
            seen.add(key)
        item = {
            "kind": "local",
            "n": number,
            "record_key": key,
            "title": "",
            "snippet": _clean_snippet(evidence.group(1) if evidence else head),
        }
        out.append(item)
        if len(out) >= cap:
            break
    return out


MAX_LOCAL = 8
MAX_WEB = 6


def _read_ref(row: dict) -> dict | None:
    try:
        ref = json.loads(row.get("read_ref") or "null")
    except (ValueError, RecursionError):
        return None
    return ref if isinstance(ref, dict) else None


def _local_evidence(ref: dict, session_id: str, user: dict, *, preview: bool = False) -> dict | None:
    from src.retrieval.evidence_adapter import read_evidence
    width = ref["end"] - ref["start"]
    try:
        result = read_evidence(ref["ref"], turn_id=ref["turn_id"], generation=ref["generation"],
                               offset=ref["start"], max_chars=width if preview else min(width, SNIPPET_CHARS),
                               user=user, session_id=session_id)
    except (ValueError, OSError, PermissionError):
        return None
    expected_end = ref["end"] if preview else min(ref["end"], ref["start"] + SNIPPET_CHARS)
    return result if result["status"] == "ok" and result["range"]["end"] == expected_end else None


def read_local_citation(key: str, user: dict) -> dict | None:
    row = turn_source_ledger.find_local_citation(key)
    ref = turn_source_ledger.local_read_reference(_read_ref(row)) if row else None
    if not ref or turn_source_ledger.reference_key("cite", ref) != key:
        return None
    result = _local_evidence(ref, row["session_id"], user, preview=True)
    if not result:
        return None
    return {"ok": True, "kind": "stored_evidence", "record_key": key,
            "source_ref": ref["ref"], "read_ref": ref, "title_label": row["title"],
            "corpus": result["source"]["locator"]["corpus_id"], "markdown": result["text"],
            "chars": len(result["text"])}


CITATION_PAGE_CHARS = 60_000
_CITE_KEY = re.compile(r"cite:[a-f0-9]{64}")


def citation_bubble_id(item: dict) -> str:
    """Read selector from the bound identity, never a model-facing citation ordinal."""
    if item.get("kind") == "local":
        key = item.get("record_key")
        return key if isinstance(key, str) and _CITE_KEY.fullmatch(key) else ""
    if item.get("kind") == "shared":
        from src.shared_corpus.references import read_reference
        ref = read_reference(item)
        if ref:
            return turn_source_ledger.reference_key("shared", ref)
    return ""


def _cited_ledger_entry(item: dict, session_id: str, request_id: str) -> tuple[dict, dict] | None:
    key = citation_bubble_id(item)
    kind = item.get("kind")
    if not key or not session_id or not request_id:
        return None
    for row in turn_source_ledger.fetch_rows(session_id, request_id):
        if row["kind"] != kind or row["source_key"] != key:
            continue
        ref = _read_ref(row)
        if kind == "local":
            ref = turn_source_ledger.local_read_reference(ref)
            ref_kind = "cite"
        else:
            from src.shared_corpus.references import read_reference
            ref = read_reference({"read_ref": ref})
            ref_kind = "shared"
        if ref and turn_source_ledger.reference_key(ref_kind, ref) == key:
            return row, ref
    return None


def _cited_page(document: dict, *, offset: int | None, length: int) -> dict | None:
    text = document["markdown"]
    hit_start, hit_end = document["hit_start"], document["hit_end"]
    if not 0 <= hit_start < hit_end <= len(text):
        return None
    target = hit_start if offset is None else offset
    if target >= len(text):
        return None

    hit_page_start = hit_start // length * length
    if hit_page_start + length < hit_end:
        hit_page_start = hit_start
    hit_page_end = max(hit_page_start + length, hit_end)

    def page_start(position):
        if position < hit_page_start:
            return position // length * length
        if position < hit_page_end:
            return hit_page_start
        return hit_page_end + (position - hit_page_end) // length * length

    start = page_start(target)
    end = start + length
    if start < hit_page_start:
        end = min(end, hit_page_start)
    elif start == hit_page_start:
        end = hit_page_end
    end = min(end, len(text))
    return {**document, "ok": True, "offset_unit": "unicode_code_point",
            "start": start, "end": end, "total_chars": len(text), "chars": end - start,
            "previous_start": page_start(start - 1) if start else None,
            "next_start": end if end < len(text) else None, "markdown": text[start:end]}


def _local_cited_document(ref: dict, row: dict, session_id: str, user: dict) -> dict | None:
    from src.core import corpus_source
    from src.core.capability_policy import allows
    from src.retrieval.evidence_adapter import read_evidence

    exact = _local_evidence(ref, session_id, user, preview=True)
    if not exact:
        return None
    locator = exact["source"]["locator"]
    details = {"title": row["title"], "corpus": locator["corpus_id"]}
    if allows("local_rag"):
        chunk = read_evidence(ref["ref"], turn_id=ref["turn_id"], generation=ref["generation"],
                              offset=0, max_chars=30_000, user=user, session_id=session_id)
        if chunk["status"] != "ok":
            return None
        if (chunk["next_offset"] is None and chunk["source"]["locator"] == locator
                and sha256(chunk["text"].encode("utf-8")).hexdigest() == locator.get("input_sha256")
                and chunk["text"][ref["start"]:ref["end"]] == exact["text"]):
            reconstructed = (corpus_source.reconstruct_cited_document(locator, ref["generation"], user)
                             if not ref["ref"].startswith("ce1:") else None)
            if reconstructed is not None and allows("local_rag"):
                # Check the evidence again after expanding to the entire document.
                if not _local_evidence(ref, session_id, user, preview=True):
                    return None
                return {**details, **reconstructed, "kind": "reconstructed_markdown"}
            if allows("local_rag"):
                return {**details, "kind": "stored_evidence", "coverage": "chunk", "markdown": chunk["text"],
                        "hit_start": 0, "hit_end": len(chunk["text"]), "md_sha256": locator["input_sha256"]}
    text = exact["text"]
    return {**details, "kind": "stored_evidence", "coverage": "excerpt", "markdown": text,
            "hit_start": 0, "hit_end": len(text), "md_sha256": sha256(text.encode("utf-8")).hexdigest()}


def read_cited_source(item: dict, user: dict, *, session_id: str, request_id: str,
                      offset: int | None = None, length: int = CITATION_PAGE_CHARS) -> dict | None:
    """Expand a durable answer bubble already selected by the owned-turn API."""
    from src.corpus_integration.read_access import read_reference_document
    from src.retrieval.evidence_adapter import _require_session
    from src.shared_corpus.activity import MaintenanceBusy

    if (type(length) is not int or not 1 <= length <= CITATION_PAGE_CHARS
            or (offset is not None and (type(offset) is not int or offset < 0))
            or type(user.get("user_id")) is not int or user["user_id"] <= 0):
        return None
    try:
        _require_session(user["user_id"], session_id)
        verified = _cited_ledger_entry(item, session_id, request_id)
        if not verified:
            return None
        row, ref = verified
        if row["kind"] == "local":
            document = _local_cited_document(ref, row, session_id, user)
        elif user.get("active") and user.get("approval_status") == "approved":
            document = {**read_reference_document(ref), "kind": "stored_markdown"}
        else:
            return None
        _require_session(user["user_id"], session_id)
        return _cited_page(document, offset=offset, length=length) if document else None
    except (LookupError, MaintenanceBusy, OSError, sqlite3.Error, ValueError):
        return None


def _retained_shared_rows(records):
    from src.shared_corpus.references import evidence_rows
    for stored in records:
        name = stored.get("tool_name")
        if name not in {"search_agent_corpus", "local_hybrid_search"}:
            continue
        if not tool_results.storage_integrity(stored)["complete"]:
            continue
        for row, ref in evidence_rows(name, stored.get("result_text") or ""):
            yield {"origin": "current", "kind": "shared", "title": row.get("title"),
                   "source_key": turn_source_ledger.reference_key("shared", ref), "read_ref": json.dumps(ref)}


def _project_sources(session_id: str, request_id: str, user: dict, cap: int) -> tuple[list[dict], bool]:
    from itertools import chain
    from src.shared_corpus.references import read_reference
    from src.corpus_integration.read_access import reference_visible

    ledger = turn_source_ledger.fetch_rows(session_id, request_id)
    records = tool_results.fetch_by_request(session_id, request_id)
    stored = {row["tool_call_id"]: row for row in records}
    sources, seen = [], set()
    counts = {"local": 0, "web": 0, "shared": 0}
    limits = {"local": MAX_LOCAL, "web": MAX_WEB, "shared": 4}
    unverified_legacy = False
    shared = (row for row in ledger if row["kind"] == "shared")
    other = (row for row in ledger if row["kind"] != "shared")
    for row in chain(shared, _retained_shared_rows(records), other):
        kind, key = row["kind"], row["source_key"]
        if kind not in counts or counts[kind] >= limits[kind] or (kind, key) in seen:
            continue
        ref = _read_ref(row)
        if not ref:
            unverified_legacy = True
            continue
        item = {"kind": kind, "origin": row["origin"], "read_ref": ref, "source_status": "unavailable"}
        if kind == "local":
            ref = turn_source_ledger.local_read_reference(ref)
            if not ref or turn_source_ledger.reference_key("cite", ref) != key:
                continue
            item.update(record_key=key, source_ref=ref["ref"], title="本地引用不可用")
            evidence = _local_evidence(ref, session_id, user)
            if evidence:
                locator = evidence["source"]["locator"]
                item.update(source_status="available", title=row["title"], corpus=locator["corpus_id"],
                            year=locator.get("year"), doi=locator.get("doi"),
                            document_key=evidence["source"]["document_key"],
                            snippet=turn_source_ledger.safe_text(evidence["text"], SNIPPET_CHARS))
        elif kind == "shared":
            ref = read_reference({"read_ref": ref})
            if not ref or turn_source_ledger.reference_key("shared", ref) != key:
                continue
            item.update(source_ref=key, title="共享引用不可用")
            if (user.get("active") and user.get("approval_status") == "approved"
                    and reference_visible(ref)):
                item.update(source_status="available", title=turn_source_ledger.safe_text(row["title"], 200))
        else:
            url = turn_source_ledger.safe_http_url(row.get("url"))
            if not url:
                continue
            call = stored.get(ref.get("tool_call_id"))
            item.update(source_ref=turn_source_ledger.reference_key("web", ref | {"url": url}),
                        title="网页引用不可用")
            if (call and call["full_sha256"] == ref.get("full_sha256")
                    and tool_results.storage_integrity(call)["complete"]):
                item.update(source_status="available", url=url, title=row["title"], snippet=row["snippet"])
        seen.add((kind, key))
        counts[kind] += 1
        sources.append(item)
        if len(sources) >= cap:
            break
    return sources, unverified_legacy


def sources_for_request(session_id: str, request_id: str, *, user: dict,
                        limit: int = MAX_SOURCES) -> list[dict[str, Any]]:
    cap = max(1, min(int(limit or MAX_SOURCES), MAX_SOURCES))
    return _project_sources(session_id, request_id, user, cap)[0]


def sources_payload(session_id: str, request_id: str, *, user: dict,
                    limit: int = MAX_SOURCES) -> dict[str, Any]:
    cap = max(1, min(int(limit or MAX_SOURCES), MAX_SOURCES))
    sources, legacy = _project_sources(session_id, request_id, user, cap)
    origins = {item["origin"] for item in sources}
    status = "mixed" if len(origins) > 1 else next(iter(origins), "unavailable" if legacy else "none")
    return {"status": status, "sources": sources}


_ANSWER_REF = re.compile(r"\[(L\d{1,3}|W\d{1,3}|H\d{1,3}|C\d{1,3}|\d{1,2})\]", re.I)


def _reference_text(value, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:max(0, int(limit))]


def _reference_score(value) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def references_data(answer: str, main_results, web_results) -> dict[str, Any]:
    """Project retained, authorized sources into the existing citations contract.

    Display ids come from the tool output (``L#`` for local, ``W#`` for web) and
    are stored with the evidence.  They are never re-derived from array
    positions, so multi-batch searches, duplicate URLs and repeated chunks keep
    the same citation identity.  Legacy numeric markers still map to local
    order, and ``[cNN]`` maps to a visible candidate id.
    """
    from src.core.evidence import clean_doi, src_of

    local, seen_local, by_ref = [], set(), {}
    local_by_key, by_candidate = {}, {}
    for row in main_results if isinstance(main_results, list) else []:
        if not isinstance(row, dict):
            continue
        doi = clean_doi(row.get("doi"))
        key = (doi or str(row.get("record_key") or row.get("chunk_id") or
               row.get("title") or "").strip())
        if not key:
            continue
        if key in seen_local:
            duplicate_ref = str(row.get("citation_ref") or "").strip().upper()
            if re.fullmatch(r"L\d{1,3}", duplicate_ref):
                by_ref.setdefault(duplicate_ref, local_by_key[key])
            continue
        seen_local.add(key)
        ref = str(row.get("citation_ref") or "").strip().upper()
        if not re.fullmatch(r"L\d{1,3}", ref):
            ref = str(len(local) + 1)
        if ref in by_ref:
            continue
        title = _reference_text(row.get("title") or row.get("paper_title"), 180) or "本地文献"
        entry = {
            "ref_id": ref, "type": "main", "source": src_of(row),
            "doi": doi or "N/A", "url": "", "title": title,
            "year": _reference_text(row.get("year"), 32),
            "score": _reference_score(row.get("score", row.get("rerank_score"))),
            "chunk_id": _reference_text(row.get("chunk_id"), 200),
            "text_preview": _reference_text(row.get("snippet") or row.get("text"), 250),
        }
        candidate_id = str(row.get("candidate_id") or "").strip()
        if candidate_id:
            entry["candidate_id"] = candidate_id
        local.append(entry)
        local_by_key[key] = entry
        by_ref[ref] = entry
        for alias in (candidate_id, row.get("source_ref")):
            if alias:
                by_candidate.setdefault(str(alias).strip().casefold(), entry)

    from src.core.source_tiers import classify_url

    tier_labels = {
        "T1": "同行评议 / 官方 / 学术数据库",
        "T2": "权威与行业媒体 / 机构新闻稿",
        "T3": "自媒体 / 论坛 / 来源不明",
    }
    web, seen_web, by_web, web_by_url = [], set(), {}, {}
    for row in web_results if isinstance(web_results, list) else []:
        if not isinstance(row, dict):
            continue
        url = turn_source_ledger.safe_http_url(row.get("url"))
        if not url:
            continue
        if url in seen_web:
            duplicate_ref = str(row.get("citation_ref") or "").strip().upper()
            if re.fullmatch(r"W\d{1,3}", duplicate_ref):
                by_web.setdefault(duplicate_ref, web_by_url[url])
            continue
        seen_web.add(url)
        ref = str(row.get("citation_ref") or "").strip().upper()
        if not re.fullmatch(r"W\d{1,3}", ref):
            ref = f"W{len(web) + 1}"
        if ref in by_web:
            continue
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        tier = classify_url(url, str(row.get("doi") or ""))
        entry = {
            "ref_id": ref, "type": "web", "source": "web", "url": url,
            "title": _reference_text(row.get("title"), 180) or host or url,
            "host": host,
            "snippet": _reference_text(row.get("abstract") or row.get("snippet") or row.get("text"), 320),
            "round_no": 1, "is_summary": False,
            "origin_label": "网络来源",
            "tier": tier.tier, "tier_rule": tier.rule,
            "tier_label": f"网络来源 · {tier.tier} {tier_labels[tier.tier]}",
            "role": "candidate", "cited": False,
        }
        web.append(entry)
        web_by_url[url] = entry
        by_web[ref] = entry

    cited_local, cited_web, dropped = set(), set(), []
    for marker in _ANSWER_REF.findall(str(answer or "")):
        token = marker.upper()
        entry = None
        if token.startswith("H"):
            entry = None
        elif token.startswith("W"):
            entry = by_web.get(token)
        elif token.startswith("L"):
            entry = by_ref.get(token)
        elif token.startswith("C"):
            entry = by_candidate.get(marker.casefold())
        elif token.isdigit():
            number = int(token)
            entry = local[number - 1] if 1 <= number <= len(local) else None
        if entry is None:
            dropped.append(marker)
        elif entry["type"] == "web":
            cited_web.add(entry["ref_id"])
            entry["cited"] = True
            entry["role"] = "cited"
        else:
            cited_local.add(entry["ref_id"])
            entry["cited"] = True
            entry["role"] = "cited"
            entry["type"] = "main"

    for item in web:
        if item["ref_id"] not in cited_web:
            item["cited"] = False
            item["role"] = "candidate"
    cited = [item for item in local if item["ref_id"] in cited_local]
    uncited = [item for item in local if item["ref_id"] not in cited_local]
    for item in uncited:
        item["type"] = "candidate"
        item["role"] = "candidate"
        item["cited"] = False
    return {
        "cited": cited,
        "uncited": uncited,
        "historical": [],
        "web_separate": web,
        "remap": {},
        "dropped": list(dict.fromkeys(dropped)),
        "pre_renumbered": True,
    }


_REFERENCE = r"[LWHC][0-9]{1,3}"
_REFERENCE_GROUP = rf"{_REFERENCE}(?:[^\S\n]*[,，、;；][^\S\n]*{_REFERENCE})*"
_INLINE_REFERENCE = re.compile(
    rf"\[[^\S\n]*({_REFERENCE_GROUP})[^\S\n]*\]|【[^\S\n]*({_REFERENCE_GROUP})[^\S\n]*】", re.I)
_MARKER = re.compile(_REFERENCE, re.I)
_LINK_LABEL = r"(?:\\.|[^\[\]\\\n]|\[[^\]\n]*\])*"
_LITERAL_MARKDOWN = re.compile(
    rf"^[ \t]{{0,3}}(?P<ticks>`{{3,}})[^\n]*\n[\s\S]*?(?:^[ \t]{{0,3}}(?P=ticks)`*[ \t]*(?:\n|$)|\Z)"
    rf"|^[ \t]{{0,3}}(?P<tildes>~{{3,}})[^\n]*\n[\s\S]*?(?:^[ \t]{{0,3}}(?P=tildes)~*[ \t]*(?:\n|$)|\Z)"
    r"|^[ \t]*>[^\n]*|(?<!`)(?P<inline>`+)(?!`)[^\n]*?(?<!`)(?P=inline)(?!`)"
    r"|\\(?:\[[^\]\n]*\]|【[^】\n]*】)"
    rf"|^[ \t]{{0,3}}\[{_LINK_LABEL}\]:[^\n]*"
    rf"|!?\[{_LINK_LABEL}\]\([^\n]*?\)"
    rf"|!?\[(?![^\S\n]*{_REFERENCE_GROUP}[^\S\n]*\]){_LINK_LABEL}\][ \t]*\[{_LINK_LABEL}\]"
    r'|“[^”\n]*”|「[^」\n]*」|『[^』\n]*』|"[^"\n]*"', re.M | re.I)


def _visible_shared_reference(ref, user):
    from src.shared_corpus.references import read_reference
    from src.corpus_integration.read_access import reference_visible

    if "active" not in user or "approval_status" not in user:
        from src.auth import user_by_id
        user = user_by_id(int(user.get("user_id") or 0)) or {}
    ref = read_reference({"read_ref": ref})
    if ref and user.get("active") and user.get("approval_status") == "approved":
        if reference_visible(ref):
            return ref
    return None


def _citation_entries(session_id, request_id):
    entries = {}
    for row in turn_source_ledger.fetch_rows(session_id, request_id):
        marker = str(row.get("citation_ref") or "").upper()
        if marker:
            entries[marker] = row if marker not in entries else None
    return entries


def _web_citation(row, stored):
    ref = _read_ref(row)
    url = turn_source_ledger.safe_http_url(row.get("url"))
    call = stored.get(ref.get("tool_call_id")) if ref else None
    if (url and call and call["full_sha256"] == ref.get("full_sha256")
            and tool_results.storage_integrity(call)["complete"]):
        return {"kind": "web", "url": url, "title": row["title"]}
    return None


def answer_citations(answer, *, session_id, request_id, user):
    """Project visible text and verified turn evidence in a single pass."""
    entries = _citation_entries(session_id, request_id)
    markers = []

    def redact(match):
        markers.extend(marker.upper() for marker in _MARKER.findall(match[1] or match[2]))
        return ""

    body, start = [], 0
    for literal in _LITERAL_MARKDOWN.finditer(answer):
        body.extend((_INLINE_REFERENCE.sub(redact, answer[start:literal.start()]), literal[0]))
        start = literal.end()
    body.append(_INLINE_REFERENCE.sub(redact, answer[start:]))
    cited, dropped = [], []
    stored = None
    seen = set()
    for marker in dict.fromkeys(markers):
        row = entries.get(marker)
        ref = _read_ref(row) if row else None
        if not ref:
            dropped.append(marker)
            continue
        if row["kind"] == "local":
            ref = turn_source_ledger.local_read_reference(ref)
            if not ref or not _local_evidence(ref, session_id, user):
                dropped.append(marker)
                continue
            item = {"kind": "local", "record_key": row["source_key"], "title": row["title"]}
        elif row["kind"] == "shared":
            ref = _visible_shared_reference(ref, user)
            if not ref or turn_source_ledger.reference_key("shared", ref) != row["source_key"]:
                dropped.append(marker)
                continue
            item = {"kind": "shared", "read_ref": ref, "title": row["title"]}
        elif row["kind"] == "web":
            if stored is None:
                stored = {call["tool_call_id"]: call for call in tool_results.fetch_by_request(session_id, request_id)}
            item = _web_citation(row, stored)
            if not item:
                dropped.append(marker)
                continue
        else:
            dropped.append(marker)
            continue
        key = (row["kind"], row["source_key"])
        if key not in seen:
            item["ref_id"] = marker
            if reader_id := citation_bubble_id(item):
                item["reader_id"] = reader_id
            cited.append(item)
            seen.add(key)
    return "".join(body), {"display": "bubble_only", "bubbles": cited, "dropped": dropped}


def visible_citation_bubbles(bubbles, user, *, session_id, request_id):
    """Recheck saved evidence for a turn in the caller's owned session."""
    entries = stored = None
    visible = []
    for item in bubbles:
        kind = item.get("kind")
        if kind == "local" and read_local_citation(item.get("record_key", ""), user):
            visible.append(item)
        elif kind == "shared" and (ref := _visible_shared_reference(item.get("read_ref"), user)):
            visible.append({**item, "reader_id": citation_bubble_id({**item, "read_ref": ref})})
        elif kind == "web" and request_id:
            if entries is None:
                entries = _citation_entries(session_id, request_id)
                stored = {call["tool_call_id"]: call for call in tool_results.fetch_by_request(session_id, request_id)}
            row = entries.get(item.get("ref_id"))
            source = _web_citation(row, stored) if row and row["kind"] == "web" else None
            if source and source["url"] == item.get("url"):
                visible.append({**source, "ref_id": item["ref_id"]})
    return visible
