"""Metadata-only index over authorized, persisted evidence pages."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass

from src.core.tool_results import fetch_for_current_request, prior_readback_page, storage_integrity


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(parts: list) -> str:
    return _hash(json.dumps(parts, ensure_ascii=True, separators=(",", ":")))


@dataclass(frozen=True)
class EvidenceCacheRecord:
    result_ref: dict
    source_file_id: str
    source_sha256: str
    tool_name: str
    selector_hash: str
    page_cursor: str
    returned_fields: tuple[str, ...]
    content_sha256: str
    created_at: str


class ReadbackCache:
    """No cache authority of its own: the receipt store owns scope and TTL."""

    def __init__(self, row, source, fields, cursor, max_chars, *, complete):
        self.row, self.source, self.fields, self.cursor = row, source, fields, cursor
        self.selector_hash = _key(fields)
        self.page_key = _key(["readback-v2", source["tool_call_id"], source["full_sha256"],
                              row.get("evidence_ref"), self.selector_hash, cursor, max_chars])
        self.eligible = complete and row.get("status") == "complete" and not row.get("error_text")

    def lookup(self):
        if not self.eligible or not (prior := prior_readback_page(self.row, self.page_key)):
            return None
        try:
            stored = fetch_for_current_request(prior[0], strict=True)
            if (not stored or stored.get("tool_name") != "read_tool_result"
                    or stored.get("status") != "complete" or stored.get("error_text")
                    or stored.get("full_sha256") != prior[1] or not storage_integrity(stored)["complete"]):
                return None
            payload = json.loads(stored["result_text"])
            if (payload.get("tool_result_ref") != self.source or payload.get("selected_fields") != self.fields
                    or payload.get("missing_fields") != [] or not isinstance(payload.get("page"), dict)
                    or payload.get("execution_status") != "complete" or payload.get("status") != "ok"
                    or payload.get("readback_page_key") != self.page_key
                    or payload.get("_tool_outcome", {}).get("status") != "complete"):
                return None
            text = payload["result_text"]
            receipt = {"tool_call_id": stored["tool_call_id"], "full_sha256": prior[1]}
            # A generic receipt need not originate from an uploaded file.
            # Never treat a digest or filename printed in tool output as file authority.
            evidence = EvidenceCacheRecord(receipt, "", self.source["full_sha256"],
                self.row["tool_name"], self.selector_hash, self.cursor,
                tuple(self.fields) if payload["page"].get("complete") else (),
                _hash(text), stored["created_at"])
            return {"reused_readback": {**receipt, "original_page_bytes": len(text.encode("utf-8"))},
                    "page": payload["page"], "evidence_cache": asdict(evidence)}
        except (PermissionError, sqlite3.Error, KeyError, TypeError, ValueError, AttributeError, RecursionError):
            return None



def publish_audit_dataset(*, owner, session, source_file_id, source_sha256, execution_id,
                          table, fields=None):
    """Freeze an exact projection of a successful, server-verified audit table.

    The caller supplies a retained table envelope, not cells from a preview or
    finding. Only the immutable dataset reference and selector go to the model.
    """
    from datetime import datetime, timezone
    import re

    import pandas as pd
    from citrus_audit.envelope import TableEnvelope
    from src.figures.dataset_codec import MAX_CELLS, MAX_COLUMNS
    from src.research.analysis_results import persist_prepared_dataset
    from src.session.manager import session_manager

    if (type(owner) is not int or owner <= 0 or not session
            or session_manager.get_session_owner(session) != owner
            or not isinstance(source_file_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_file_id)
            or not isinstance(source_sha256, str) or table.get("source_sha256") != source_sha256):
        raise PermissionError("audit_dataset_source_unverified")
    source = TableEnvelope.from_dict(table)
    names = list(source.columns if fields is None else fields)
    if (source.coverage.get("truncated") or not source.values or not names or len(names) > MAX_COLUMNS
            or len(set(names)) != len(names) or not set(names).issubset(source.columns)
            or len(source.values) * len(names) > MAX_CELLS):
        raise ValueError("audit_dataset_projection_unavailable")
    indices = [source.columns.index(name) for name in names]
    frame = pd.DataFrame([[row[index] for index in indices] for row in source.values],
                         index=list(source.row_numbers), columns=names, dtype=object)
    selector = {"sheet": source.sheet, "region_id": source.region_id, "bounds": source.bounds,
                "header_rows": list(source.header_rows), "returned_fields": names}
    provenance = {"source_file_id": source_file_id, "source_sha256": source_sha256,
                  "tool_name": "audit_uploaded_data", **selector}
    snapshot = persist_prepared_dataset(owner, frame, session_id=session,
        parents=[{"kind": "input_snapshot", "file_id": source_file_id, "sha256": source_sha256}], execution_id=execution_id,
        script_sha256=_hash("verified-audit-table-projection/v1"), source=provenance)
    return {**provenance, "selector_hash": _key(list(selector.values())), "page_cursor": "",
            "content_sha256": snapshot.to_dict()["payload_sha256"],
            "created_at": datetime.now(timezone.utc).isoformat(), "result_ref": snapshot.ref,
            "dataset_ref": snapshot.ref, "inputs_binding": {"data": {"dataset_ref": snapshot.ref}}}
