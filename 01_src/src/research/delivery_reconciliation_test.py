"""F-09: publication evidence is per file, and unknown never becomes success."""
from __future__ import annotations

import pytest

from src.research.delivery_reconciliation import (PublicationReceipt, capture_delivery, delivery_outcome,
    publication_bound, publication_committed, publication_confirmed, publication_started,
    reconciliation_plan, resume_publication)


ONE = "/api/v2/session/s/artifacts/" + "1" * 32
TWO = "/api/v2/session/s/artifacts/" + "2" * 32


def test_each_file_carries_its_own_stages_and_hash():
    receipt = PublicationReceipt(file_index=2, filename="b.xlsx")
    assert (receipt.status, receipt.next_stage) == ("not_started", "started")
    receipt.started = True
    assert receipt.next_stage == "committed"
    receipt.committed, receipt.sha256 = True, "c" * 64
    assert receipt.next_stage == "confirmed"
    receipt.confirmed, receipt.url = True, TWO
    assert (receipt.status, receipt.next_stage) == ("confirmed", None)
    assert receipt.as_dict()["sha256"] == "c" * 64


def test_one_file_succeeds_and_one_file_fails():
    with capture_delivery("audit_uploaded_data", "call-one") as evidence:
        first = publication_started(filename="review.xlsx", file_index=1)
        publication_committed(sha256="a" * 64, receipt=first)
        publication_confirmed(ONE, sha256="a" * 64)
        second = publication_started(filename="summary.xlsx", file_index=2)
        publication_committed(sha256="b" * 64, receipt=second)
        outcome = delivery_outcome("complete", "", [{"url": ONE}])
        assert evidence.receipts == [first, second]
    assert outcome["status"] == "unknown" and outcome["reason_code"] == "write_requires_reconciliation"
    assert [item["status"] for item in outcome["files"]] == ["confirmed", "committed"]
    # The file that really published is not erased by the unknown sibling.
    assert outcome["artifacts"] == [ONE]
    assert outcome["confirmed_files"][0]["sha256"] == "a" * 64
    assert outcome["resume_stages"] == [{"file_index": 2, "filename": "summary.xlsx", "stage": "confirmed",
                                         "url": "", "sha256": "b" * 64}]


def test_a_started_write_without_confirmation_is_never_success():
    for status in ("complete", "partial", "failed"):
        with capture_delivery("collect_external_skill", "call-" + status):
            publication_started(filename="review.xlsx")
            outcome = delivery_outcome(status, "external_skill_report", [])
        assert outcome["status"] == "unknown" and outcome["reason_code"] == "write_requires_reconciliation"


def test_confirmed_publication_keeps_the_reported_status():
    with capture_delivery("collect_external_skill", "confirmed"):
        publication_started(filename="review.xlsx")
        publication_confirmed(ONE)
        partial = delivery_outcome("partial", "external_skill_report", [{"url": ONE}])
        complete = delivery_outcome("complete", "external_skill_report", [{"url": ONE}])
    assert partial["status"] == "partial" and complete["status"] == "complete"
    assert [item["status"] for item in complete["files"]] == ["confirmed"]
    assert complete["resume_stages"] == []


def test_a_confirmed_url_outside_the_reported_artifacts_is_still_unknown():
    with capture_delivery("collect_external_skill", "extra"):
        publication_started(filename="one.xlsx")
        publication_confirmed(ONE)
        publication_started(filename="two.xlsx")
        publication_confirmed(TWO)
        outcome = delivery_outcome("complete", "", [{"url": ONE}])
    assert outcome["status"] == "unknown" and len(outcome["files"]) == 2


def test_a_failed_preflight_without_any_write_stays_not_run():
    with capture_delivery("audit_uploaded_data", "render-rejected") as evidence:
        outcome = delivery_outcome("failed", "render_failed", [])
    assert outcome["status"] == "not_run" and evidence.preflight
    # Nothing was written, so there is no per-file receipt to reconcile.
    assert "files" not in outcome and "resume_stages" not in outcome


def test_publisher_binding_attaches_identity_to_the_registry_receipt():
    with capture_delivery("collect_external_skill", "bound"):
        publication_started()  # the artifact registry announced the write
        publication_confirmed(ONE)
        bound = publication_bound(ONE, sha256="d" * 64, filename=" review.xlsx ", file_index=3)
        assert bound.sha256 == "d" * 64 and bound.file_index == 3
        assert bound.filename == " review.xlsx " and bound.status == "confirmed"
        assert len(_receipts()) == 1
    with capture_delivery("collect_external_skill", "unhooked") as evidence:
        # No registry hook ran in this scope; the publisher still attests all
        # three stages for the exact bytes it registered.
        publication_bound(TWO, sha256="e" * 64, filename="summary.xlsx", file_index=1)
        assert evidence.receipts[0].status == "confirmed"
        assert evidence.receipts[0].url == TWO and evidence.receipts[0].sha256 == "e" * 64
        assert delivery_outcome("partial", "", [{"url": TWO}])["status"] == "partial"


