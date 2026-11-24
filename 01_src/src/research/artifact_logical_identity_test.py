"""One logical figure target keeps one artifact version chain.

A re-render in the same turn may rename the output file because the model
chooses ``output_name``. Artifact identity must follow the logical target, not
the file name, while distinct targets and distinct formats stay separate.
"""
from __future__ import annotations

import base64
import sqlite3
from contextlib import nullcontext

from src.research import tools
from src.research.script_execution import (_figure_publication_id,
    _singleton_figure_goal)

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
       b'<rect width="20" height="20" fill="#fff"/></svg>')
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _isolate(tmp_path, monkeypatch, request_id="req-logical"):
    from src.core import tracing

    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(tracing, "get_user_id", lambda: 7)
    monkeypatch.setattr(tools, "get_user_id", lambda: 7)
    monkeypatch.setattr(tools, "get_session_id", lambda: "session-logical-fixture")
    monkeypatch.setattr(tracing, "get_session_id", lambda: "session-logical-fixture")
    monkeypatch.setattr(tracing, "get_client_request_id", lambda: request_id)
    monkeypatch.setattr(tracing, "get_current_tool_name",
                        lambda: "run_research_figure_script")
    monkeypatch.setattr("src.core.account_leases.account_lease",
                        lambda _owner: nullcontext())
    monkeypatch.setattr("src.core.publication_reconcile.enqueue_publication",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("src.core.publication_reconcile.register_publication",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("src.research.delivery_reconciliation.publication_started",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("src.research.delivery_reconciliation.publication_confirmed",
                        lambda *args, **kwargs: None)


def _rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "state" / "research_artifacts.sqlite3")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(
            "SELECT artifact_id, filename, logical_artifact_id, version, "
            "supersedes_artifact_id FROM artifacts ORDER BY version")]
    finally:
        conn.close()


def _id(url):
    return url.rsplit("/", 1)[-1]


def test_renamed_rerender_stays_in_one_version_chain(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    first = tools._publish_artifact("first.svg", SVG, "image/svg+xml",
                                    logical_task_id="figure:abc:svg")
    second = tools._publish_artifact("renamed.svg", SVG + b"<!--v2-->",
                                     "image/svg+xml", logical_task_id="figure:abc:svg")
    rows = _rows(tmp_path)
    assert [row["filename"] for row in rows] == ["first.svg", "renamed.svg"]
    assert {row["logical_artifact_id"] for row in rows} == {_id(first)}
    assert [row["version"] for row in rows] == [1, 2]
    assert rows[1]["supersedes_artifact_id"] == _id(first)
    assert rows[1]["artifact_id"] == _id(second)


def test_distinct_targets_and_formats_stay_separate(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tools._publish_artifact("a.svg", SVG, "image/svg+xml",
                            logical_task_id="figure:one:svg")
    tools._publish_artifact("b.svg", SVG, "image/svg+xml",
                            logical_task_id="figure:two:svg")
    tools._publish_artifact("a.png", PNG, "image/png",
                            logical_task_id="figure:one:png")
    rows = _rows(tmp_path)
    assert len(rows) == 3
    assert len({row["logical_artifact_id"] for row in rows}) == 3
    assert [row["version"] for row in rows] == [1, 1, 1]


def test_without_logical_id_keeps_filename_identity(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    tools._publish_artifact("keep.svg", SVG, "image/svg+xml")
    tools._publish_artifact("other.svg", SVG, "image/svg+xml")
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert len({row["logical_artifact_id"] for row in rows}) == 2


def test_same_proven_identity_merges_real_renamed_titles(tmp_path, monkeypatch):
    """The 8144 follow-up really used both titles for one 220 d boxplot.

    When the host supplies a proven logical figure identity, a title/name
    rewrite must stay in one version chain rather than create two active
    deliverables.  The two filenames below are copied from the actual smoke
    registry rows, not invented labels.
    """
    _isolate(tmp_path, monkeypatch, request_id="5de3f984a45d499aa6636179cf473b5c")
    proven = "figure:5de3f984-model-goal:svg"
    first = tools._publish_artifact(
        "tss_220d_box_tss_220d_box.svg", SVG, "image/svg+xml",
        logical_task_id=proven)
    second = tools._publish_artifact(
        "tss220_box_box220.svg", SVG + b"<!--retitled-->", "image/svg+xml",
        logical_task_id=proven)
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert {row["logical_artifact_id"] for row in rows} == {_id(first)}
    assert [row["version"] for row in rows] == [1, 2]
    assert rows[1]["supersedes_artifact_id"] == _id(first)
    assert rows[1]["artifact_id"] == _id(second)


def test_without_proven_identity_real_renamed_titles_report_gap(tmp_path, monkeypatch):
    """Residual gap: title/name alone cannot prove two renders are one goal.

    This reproduces the two actual 8144 filenames with no host-supplied
    logical identity.  They remain separate by filename.  That is deliberate:
    guessing from title similarity could merge two genuinely separate figures.
    The caller must supply an existing proven identity to close this gap.
    """
    _isolate(tmp_path, monkeypatch, request_id="5de3f984a45d499aa6636179cf473b5c")
    tools._publish_artifact("tss_220d_box_tss_220d_box.svg", SVG,
                            "image/svg+xml")
    tools._publish_artifact("tss220_box_box220.svg", SVG + b"<!--retitled-->",
                            "image/svg+xml")
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert len({row["logical_artifact_id"] for row in rows}) == 2
    assert [row["version"] for row in rows] == [1, 1]


def test_same_logical_id_never_merges_different_formats(tmp_path, monkeypatch):
    """A caller mistake cannot chain SVG and PNG into one version history."""
    _isolate(tmp_path, monkeypatch)
    tools._publish_artifact("figure.svg", SVG, "image/svg+xml",
                            logical_task_id="figure:same:svg")
    tools._publish_artifact("figure.png", PNG, "image/png",
                            logical_task_id="figure:same:svg")
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert len({row["logical_artifact_id"] for row in rows}) == 2
    assert [row["version"] for row in rows] == [1, 1]



def test_actual_single_figure_request_has_explicit_goal_evidence():
    goal = _singleton_figure_goal(
        query="可以，按220d画箱线图",
        has_tables=True, request_id="5de3f984a45d499aa6636179cf473b5c",
        execution_id="run-one")
    assert goal.startswith("5de3f984a45d499aa6636179cf473b5c|")


def test_multi_figure_request_never_grants_singleton_goal():
    for query in (
        "请分别画220d和184d的箱线图",
        "给我画箱线图和柱状图",
        "先画220d箱线图再画184d箱线图",
        "two separate figures",
    ):
        assert _singleton_figure_goal(query=query, has_tables=True,
            request_id="request-one", execution_id="run-one") == ""


def test_two_sequential_singleton_contracts_for_multi_figure_request_stay_separate():
    one = {"figures": [{"name": "left", "parents": ["table_a"], "formats": ["svg"]}]}
    two = {"figures": [{"name": "right", "parents": ["table_b"], "formats": ["svg"]}]}
    left = _figure_publication_id(
        contract=one, figure=one["figures"][0], execution_id="run-left",
        fmt="svg", logical_task_id="same-proven-request:operation-left",
        request_id="same-request", request_goal_id="")
    right = _figure_publication_id(
        contract=two, figure=two["figures"][0], execution_id="run-right",
        fmt="svg", logical_task_id="same-proven-request:operation-right",
        request_id="same-request", request_goal_id="")
    assert left != right
    assert left.startswith("figure:slot-")
    assert right.startswith("figure:slot-")


def test_explicit_single_figure_goal_merges_real_rewrite_but_keeps_formats_apart():
    first_contract = {"figures": [{"name": "tss_220d_box", "parents": ["tss220"],
                                   "formats": ["svg"]}]}
    second_contract = {"figures": [{"name": "box220", "parents": ["t220"],
                                    "formats": ["svg"]}]}
    goal = "5de3f984|220d boxplot"
    first = _figure_publication_id(
        contract=first_contract, figure=first_contract["figures"][0],
        execution_id="run-one", fmt="svg",
        logical_task_id="ca295c3c-proven:b8c9-operation", request_goal_id=goal)
    second = _figure_publication_id(
        contract=second_contract, figure=second_contract["figures"][0],
        execution_id="run-two", fmt="svg",
        logical_task_id="ca295c3c-proven:b636-operation", request_goal_id=goal)
    pdf = _figure_publication_id(
        contract=second_contract, figure=second_contract["figures"][0],
        execution_id="run-two", fmt="pdf",
        logical_task_id="ca295c3c-proven:b636-operation", request_goal_id=goal)
    assert first == second
    assert first != pdf


def test_empty_request_id_falls_back_to_distinct_execution_ids():
    contract = {"figures": [{"name": "single", "parents": ["table"],
                             "formats": ["svg"]}]}
    one = _figure_publication_id(
        contract=contract, figure=contract["figures"][0],
        execution_id="execution-one", fmt="svg")
    two = _figure_publication_id(
        contract=contract, figure=contract["figures"][0],
        execution_id="execution-two", fmt="svg")
    assert one != two
