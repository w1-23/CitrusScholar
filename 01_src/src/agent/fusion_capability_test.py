"""F-02: the Fusion tool surface switches atomically, never through a gap."""
from __future__ import annotations

from itertools import product

import pytest

from src.agent import fusion_tools


SKILLS = ("data-audit", "literature-verification")


def _release(tmp_path, *, installed=True):
    if not installed:
        return tmp_path / "missing-release"
    (tmp_path / "fusion").mkdir(parents=True, exist_ok=True)
    (tmp_path / "fusion" / "bundle.json").write_bytes(b'{"schema":"citrus.fusion-bundle/1"}')
    return tmp_path


def _rows(*ids):
    return [{"id": name, "version": "1.0.0"} for name in ids]


def _wired(allowed, state, names):
    """The exact host loop this module offers; host.py keeps the same order."""
    allowed = set(allowed)
    if fusion_tools.fusion_retires_legacy_tools(state):
        allowed.difference_update(fusion_tools.REPLACED_TOOLS)
    allowed.update(names)
    return allowed


def test_new_tools_only_open_when_release_worker_and_skill_are_all_ready(tmp_path):
    release = _release(tmp_path)
    rows = _rows(*fusion_tools.SKILL_IDS)
    state, names, reason = fusion_tools.fusion_capability_state(
        release=release, installed_rows=rows, worker_ready=True, allowed_skills=SKILLS,
        external_release=True, external_required=True)
    assert state == fusion_tools.CAPABILITY_NEW and reason == "external_skill_ready"
    assert names == fusion_tools.FUSION_TOOL_NAMES - {"resolve_external_report"}
    # A findings-list follow-up is the only reason to expose the read-only resolver.
    assert fusion_tools.fusion_capability_state(
        release=release, installed_rows=rows, worker_ready=True, allowed_skills=SKILLS,
        external_release=True, external_required=True, report_followup=True)[1] == fusion_tools.FUSION_TOOL_NAMES


@pytest.mark.parametrize(("worker_ready", "skills", "required", "expected_reason"), [
    (False, SKILLS, True, "code_worker_unavailable"),
    (False, SKILLS, False, "code_worker_unavailable"),
    (True, (), True, "skill_capability_unauthorized"),
    (True, (), False, "skill_capability_unauthorized"),
])
def test_every_incomplete_fact_reports_an_explicit_state(tmp_path, worker_ready, skills, required, expected_reason):
    state, names, reason = fusion_tools.fusion_capability_state(
        release=_release(tmp_path), installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=worker_ready,
        allowed_skills=skills, external_release=True, external_required=required)
    assert names == frozenset()
    assert reason == expected_reason
    assert state == (fusion_tools.CAPABILITY_UNAVAILABLE if required else fusion_tools.CAPABILITY_LEGACY)
    assert not fusion_tools.fusion_retires_legacy_tools(state)


def test_missing_release_is_derived_from_the_pinned_inventory(tmp_path):
    release = _release(tmp_path, installed=False)
    state, names, reason = fusion_tools.fusion_capability_state(
        release=release, installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=True,
        allowed_skills=SKILLS, external_required=False)
    assert (state, names, reason) == (fusion_tools.CAPABILITY_LEGACY, frozenset(), "fusion_release_not_installed")
    state, names, reason = fusion_tools.fusion_capability_state(
        release=release, installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=True,
        allowed_skills=SKILLS, external_required=True)
    assert (state, names, reason) == (fusion_tools.CAPABILITY_UNAVAILABLE, frozenset(),
                                      "fusion_required_without_release")


def test_required_without_any_capability_never_silently_drops_the_review_surface(tmp_path):
    state, names, reason = fusion_tools.fusion_capability_state(
        release=_release(tmp_path, installed=False), installed_rows=[], worker_ready=False, allowed_skills=(),
        external_release=False, external_required=True)
    assert (state, names) == (fusion_tools.CAPABILITY_UNAVAILABLE, frozenset())
    notice = fusion_tools.fusion_capability_notice(state, reason)
    # The caller has explicit text to surface; nothing claims the Skill works.
    assert reason in notice and "不可用" in notice and "旧分析器" in notice
    assert fusion_tools.fusion_capability_notice(fusion_tools.CAPABILITY_NEW, "external_skill_ready") == ""


@pytest.mark.parametrize(("release", "worker", "skills", "required"), list(product((True, False), repeat=4)))
def test_old_tools_are_retired_only_with_a_complete_replacement(tmp_path, release, worker, skills, required):
    ready = (tmp_path if release else tmp_path / "none")
    if release:
        ready = _release(tmp_path)
    state, names, _ = fusion_tools.fusion_capability_state(
        release=ready, installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=worker,
        allowed_skills=SKILLS if skills else (), external_release=release, external_required=required)
    allowed = _wired(fusion_tools.REPLACED_TOOLS | {"read_file"}, state, names)
    if state == fusion_tools.CAPABILITY_NEW:
        assert allowed == {"read_file", *names}
        assert not fusion_tools.REPLACED_TOOLS <= allowed
    else:
        # The previous analyzers stay reachable instead of vanishing with nothing
        # to replace them.
        assert fusion_tools.REPLACED_TOOLS <= allowed and not names
        assert allowed != set()


def test_tool_names_are_a_pure_projection_of_the_decided_state():
    assert fusion_tools.fusion_tool_names_for(fusion_tools.CAPABILITY_LEGACY) == frozenset()
    assert fusion_tools.fusion_tool_names_for(fusion_tools.CAPABILITY_UNAVAILABLE) == frozenset()
    assert fusion_tools.fusion_tool_names_for(fusion_tools.CAPABILITY_NEW) == (
        fusion_tools.FUSION_TOOL_NAMES - {"resolve_external_report"})
    assert "resolve_external_report" in fusion_tools.fusion_tool_names_for(
        fusion_tools.CAPABILITY_NEW, report_followup=True)


def test_decided_state_drives_the_real_catalog_names(tmp_path):
    # Only authorized Skill capabilities count toward readiness.
    authorized = [name for name, ok in (("data-audit", True),
                                        ("literature-verification", False)) if ok]
    state, names, _ = fusion_tools.fusion_capability_state(
        release=_release(tmp_path), installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=True,
        allowed_skills=authorized, external_release=True, external_required=True)
    assert authorized == ["data-audit"]
    assert state == fusion_tools.CAPABILITY_NEW and "run_external_skill" in names
    assert fusion_tools.CAPABILITY_NEW != fusion_tools.fusion_capability_state(
        release=_release(tmp_path), installed_rows=_rows(*fusion_tools.SKILL_IDS), worker_ready=True,
        allowed_skills=[], external_release=True, external_required=True)[0]
