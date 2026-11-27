from collections import Counter

import pytest

from src.core import cancel
from src.core.capability_policy import CapabilityPolicy, bind_policy
from src.research import literature
from src.research.reference_verification_service import SCHEMA, verify_references


@pytest.fixture
def registry(monkeypatch):
    calls = Counter()

    def get(url, *, params=None, source):
        assert literature._LOOKUP_CONTEXT.get() is not None
        name = url.rsplit("/", 1)[-1]
        calls[source, name] += 1
        if name == "timeout":
            return 0, None, "timeout"
        if name == "limited" and source == "crossref":
            return 429, None, "http_429"
        if name == "works" and source == "crossref":
            title = params["query.bibliographic"].split(" Alice", 1)[0]
            item = {"DOI": "10.5555/title", "title": [title], "author": [{"given": "Alice", "family": "Smith"}]}
            if title != "Missing year":
                item["issued"] = {"date-parts": [[2025]]}
            return 200, {"message": {"items": [item]}}, ""
        if source == "epmc":
            return 200, {"resultList": {"result": [{"pmid": "101", "title": "Registry title"}]}}, ""
        if source == "crossref" and name in {"known", "retracted"}:
            item = {"DOI": "10.5555/" + name, "title": ["The registered title"], "volume": "3"}
            if name == "retracted":
                item["updated-by"] = [{"type": "retraction"}]
            return 200, {"message": item}, ""
        return 404, None, ""

    monkeypatch.setattr(literature, "_get", get)
    return calls


def test_one_call_checks_duplicate_doi_claims_separately(registry):
    result = verify_references([
        {"doi": "https://doi.org/10.5555/known"},
        {"doi": "DOI: 10.5555/known", "title": "Unrelated withdrawn paper", "volume": 3, "issue": 5},
        {"doi": "10.5555/retracted"},
    ])
    assert result["schema_version"] == SCHEMA
    assert result["status"] == "complete"
    assert result["coverage"] == {"requested": 3, "actual": 3, "gap": 0, "processed": 3}
    found, conflict, retracted = result["records"]
    assert [row["status"] for row in (found, conflict, retracted)] == ["verified", "metadata_conflict", "retracted"]
    assert found["retraction_status"] == "not_established"
    assert conflict["field_checks"]["title"]["status"] == "conflicting"
    assert conflict["field_checks"]["volume"]["status"] == "matched"
    assert conflict["field_checks"]["issue"]["status"] == "missing"
    assert "issue" not in conflict["fields_checked"]
    assert registry["crossref", "known"] == 1


def test_title_candidate_requires_registered_author_and_year(registry):
    result = verify_references([
        {"title": "Only title"},
        {"title": "Missing year", "authors": ["Alice Smith"], "year": 2025},
        {"title": "Corroborated title", "authors": ["Alice Smith"], "year": 2025},
    ])
    first, second, third = result["records"]
    assert [row["status"] for row in (first, second, third)] == ["unconfirmed", "unconfirmed", "verified"]
    assert second["reason"] == "title_metadata_not_corroborated"
    assert second["field_checks"]["year"]["status"] == "missing"
    assert result["coverage"]["actual"] == 3


def test_outage_and_rate_limit_are_not_not_found(registry):
    result = verify_references([{"doi": "10.5555/missing"}, {"doi": "10.5555/limited"}])
    missing, limited = result["records"]
    assert missing["status"] == "not_found"
    assert limited["status"] == "source_unavailable"
    assert limited["reason"] == "rate_limited"
    assert result["status"] == "partial"
    assert result["coverage"] == {"requested": 2, "actual": 1, "gap": 1, "processed": 2}
    result = verify_references([{"doi": "10.5555/timeout"}])
    assert result["records"][0]["reason"] == "timeout"
    assert result["coverage"]["actual"] == 0


@pytest.mark.parametrize("policy", [CapabilityPolicy(bibliographic_lookup=False), CapabilityPolicy(enabled_skills=())])
def test_revoked_access_never_dispatches(registry, policy):
    with bind_policy(policy):
        result = verify_references([{"doi": "10.5555/known"}])
    assert not registry
    assert result["status"] == "not_run" and result["reason"] == "network_disabled"
    assert result["records"][0]["status"] == "not_processed"


def test_cancelled_and_malformed_inputs_do_not_dispatch(registry):
    token = cancel.CancelToken()
    token.cancel()
    with cancel.bound_token(token):
        result = verify_references([{"doi": "10.5555/known"}])
    assert result["status"] == "cancelled"
    result = verify_references([{"doi": "not a DOI"}])
    assert result["records"][0]["status"] == "not_processed"
    with pytest.raises(ValueError, match="batch_size"):
        verify_references([{"doi": "10.5555/known"}] * 11)
    with pytest.raises(ValueError, match="fields_invalid"):
        verify_references([{"doi": "10.5555/known", "url": "https://example.org"}])
    assert not registry


def test_identifier_field_is_backed_by_its_registry(registry):
    result = verify_references([{"pmid": 101}, {"pmid": 102}])
    matched, mismatched = result["records"]
    assert matched["field_checks"]["pmid"]["status"] == "matched"
    assert matched["fields_checked"] == ["pmid"]
    assert mismatched["field_checks"]["pmid"]["status"] == "not_checked"
    assert mismatched["status"] == "unconfirmed"


def test_dispatch_deadline_preserves_unprocessed_gap(monkeypatch, registry):
    from citrus_refcheck.lookup import AuditStopped

    def expired(*_args):
        raise AuditStopped("deadline")

    monkeypatch.setattr(literature, "_crossref_work", expired)
    result = verify_references([{"doi": "10.5555/known"}])
    assert not registry
    assert result["status"] == "partial" and result["reason"] == "deadline"
    assert result["coverage"] == {"requested": 1, "actual": 0, "gap": 1, "processed": 0}
    assert result["records"][0]["status"] == "not_processed"
