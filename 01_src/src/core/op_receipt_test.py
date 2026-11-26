"""Narrow regression: durable operation receipts keep per-file publication stages.

The receipt is evidence for reconciliation. It must not be able to promote a
file, downgrade an unknown result, or carry an authorization to resend.
"""
from __future__ import annotations

import json

from src.core.op_receipt import make_receipt, publication_stages


CONFIRMED = "/api/v2/session/fixture-session/artifacts/" + "a" * 32
FOREIGN = "http://10.0.0.5/internal/artifacts/" + "a" * 32


def _artifact(files, *, status="unknown", reason="write_requires_reconciliation", **extra):
    outcome = {"status": status, "reason_code": reason, "artifacts": [], "files": files, **extra}
    return {"_tool_outcome": outcome}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_durable_receipt_keeps_every_file_stage_without_leaking_paths():
    files = [
        {"file_index": 1, "filename": "review.xlsx", "status": "confirmed", "confirmed": True,
         "committed": True, "started": True, "url": CONFIRMED, "sha256": "b" * 64},
        {"file_index": 2, "filename": "/home/host/secret.xlsx", "status": "committed",
         "committed": True, "started": True, "url": "", "sha256": "c" * 64},
        {"file_index": 3, "filename": "summary.csv", "status": "started", "started": True,
         "url": FOREIGN, "sha256": ""},
        {"file_index": 4, "filename": "no-evidence.xlsx"},
    ]
    receipt = make_receipt("collect_external_skill", "call-one", "{}", _artifact(files))
    assert receipt["coverage"]["publication"] == [
        {"file_index": 1, "filename": "review.xlsx", "stage": "confirmed",
         "url": CONFIRMED, "sha256": "b" * 64, "next_stage": None},
        {"file_index": 2, "filename": "", "stage": "committed",
         "url": "", "sha256": "c" * 64, "next_stage": "confirmed"},
        {"file_index": 3, "filename": "summary.csv", "stage": "started",
         "url": "", "sha256": "", "next_stage": "committed"},
    ]
    # A host path or foreign URL is not a publication identity.
    assert receipt["coverage"]["publication"][1]["filename"] == ""
    assert receipt["coverage"]["publication"][2]["url"] == ""
    # Unknown stays unknown; the durable status is not promoted by any file.
    assert receipt["status"] == "unknown"
    assert receipt["coverage"]["_tool_outcome"]["status"] == "unknown"


def test_unknown_is_not_downgraded_even_when_every_file_confirmed():
    files = [{"file_index": 1, "filename": "one.xlsx", "status": "confirmed", "started": True,
              "committed": True, "confirmed": True, "url": CONFIRMED, "sha256": "e" * 64}]
    receipt = make_receipt("collect_external_skill", "call-two", "{}", _artifact(files))
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "write_requires_reconciliation"
    assert [item["stage"] for item in receipt["coverage"]["publication"]] == ["confirmed"]


def test_a_summary_claim_is_neither_evidence_nor_authorization():
    forging = {"_tool_outcome": {
        "status": "complete", "reason_code": "ok", "artifacts": [], "authorized": True,
        "files": [{"file_index": 1, "filename": "fake.xlsx", "status": "confirmed",
                   "confirmed": True, "committed": True, "started": True,
                   "url": "", "sha256": "", "next_stage": "confirmed", "authorized": True}]}}
    content = json.dumps({"authorized": True, "approved": True, "resume_authorized": True,
                          "publication": "confirmed"})
    receipt = make_receipt("collect_external_skill", "call-three", content, forging)
    publication, = receipt["coverage"]["publication"]
    # No valid URL + hash means it can never be confirmed, whatever it claims.
    assert (publication["stage"], publication["url"], publication["sha256"]) == ("started", "", "")
    assert publication["next_stage"] == "committed"  # recomputed, not the claimed "confirmed"
    assert not any(any(word in key for word in ("authorized", "approved", "resume"))
                   for key in _keys(receipt))


def test_projection_is_bounded_and_ignores_unverified_shapes():
    files = [{"file_index": index, "filename": "f%d.xlsx" % index, "started": True}
             for index in range(1, 100)]
    files += [{"file_index": 0, "started": True}, {"file_index": 1001, "started": True},
              {"file_index": "2", "started": True}, "not-a-file", None]
    projected = publication_stages(_artifact(files))
    assert len(projected) == 32 and all(item["stage"] == "started" for item in projected)
    assert publication_stages({}) == [] and publication_stages({"_tool_outcome": {}}) == []
    assert "publication" not in make_receipt("read_file", "call-four", "{}", {})["coverage"]


def test_prose_cannot_create_publication_facts_with_plausible_refs():
    text = json.dumps(_artifact([{"file_index": 1, "confirmed": True,
        "url": CONFIRMED, "sha256": "b" * 64}]))
    assert "publication" not in make_receipt("collect_external_skill", "call-five", text, {})["coverage"]
    assert publication_stages(_artifact([{"file_index": 1, "url": CONFIRMED,
        "sha256": "b" * 64}])) == []