def _receipts():
    from src.research.delivery_reconciliation import _current

    return _current.get().receipts


def test_reconciliation_resumes_only_the_unconfirmed_stage():
    with capture_delivery("audit_uploaded_data", "plan"):
        first = publication_started(filename="review.xlsx", file_index=1)
        publication_committed(sha256="a" * 64, receipt=first)
        publication_confirmed(ONE, sha256="a" * 64)
        second = publication_started(filename="plot.png", file_index=2)
        publication_committed(sha256="b" * 64, receipt=second)
        outcome = delivery_outcome("complete", "", [{"url": ONE}])
    plan = reconciliation_plan(outcome)
    assert plan["rerun_review"] is False and plan["rerun_plot"] is False
    assert [item["filename"] for item in plan["confirmed"]] == ["review.xlsx"]
    assert plan["resume"] == [{"file_index": 2, "filename": "plot.png", "stage": "confirmed",
                               "url": "", "sha256": "b" * 64}]
    assert reconciliation_plan({}) == {"status": None, "reason_code": None, "confirmed": [], "resume": [],
                                       "rerun_review": False, "rerun_plot": False}


def test_stage_helpers_never_infer_a_later_stage():
    with capture_delivery("collect_external_skill", "no-inference"):
        receipt = publication_committed(sha256="f" * 64, filename="only.xlsx")
        assert receipt.status == "committed" and not receipt.confirmed and not receipt.url
        assert publication_confirmed(ONE).status == "confirmed"
        assert delivery_outcome("complete", "", [{"url": ONE}])["status"] == "complete"


@pytest.mark.parametrize(("started", "committed", "confirmed", "expected"), [
    (False, False, False, "not_started"), (True, False, False, "started"),
    (True, True, False, "committed"), (True, True, True, "confirmed")])
def test_receipt_status_is_the_furthest_true_stage(started, committed, confirmed, expected):
    assert PublicationReceipt(1, "x", started, committed, confirmed).status == expected


def _two_file_plan():
    with capture_delivery("audit_uploaded_data", "resume"):
        first = publication_started(filename="review.xlsx", file_index=1)
        publication_committed(sha256="a" * 64, receipt=first)
        publication_confirmed(ONE, sha256="a" * 64)
        second = publication_started(filename="plot.png", file_index=2)
        publication_committed(sha256="b" * 64, receipt=second)
        return reconciliation_plan(delivery_outcome("complete", "", [{"url": ONE}]))


def test_resume_never_runs_without_an_explicit_authorization():
    plan = _two_file_plan()
    with pytest.raises(PermissionError, match="publication_resume_requires_authorization"):
        resume_publication(plan, republish=lambda item: TWO)
    with pytest.raises(PermissionError, match="publication_resume_requires_authorization"):
        resume_publication(plan, republish=lambda item: TWO, authorized=False)


def test_resume_republishes_only_the_unconfirmed_file():
    plan = _two_file_plan()
    calls = []

    def republish(item):
        calls.append(item)
        return TWO

    result = resume_publication(plan, republish=republish, authorized=True)
    assert [item["filename"] for item in calls] == ["plot.png"]
    assert calls[0]["stage"] == "confirmed" and calls[0]["sha256"] == "b" * 64
    assert [item["filename"] for item in result["reused"]] == ["review.xlsx"]
    assert result["reused"][0]["url"] == ONE and result["reused"][0]["reused"] is True
    assert result["resumed"] == [{"file_index": 2, "filename": "plot.png", "stage": "confirmed",
                                  "sha256": "b" * 64, "url": TWO, "status": "confirmed"}]
    assert result["status"] == "complete" and result["complete"] is True
    assert result["rerun_review"] is False and result["rerun_plot"] is False


@pytest.mark.parametrize("outcome", ["raise", "empty"])
def test_a_file_that_cannot_be_reconfirmed_never_becomes_success(outcome):
    plan = _two_file_plan()

    def republish(_item):
        if outcome == "raise":
            raise PermissionError("source moved")
        return ""

    result = resume_publication(plan, republish=republish, authorized=True)
    assert result["status"] == "unknown" and result["complete"] is False
    assert result["resumed"] == [] and [item["filename"] for item in result["unconfirmed"]] == ["plot.png"]
    if outcome == "raise":
        assert result["unconfirmed"][0]["error"] == "PermissionError"


def test_resume_of_a_fully_confirmed_plan_reuses_every_file():
    with capture_delivery("collect_external_skill", "all-confirmed"):
        receipt = publication_started(filename="review.xlsx", file_index=1)
        publication_confirmed(ONE, sha256="a" * 64)
        plan = reconciliation_plan(delivery_outcome("complete", "", [{"url": ONE}]))
    result = resume_publication(plan, republish=lambda item: pytest.fail("republished a confirmed file"),
                                authorized=True)
    assert [item["filename"] for item in result["reused"]] == ["review.xlsx"]
    assert result["complete"] is True and receipt.confirmed
