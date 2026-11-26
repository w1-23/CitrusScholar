"""One declarative ToolSpec projects into every consumer without drift.

This file used to compare two hand-written tool tables (``preset_catalog._PRESETS``
against ``legacy_registry._TOOL_DECLARATIONS``). Both copies are gone: the schema
manifest, the capability/label catalog, the readiness bindings, the scheduling
metadata, the compatibility registry and the packaged JSON all project
``src.tools.tool_specs.TOOL_DECLARATIONS``. These tests keep it that way:

* one row reaches every consumer, and adding a tool is one row;
* the projections still equal the pre-migration hand-written values, so
  collapsing the tables cannot silently change a label, capability, requirement,
  write scope, concurrency key, recovery mode or import target;
* describing, scheduling and authorizing a tool never imports its implementation.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
from importlib import import_module
from pathlib import Path

import pytest

from src.agent import preset_catalog
from src.agent.preset_catalog import (
    _PRESETS,
    _TARGETS,
    build_preset_catalog,
    canonical_tool_schema,
    generate_tool_manifest,
    preset_bindings,
    preset_metadata,
)
from src.core import tool_catalog
from src.tools import legacy_registry, tool_specs
from src.tools.tool_specs import TOOL_DECLARATIONS, ToolSpec

MANIFEST_PATH = Path(__file__).parent / "preset_tool_schemas.json"


def test_manifest_matches_canonical_tool_schemas():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected = {tool["function"]["name"]: tool for tool in manifest["tools"]}
    assert set(expected) == {preset.name for preset in _PRESETS}
    for preset in _PRESETS:
        tool = getattr(import_module(_TARGETS[preset.name]), preset.name)
        assert canonical_tool_schema(tool) == expected[preset.name], preset.name


def test_manifest_matches_the_generator_that_writes_it():
    """--check and the loader compare the same projection of the same rows."""
    installed = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert generate_tool_manifest() == installed


def test_one_row_reaches_every_consumer():
    declared = [spec.name for spec in TOOL_DECLARATIONS]
    assert declared == [preset.name for preset in _PRESETS]
    assert set(_TARGETS) == set(declared)
    assert set(tool_catalog.TOOL_METADATA) == set(declared)
    assert set(preset_bindings()) == set(declared)
    assert set(preset_metadata()) == set(declared)
    assert set(legacy_registry.tool_bindings()) == set(declared)
    assert legacy_registry.get_tool_names() == declared
    catalog = build_preset_catalog()
    assert {row["name"] for row in catalog.describe()} == set(declared)
    assert {tool["function"]["name"] for tool in
            json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["tools"]} == set(declared)

    # Every consumer reads the same values, not merely the same names.
    bindings = preset_bindings()
    metadata = preset_metadata()
    legacy = legacy_registry.tool_bindings()
    descriptions = {row["name"]: row for row in catalog.describe()}
    for spec in TOOL_DECLARATIONS:
        policy = (spec.requires, spec.produces, spec.authorization)
        assert bindings[spec.name] == policy
        assert legacy[spec.name] == policy
        assert metadata[spec.name]["category"] == spec.category
        assert metadata[spec.name]["concurrency_key"] == spec.concurrency_key
        assert metadata[spec.name]["write_scope"] == spec.write_scope
        assert metadata[spec.name]["is_readonly"] is spec.is_readonly
        assert metadata[spec.name]["recovery_mode"] == spec.recovery_mode
        assert metadata[spec.name]["execution_mode"] == spec.execution_mode
        assert metadata[spec.name]["display_label"] == spec.display_label
        assert metadata[spec.name]["link_label"] == spec.link_label
        assert tuple(metadata[spec.name]["capabilities"]) == spec.capabilities
        assert descriptions[spec.name]["provider"] == spec.import_target
        assert descriptions[spec.name]["mutates"] is spec.mutates
        assert descriptions[spec.name]["execution_mode"] == spec.execution_mode


def test_presentation_catalog_matches_the_pre_migration_snapshot():
    """The capability/label catalog is derived now; the values must not move."""
    derived = tool_catalog.TOOL_METADATA
    assert set(derived) == set(_MIGRATED)
    for name, row in _MIGRATED.items():
        (label, link, capabilities, requires, produces, write_scope,
         concurrency_key, category, authorization, recovery_mode, import_target) = row
        metadata = derived[name]
        assert (metadata.display_label, metadata.link_label) == (label, link), name
        assert tuple(sorted(metadata.capabilities)) == tuple(sorted(capabilities)), name
        spec = tool_specs.spec_by_name(name)
        assert spec is not None, name
        assert spec.requires == requires, name
        assert spec.produces == produces, name
        assert spec.write_scope == write_scope, name
        assert spec.concurrency_key == concurrency_key, name
        assert spec.category == category, name
        assert spec.authorization == authorization, name
        assert spec.recovery_mode == recovery_mode, name
        assert spec.import_target == import_target, name
    # The routing groups are derived from the same records, so they cannot move
    # independently either. Spot-check each retrieval family.
    groups = tool_catalog.capability_groups()
    assert groups["network"] == frozenset(
        name for name, row in _MIGRATED.items() if "network" in row[2])
    assert groups["visualization"] == frozenset(
        name for name, row in _MIGRATED.items() if "visualization" in row[2])
    assert groups["shared_corpus"] == frozenset(
        name for name, row in _MIGRATED.items() if "shared_corpus" in row[2])
    assert groups["web"] == groups["network"] and groups["local"] == groups["local_corpus"]
    assert "audit_uploaded_data" in groups["artifact"]
    assert "run_research_figure_script" in groups["visualization"]


def test_declarations_reuse_the_known_vocabularies():
    """A typo in requires/produces/authorization must fail here, not silently."""
    from src.graph.tool_availability import (
        KNOWN_AUTHORIZATIONS,
        KNOWN_PRODUCES,
        KNOWN_REQUIRES,
    )
    for spec in TOOL_DECLARATIONS:
        assert set(spec.requires) <= KNOWN_REQUIRES, spec.name
        assert set(spec.produces) <= KNOWN_PRODUCES, spec.name
        assert spec.authorization in KNOWN_AUTHORIZATIONS | {""}, spec.name
        assert set(spec.capabilities) <= tool_specs.CAPABILITIES, spec.name


def test_adding_one_row_is_enough(monkeypatch):
    """A new tool is describable, generatable and loadable from one row."""
    module = _fixture_module()
    monkeypatch.setitem(sys.modules, "toolspec_fixture_tools", module)
    fixture = ToolSpec(
        "toolspec_fixture_tool", "toolspec_fixture_tools", "research",
        "toolspec_fixture", write_scope="session_artifact",
        requires=("session",), produces=("report_json",),
        validator="validate_toolspec_fixture",
        display_label="夹具工具", link_label="下载夹具",
        capabilities=("research", "artifact"))
    rows = (*TOOL_DECLARATIONS, fixture)
    # A packaged manifest is generated from the rows; simulate the regenerated
    # package instead of hand-editing JSON.
    manifest = generate_tool_manifest(rows)
    monkeypatch.setattr(preset_catalog, "_INSTALLED_MANIFEST",
                        json.dumps(manifest, ensure_ascii=False))

    assert preset_bindings(rows)["toolspec_fixture_tool"] == (
        ("session",), ("report_json",), "")
    metadata = preset_metadata(rows)["toolspec_fixture_tool"]
    assert metadata["display_label"] == "夹具工具"
    assert metadata["execution_mode"] == "sequential"
    assert metadata["is_readonly"] is False
    presentation = tool_catalog.metadata_catalog(rows)["toolspec_fixture_tool"]
    assert presentation.capabilities == frozenset({"research", "artifact"})
    assert legacy_registry.tool_bindings(rows)["toolspec_fixture_tool"] == (
        ("session",), ("report_json",), "")
    generated = {tool["function"]["name"]: tool for tool in manifest["tools"]}
    assert generated["toolspec_fixture_tool"]["function"]["description"]

    catalog = build_preset_catalog(rows)
    assert "toolspec_fixture_tool" in {row["name"] for row in catalog.describe()}
    from src.tools import registry
    try:
        tool = catalog.resolve("toolspec_fixture_tool", {"toolspec_fixture_tool"})
        assert tool.name == "toolspec_fixture_tool"
        spec = registry.get_tool_spec("toolspec_fixture_tool")
        assert spec is not None
        # The runtime registration is the row, verbatim.
        assert (spec.category, spec.concurrency_key, spec.write_scope,
                spec.is_readonly, spec.recovery_mode, spec.authorization) == (
            "research", "toolspec_fixture", "session_artifact",
            False, "never", "")
        assert spec.preflight is module.validate_toolspec_fixture
    finally:
        registry._TOOL_SPECS.pop("toolspec_fixture_tool", None)

    # The installed table itself was never mutated by the fixture.
    assert [spec.name for spec in _PRESETS] == [spec.name for spec in TOOL_DECLARATIONS]


def test_describing_tools_imports_no_implementation():
    """The single source of truth must not become an eager import of every tool."""
    script = "\n".join((
        "import sys",
        "import src.agent.preset_catalog",
        "import src.core.tool_catalog",
        "import src.tools.legacy_registry as legacy",
        "from src.tools.tool_specs import TOOL_DECLARATIONS",
        "assert len(TOOL_DECLARATIONS) > 20",
        "assert legacy._TOOL_BINDINGS and legacy.get_tool_names()",
        "heavy = [",
        "    'src.tools.deepseek_search_fetch', 'src.tools.browse_web',",
        "    'src.tools.candidate_tools', 'src.tools.draw_svg',",
        "    'src.tools.interaction', 'src.tools.account_files',",
        "    'src.research.tools', 'src.research.figure_tools',",
        "    'src.research.word_tools', 'src.research.delivery_tools',",
        "    'src.research.workspace_tools', 'src.research.statistics_tools',",
        "    'src.research.image_integrity_tools', 'src.shared_corpus.tool_adapter',",
        "]",
        "loaded = [name for name in heavy if name in sys.modules]",
        "print(','.join(loaded))",
    ))
    result = subprocess.run([sys.executable, "-B", "-c", script],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", "eagerly imported: " + result.stdout.strip()


@pytest.mark.parametrize("overrides, reason", [
    ({"write_scope": None, "recovery_mode": "local_read"}, None),
    ({"write_scope": "session_artifact", "recovery_mode": "local_read"}, "ValueError"),
    ({"write_scope": "database"}, "ValueError"),
    ({"write_scope": None, "authorization": "anyone_who_asks"}, "ValueError"),
    ({"write_scope": None, "capabilities": ("made_up_capability",)}, "ValueError"),
    ({"write_scope": None, "display_label": "  "}, "ValueError"),
    ({"write_scope": "session_artifact", "category": "interaction"}, "ValueError"),
    ({"write_scope": None, "import_target": "src.tools.not a module"}, "ValueError"),
    ({"write_scope": None, "requires": ("session", "session")}, "ValueError"),
    ({"write_scope": None, "requires": ("session",), "produces": ("session",)}, "ValueError"),
])
def test_inconsistent_rows_fail_closed_at_declaration(overrides, reason):
    fields = {"name": "toolspec_negative_case", "import_target": "src.tools.interaction",
              "category": "web", "concurrency_key": "toolspec_negative",
              "write_scope": None, "requires": ("network",),
              "display_label": "负例", "capabilities": ("network",)}
    fields.update(overrides)
    if reason is None:
        # A read-only row may be declared as safely resumable.
        assert ToolSpec(**fields).recovery_mode == "local_read"
        return
    with pytest.raises(ValueError):
        ToolSpec(**fields)


def test_duplicate_declarations_are_rejected(monkeypatch):
    row = ToolSpec("toolspec_duplicate", "src.tools.interaction", "web",
                   "toolspec_duplicate", write_scope=None,
                   requires=("network",), display_label="重复")
    with pytest.raises(ValueError):
        tool_specs._index((row, row))


def _fixture_module():
    from langchain_core.tools import tool

    @tool
    def toolspec_fixture_tool(query: str) -> str:
        """Echo one query; proves that a single declaration is enough."""
        return query

    def validate_toolspec_fixture(**kwargs) -> None:
        return None

    module = types.ModuleType("toolspec_fixture_tools")
    module.toolspec_fixture_tool = toolspec_fixture_tool
    module.validate_toolspec_fixture = validate_toolspec_fixture
    return module


_MIGRATED = {
    "request_user_input": (
        "请你确认", "", ("interaction",),
        ("session",), (), None, "user_question",
        "interaction", "", "never", "src.tools.interaction"),
    "deepseek_search": (
        "网页搜索", "", ("literature_retrieval", "network", "route_web", "web_retrieval"),
        ("network",), (), None, "deepseek_search",
        "web", "", "never", "src.tools.deepseek_search_fetch"),
    "deepseek_fetch": (
        "读取网页", "", ("literature_retrieval", "network", "route_web", "web_retrieval"),
        ("network",), (), None, "deepseek_fetch",
        "web", "", "never", "src.tools.deepseek_search_fetch"),
    "browse_web": (
        "交互式浏览网页", "下载网页资料", ("artifact", "network", "web_retrieval"),
        ("network", "session"), (), "session_artifact", "browse_web",
        "web", "", "never", "src.tools.browse_web"),
    "deepseek_fetch_paper": (
        "获取论文全文", "", ("literature_retrieval", "network", "route_web", "web_retrieval"),
        ("network",), (), None, "deepseek_fetch_paper",
        "web", "", "never", "src.tools.deepseek_search_fetch"),
    "local_hybrid_search": (
        "本地检索", "", ("literature_retrieval", "local_corpus", "local_retrieval", "route_local"),
        ("local_corpus",), (), None, "local_hybrid_search",
        "rag", "", "never", "src.tools.deepseek_search_fetch"),
    "read_candidate": (
        "读取候选原文", "", ("local_corpus",),
        ("local_corpus", "candidate_pool"), (), None, "candidate_pool",
        "rag", "", "never", "src.tools.candidate_tools"),
    "search_candidates": (
        "补充候选检索", "", ("local_corpus",),
        ("local_corpus", "candidate_pool"), (), None, "candidate_pool",
        "rag", "", "never", "src.tools.candidate_tools"),
    "draw_svg": (
        "绘图", "下载 SVG", ("artifact", "visualization"),
        (), ("figure",), "session_artifact", "draw_svg",
        "analysis", "", "never", "src.tools.draw_svg"),
    "read_account_file": (
        "读取账户文件", "", (),
        ("account_files",), (), None, "read_account_file",
        "files", "", "local_read", "src.tools.account_files"),
    "read_tool_result": (
        "读取工具结果", "", (),
        ("session",), (), None, "read_tool_result",
        "files", "", "never", "src.research.result_tools"),
    "research_workspace": (
        "科研工作区", "", ("research",),
        ("session", "research_tools"), (), "session_artifact", "research_workspace",
        "research", "", "never", "src.research.workspace_tools"),
    "inspect_uploaded_table": (
        "读取表格", "", ("research",),
        ("research_tools", "session"), (), None, "research_data",
        "research", "", "never", "src.research.tools"),
    "audit_uploaded_data": (
        "数据审查", "下载数据审查 Excel", ("artifact", "research"),
        ("research_tools", "session"), ("annotation_table", "report_json"), "session_artifact", "research_data",
        "research", "", "never", "src.research.tools"),
    "search_literature": (
        "文献检索", "", ("literature_retrieval", "network"),
        ("network",), (), None, "literature_search",
        "web", "", "never", "src.research.tools"),
    "verify_uploaded_references": (
        "文献核验", "下载 DOI 核验表", ("artifact", "research"),
        ("research_tools", "session"), ("verification_sheet",), "session_artifact", "research_references",
        "research", "", "never", "src.research.tools"),
    "fetch_and_audit_open_supplements": (
        "获取补充数据", "下载补充材料", ("artifact", "literature_retrieval", "network", "research"),
        ("network", "research_tools", "session"), ("supplement_archive", "supplement_report"), "session_artifact", "research_web_fetch",
        "research", "", "never", "src.research.tools"),
    "word_document": (
        "处理 Word 文档", "下载 Word 文档", ("artifact", "research"),
        ("session", "research_tools"), ("report_docx",), "session_artifact", "word_documents",
        "research", "", "never", "src.research.word_tools"),
    "deliver_research_artifact": (
        "保存研究成果", "打开成果", ("artifact", "research"),
        ("session", "research_tools"), ("table_file", "bibliography", "report_html", "gap_csv"), "session_artifact", "research_artifacts",
        "research", "", "never", "src.research.delivery_tools"),
    "save_research_note": (
        "保存课题记录", "下载课题记录", ("artifact", "research"),
        ("session", "research_tools"), ("note",), "account_record", "research_notes",
        "research", "explicit_user_request", "never", "src.research.tools"),
    "recall_research_notes": (
        "查阅课题记录", "", ("research",),
        ("research_tools",), (), None, "research_notes",
        "research", "", "never", "src.research.tools"),
    "analyze_research_data": (
        "统计分析", "下载统计结果", ("artifact", "research"),
        ("research_tools", "session"), ("statistics_report",), "session_artifact", "research_statistics",
        "research", "", "never", "src.research.statistics_tools"),
    "audit_research_images": (
        "图像复用核查", "下载图像复用证据", ("artifact", "research"),
        ("account_files", "research_tools", "session"), ("report_json",), "session_artifact", "research_image_integrity",
        "research", "", "never", "src.research.image_integrity_tools"),
    "run_research_figure_script": (
        "执行绘图脚本", "下载图件", ("artifact", "research", "visualization"),
        ("session", "research_tools", "script_sandbox"), ("figure_package",), "session_artifact", "research_artifacts",
        "research", "", "never", "src.research.figure_tools"),
    "propose_document_ingest": (
        "申请收录文献", "", ("shared_corpus",),
        ("shared_corpus",), (), "account_record", "shared_corpus_proposal",
        "research", "", "never", "src.shared_corpus.tool_adapter"),
    "prepare_document": (
        "准备文献入库", "", ("shared_corpus",),
        ("shared_corpus",), (), "account_record", "shared_corpus_prepare",
        "research", "", "never", "src.shared_corpus.tool_adapter"),
    "commit_document_index": (
        "更新共享索引", "", ("shared_corpus",),
        ("shared_corpus",), (), "account_record", "shared_corpus_build",
        "research", "", "never", "src.shared_corpus.tool_adapter"),
    "search_agent_corpus": (
        "检索共享文献库", "", ("shared_corpus",),
        ("shared_corpus",), (), None, "shared_corpus_search",
        "rag", "", "never", "src.shared_corpus.tool_adapter"),
}
