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
    except (ValueError, OSError):
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
    from src.shared_corpus.runtime import current_runtime

    ledger = turn_source_ledger.fetch_rows(session_id, request_id)
    records = tool_results.fetch_by_request(session_id, request_id)
    stored = {row["tool_call_id"]: row for row in records}
    runtime = current_runtime()
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
                    and runtime is not None and runtime.reference_visible(ref)):
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


_ANSWER_REF = re.compile(r"\[(\d{1,2}|W\d{1,2}|H\d{1,2})\]", re.I)


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

    This does not invent a citation.  Local entries are listed as uncited when
    the answer contains no numeric marker; web entries are always exposed in
    ``web_separate`` so the source panel can show real URLs.  Markers that point
    past the retained source set are reported in ``dropped`` and are never
    turned into clickable evidence.
    """
    from src.core.evidence import clean_doi, src_of

    local, seen_local = [], set()
    for row in main_results if isinstance(main_results, list) else []:
        if not isinstance(row, dict):
            continue
        doi = clean_doi(row.get("doi"))
        key = (doi or str(row.get("record_key") or row.get("chunk_id") or
               row.get("title") or "").strip())
        if not key or key in seen_local:
            continue
        seen_local.add(key)
        ref_id = str(len(local) + 1)
        title = _reference_text(row.get("title") or row.get("paper_title"), 180) or "本地文献"
        local.append({
            "ref_id": ref_id, "type": "main", "source": src_of(row),
            "doi": doi or "N/A", "url": "", "title": title,
            "year": _reference_text(row.get("year"), 32),
            "score": _reference_score(row.get("score", row.get("rerank_score"))),
            "chunk_id": _reference_text(row.get("chunk_id"), 200),
            "text_preview": _reference_text(row.get("snippet") or row.get("text"), 250),
        })

    from src.core.source_tiers import classify_url

    tier_labels = {
        "T1": "同行评议 / 官方 / 学术数据库",
        "T2": "权威与行业媒体 / 机构新闻稿",
        "T3": "自媒体 / 论坛 / 来源不明",
    }
    web, seen_web = [], set()
    for row in web_results if isinstance(web_results, list) else []:
        if not isinstance(row, dict):
            continue
        url = turn_source_ledger.safe_http_url(row.get("url"))
        if not url or url in seen_web:
            continue
        seen_web.add(url)
        ref_id = f"W{len(web) + 1}"
        from urllib.parse import urlsplit
        host = urlsplit(url).hostname or ""
        tier = classify_url(url, str(row.get("doi") or ""))
        web.append({
            "ref_id": ref_id, "type": "web", "source": "web", "url": url,
            "title": _reference_text(row.get("title"), 180) or host or url,
            "host": host,
            "snippet": _reference_text(row.get("abstract") or row.get("snippet") or row.get("text"), 320),
            "round_no": 1, "is_summary": False,
            # Honest network labelling: every entry stays in the web group and
            # carries the deterministic tier classification.  Unknown hosts
            # are T3, never promoted to an authoritative source.
            "origin_label": "网络来源",
            "tier": tier.tier, "tier_rule": tier.rule,
            "tier_label": f"网络来源 · {tier.tier} {tier_labels[tier.tier]}",
            "role": "candidate", "cited": False,
        })

    numeric_markers, web_markers, dropped = set(), set(), []
    for marker in _ANSWER_REF.findall(str(answer or "")):
        token = marker.upper()
        if token.startswith("W") and token[1:].isdigit():
            number = int(token[1:])
            if 1 <= number <= len(web):
                web_markers.add(number)
            else:
                dropped.append(f"W{number}")
        elif token.startswith("H"):
            continue
        elif token.isdigit():
            number = int(token)
            if 1 <= number <= len(local):
                numeric_markers.add(number)
            else:
                dropped.append(str(number))

    # ``web_markers`` must actually decide the role: a web result is cited only
    # when the answer contains its exact ``[Wn]`` marker.  Everything else is a
    # candidate and must not render as a citation bubble.
    for item in web:
        number = int(item["ref_id"][1:])
        item["cited"] = number in web_markers
        item["role"] = "cited" if item["cited"] else "candidate"

    cited = [item for item in local if int(item["ref_id"]) in numeric_markers]
    uncited = [item for item in local if int(item["ref_id"]) not in numeric_markers]
    for item in cited:
        item["type"] = "main"
        item["role"] = "cited"
        item["cited"] = True
    for item in uncited:
        # Candidates keep a numeric ref_id for ordering but are never typed as
        # "main"; the front end uses this role to render a candidate badge and
        # to avoid claiming the answer cites them.
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
