"""Shared Markdown references carried by persisted retrieval receipts."""
import json
import re

from citrus_corpus.contract import ISOLATED

_TOOL_FIELDS = {"search_agent_corpus": "results", "local_hybrid_search": "candidates"}
_ID = re.compile(r"[a-zA-Z0-9_-]{1,96}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def source_rows(tool_name, text):
    field = _TOOL_FIELDS.get(tool_name)
    if not field:
        return []
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return []
    rows = payload.get(field) if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict) and row.get("source_family") in {"agent_literature", ISOLATED.family_id}] \
        if isinstance(rows, list) else []


def evidence_rows(tool_name, text):
    """Yield exact references for returned passages with actual stored excerpt text."""
    for row in source_rows(tool_name, text):
        excerpt = row.get("excerpt")
        if isinstance(excerpt, str) and excerpt.strip() and (ref := read_reference(row)):
            yield row, ref


def read_reference(row):
    ref = row.get("read_ref")
    if not isinstance(ref, dict):
        return None
    ids = ("document_id", "version_id", "index_generation")
    if any(not isinstance(ref.get(key), str) or not _ID.fullmatch(ref[key]) for key in ids):
        return None
    start, end, digest = ref.get("start"), ref.get("end"), ref.get("md_sha256")
    if (type(start) is not int or type(end) is not int or not 0 <= start < end
            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
        return None
    result = {key: ref[key] for key in (*ids, "start", "end", "md_sha256")}
    family = ref.get("source_family")
    if family == ISOLATED.family_id:
        chunk = ref.get("chunk_id")
        if not isinstance(chunk, str) or not re.fullmatch(r"[a-f0-9]{32}", chunk) or end - start > 12000:
            return None
        result.update(source_family=family, chunk_id=chunk)
    elif family not in (None, "agent_literature"):
        return None
    return result
