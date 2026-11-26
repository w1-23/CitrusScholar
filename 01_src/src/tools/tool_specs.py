"""One declarative ToolSpec table: the only place an installed tool is declared.

Every consumer projects this table and never keeps a second copy of it:

* ``src.agent.preset_catalog`` builds the lazy Host ``CapabilityCatalog``, the
  readiness/authorization bindings, the scheduling metadata and the packaged
  schema manifest from these rows;
* ``src.core.tool_catalog`` derives ``TOOL_METADATA`` (capability facts and the
  user-facing labels) from the same rows, so presentation cannot drift from the
  execution contract;
* ``src.tools.legacy_registry`` is a compatibility reader that resolves the
  declared import targets instead of re-declaring route, scope or permissions;
* ``scripts/update_tool_manifest.py`` regenerates ``preset_tool_schemas.json``
  from the same rows.

A row stores an import *target string*, never an imported implementation, so
describing, scheduling and authorizing every installed tool never imports any of
them. Resolution happens only when one selected capability is actually loaded.

Adding a tool is one row here; changing its schema, capability, ``requires``,
recovery mode, concurrency key or permissions is the same one edit.

The ``requires`` / ``produces`` / ``authorization`` vocabularies are owned by
``src.graph.tool_availability`` (``KNOWN_REQUIRES``, ``KNOWN_PRODUCES``,
``KNOWN_AUTHORIZATIONS``). This module must not import that graph-layer module at
import time; ``preset_schema_manifest_test`` cross-checks the two fail-closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import re
from types import MappingProxyType
from typing import Any

__all__ = [
    "CAPABILITIES", "DECLARED_NAMES", "IMPORT_TARGETS", "TOOL_DECLARATIONS", "ToolSpec",
    "declared_names", "spec_by_name",
]

# Code-owned capability vocabulary. A capability is an environment fact, never a
# topic keyword. Adding one here is part of declaring a tool.
CAPABILITIES = frozenset({
    "interaction", "network", "local_corpus", "web_retrieval", "local_retrieval",
    "literature_retrieval", "route_web", "route_local", "research", "artifact",
    "visualization", "shared_corpus",
})

# Execution-time acceptance rules shared with src.tools.registry.
WRITE_SCOPES = frozenset({None, "session_artifact", "account_record"})
AUTHORIZATIONS = frozenset({"", "explicit_user_request"})
RECOVERY_MODES = frozenset({"never", "local_read", "pure_computation"})

_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_-]{0,63}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


@dataclass(frozen=True)
class ToolSpec:
    """Immutable execution and presentation contract for one installed tool.

    ``write_scope is None`` means the tool has no persistent write. A writable
    tool must declare its scope; a read-only tool cannot declare one.
    """

    name: str
    import_target: str
    category: str
    concurrency_key: str
    write_scope: str | None = None
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    authorization: str = ""
    recovery_mode: str = "never"
    # Attribute name of a code-owned pure validator in the same module. Never a
    # model-supplied value and never a second executable target.
    validator: str = ""
    display_label: str = ""
    link_label: str = ""
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value in (("name", self.name), ("category", self.category),
                                  ("concurrency_key", self.concurrency_key)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("tool_spec_required_field:" + field_name)
        if not _NAME.fullmatch(self.name):
            raise ValueError("invalid_tool_spec_name:" + str(self.name))
        if not isinstance(self.import_target, str) or not _MODULE.fullmatch(self.import_target):
            raise ValueError("invalid_tool_spec_import_target:" + self.name)
        if not isinstance(self.category, str) or not self.category.strip():
            raise ValueError("tool_spec_required_field:category")
        if self.category == "interaction" and self.write_scope is not None:
            raise ValueError("interactive_tool_cannot_write:" + self.name)
        if self.write_scope not in WRITE_SCOPES:
            raise ValueError(f"unsupported write scope for {self.name}: {self.write_scope}")
        if self.authorization not in AUTHORIZATIONS:
            raise ValueError(f"unsupported authorization for {self.name}: {self.authorization}")
        if self.recovery_mode not in RECOVERY_MODES:
            raise ValueError(f"unsupported recovery_mode for {self.name}: {self.recovery_mode}")
        if self.recovery_mode != "never" and self.write_scope is not None:
            raise ValueError("only read-only tools may declare a recovery mode:" + self.name)
        if self.validator and (not _IDENTIFIER.fullmatch(self.validator)
                               or self.validator == self.name):
            raise ValueError("invalid_tool_spec_validator:" + self.name)
        if not self.display_label.strip():
            raise ValueError("tool_spec_display_label_required:" + self.name)
        for field_name, values in (("requires", self.requires), ("produces", self.produces),
                                   ("capabilities", self.capabilities)):
            if not isinstance(values, tuple) or any(
                    not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"tool_spec_{field_name}_must_be_strings:" + self.name)
            if len(set(values)) != len(values):
                raise ValueError(f"tool_spec_duplicate_{field_name}:" + self.name)
        if set(self.requires) & set(self.produces):
            raise ValueError("tool_spec_requirement_is_also_an_output:" + self.name)
        unknown = set(self.capabilities) - CAPABILITIES
        if unknown:
            raise ValueError("tool_spec_unknown_capability:" + self.name + ":" + sorted(unknown)[0])

    @property
    def is_readonly(self) -> bool:
        """No persistent write, so nothing to reconcile after a restart."""
        return self.write_scope is None

    @property
    def mutates(self) -> bool:
        return self.write_scope is not None

    @property
    def execution_mode(self) -> str:
        """Scheduling hint: interaction barrier, sequential work, parallel read."""
        if self.category == "interaction":
            return "interaction"
        return "sequential" if self.write_scope is not None else "parallel"

    def resolve(self) -> Any:
        """Import the declared target and return the tool object.

        This is the only place an implementation is imported, and it runs on
        selection, never on description.
        """
        return getattr(import_module(self.import_target), self.name)

    def preflight(self):
        """The declared code-owned validator, or ``None`` when there is none."""
        if not self.validator:
            return None
        return getattr(import_module(self.import_target), self.validator)


_DECLARATIONS: tuple[ToolSpec, ...] = (
    ToolSpec("request_user_input", "src.tools.interaction", "interaction", "user_question",
             write_scope=None, requires=("session",), display_label="请你确认",
             capabilities=("interaction",)),
    ToolSpec("deepseek_search", "src.tools.deepseek_search_fetch", "web", "deepseek_search",
             write_scope=None, requires=("network",), display_label="网页搜索",
             capabilities=("literature_retrieval", "network", "route_web", "web_retrieval")),
    ToolSpec("deepseek_fetch", "src.tools.deepseek_search_fetch", "web", "deepseek_fetch",
             write_scope=None, requires=("network",), display_label="读取网页",
             capabilities=("literature_retrieval", "network", "route_web", "web_retrieval")),
    ToolSpec("browse_web", "src.tools.browse_web", "web", "browse_web",
             write_scope="session_artifact", requires=("network", "session"),
             display_label="交互式浏览网页", link_label="下载网页资料",
             capabilities=("artifact", "network", "web_retrieval")),
    ToolSpec("deepseek_fetch_paper", "src.tools.deepseek_search_fetch", "web",
             "deepseek_fetch_paper", write_scope=None, requires=("network",),
             display_label="获取论文全文",
             capabilities=("literature_retrieval", "network", "route_web", "web_retrieval")),
    ToolSpec("local_hybrid_search", "src.tools.deepseek_search_fetch", "rag",
             "local_hybrid_search", write_scope=None, requires=("local_corpus",),
             display_label="本地检索",
             capabilities=("literature_retrieval", "local_corpus", "local_retrieval", "route_local")),
    ToolSpec("read_candidate", "src.tools.candidate_tools", "rag", "candidate_pool",
             write_scope=None, requires=("local_corpus", "candidate_pool"), display_label="读取候选原文",
             capabilities=("local_corpus",)),
    ToolSpec("search_candidates", "src.tools.candidate_tools", "rag", "candidate_pool",
             write_scope=None, requires=("local_corpus", "candidate_pool"), display_label="补充候选检索",
             capabilities=("local_corpus",)),
    ToolSpec("draw_svg", "src.tools.draw_svg", "analysis", "draw_svg",
             write_scope="session_artifact", requires=(), produces=("figure",), display_label="绘图",
             validator="preflight_draw_svg", link_label="下载 SVG",
             capabilities=("artifact", "visualization")),
    ToolSpec("read_account_file", "src.tools.account_files", "files", "read_account_file",
             write_scope=None, requires=("account_files",), recovery_mode="local_read",
             display_label="读取账户文件", capabilities=()),
    ToolSpec("read_tool_result", "src.research.result_tools", "files", "read_tool_result",
             write_scope=None, requires=("session",), display_label="读取工具结果", capabilities=()),
    ToolSpec("research_workspace", "src.research.workspace_tools", "research",
             "research_workspace", write_scope="session_artifact",
             requires=("session", "research_tools"), display_label="科研工作区",
             capabilities=("research",)),
    ToolSpec("inspect_uploaded_table", "src.research.tools", "research", "research_data",
             write_scope=None, requires=("research_tools", "session"),
             validator="validate_inspection_request", display_label="读取表格",
             capabilities=("research",)),
    ToolSpec("audit_uploaded_data", "src.research.tools", "research", "research_data",
             write_scope="session_artifact", requires=("research_tools", "session"),
             produces=("annotation_table", "report_json"), validator="validate_audit_request",
             display_label="数据审查", link_label="下载数据审查 Excel",
             capabilities=("artifact", "research")),
    ToolSpec("search_literature", "src.research.tools", "web", "literature_search",
             write_scope=None, requires=("network",), display_label="文献检索",
             capabilities=("literature_retrieval", "network")),
    ToolSpec("verify_uploaded_references", "src.research.tools", "research", "research_references",
             write_scope="session_artifact", requires=("research_tools", "session"),
             produces=("verification_sheet",), display_label="文献核验", link_label="下载 DOI 核验表",
             capabilities=("artifact", "research")),
    ToolSpec("fetch_and_audit_open_supplements", "src.research.tools", "research",
             "research_web_fetch", write_scope="session_artifact",
             requires=("network", "research_tools", "session"),
             produces=("supplement_archive", "supplement_report"), display_label="获取补充数据",
             link_label="下载补充材料",
             capabilities=("artifact", "literature_retrieval", "network", "research")),
    ToolSpec("word_document", "src.research.word_tools", "research", "word_documents",
             write_scope="session_artifact", requires=("session", "research_tools"),
             produces=("report_docx",), display_label="处理 Word 文档", link_label="下载 Word 文档",
             capabilities=("artifact", "research")),
    ToolSpec("deliver_research_artifact", "src.research.delivery_tools", "research",
             "research_artifacts", write_scope="session_artifact",
             requires=("session", "research_tools"),
             produces=("table_file", "bibliography", "report_html", "gap_csv"),
             display_label="保存研究成果", link_label="打开成果", capabilities=("artifact", "research")),
    ToolSpec("save_research_note", "src.research.tools", "research", "research_notes",
             write_scope="account_record", requires=("session", "research_tools"),
             produces=("note",), authorization="explicit_user_request", display_label="保存课题记录",
             link_label="下载课题记录", capabilities=("artifact", "research")),
    ToolSpec("recall_research_notes", "src.research.tools", "research", "research_notes",
             write_scope=None, requires=("research_tools",), display_label="查阅课题记录",
             capabilities=("research",)),
    ToolSpec("analyze_research_data", "src.research.statistics_tools", "research",
             "research_statistics", write_scope="session_artifact",
             requires=("research_tools", "session"), produces=("statistics_report",),
             display_label="统计分析", link_label="下载统计结果", capabilities=("artifact", "research")),
    ToolSpec("audit_research_images", "src.research.image_integrity_tools", "research",
             "research_image_integrity", write_scope="session_artifact",
             requires=("account_files", "research_tools", "session"), produces=("report_json",),
             validator="validate_image_audit_request", display_label="图像复用核查",
             link_label="下载图像复用证据", capabilities=("artifact", "research")),
    ToolSpec("run_research_figure_script", "src.research.figure_tools", "research",
             "research_artifacts", write_scope="session_artifact",
             requires=("session", "research_tools", "script_sandbox"),
             produces=("figure_package",), display_label="执行绘图脚本", link_label="下载图件",
             capabilities=("artifact", "research", "visualization")),
    ToolSpec("propose_document_ingest", "src.shared_corpus.tool_adapter", "research",
             "shared_corpus_proposal", write_scope="account_record", requires=("shared_corpus",),
             display_label="申请收录文献", capabilities=("shared_corpus",)),
    ToolSpec("prepare_document", "src.shared_corpus.tool_adapter", "research",
             "shared_corpus_prepare", write_scope="account_record", requires=("shared_corpus",),
             display_label="准备文献入库", capabilities=("shared_corpus",)),
    ToolSpec("commit_document_index", "src.shared_corpus.tool_adapter", "research",
             "shared_corpus_build", write_scope="account_record", requires=("shared_corpus",),
             display_label="更新共享索引", capabilities=("shared_corpus",)),
    ToolSpec("search_agent_corpus", "src.shared_corpus.tool_adapter", "rag",
             "shared_corpus_search", write_scope=None, requires=("shared_corpus",),
             display_label="检索共享文献库", capabilities=("shared_corpus",)),
)


def _index(declarations: tuple[ToolSpec, ...]) -> MappingProxyType[str, ToolSpec]:
    index: dict[str, ToolSpec] = {}
    for spec in declarations:
        if spec.name in index:
            raise ValueError("duplicate_tool_spec:" + spec.name)
        index[spec.name] = spec
    return MappingProxyType(index)


# Declaration order is load-bearing: the packaged schema manifest, the capability
# catalogue and the tool directory all preserve it.
TOOL_DECLARATIONS: tuple[ToolSpec, ...] = _DECLARATIONS
_SPEC_BY_NAME = _index(TOOL_DECLARATIONS)

# name -> import target. Kept for the compatibility readers that resolve a
# declared tool by name; both maps are projections of the same rows.
IMPORT_TARGETS: MappingProxyType[str, str] = MappingProxyType(
    {spec.name: spec.import_target for spec in TOOL_DECLARATIONS})


def spec_by_name(name: str) -> ToolSpec | None:
    """The one declaration for ``name``, or ``None`` when it is not installed."""
    return _SPEC_BY_NAME.get(str(name or ""))


# Declared names as a set, for the fail-closed key checks the consumers perform.
DECLARED_NAMES: frozenset[str] = frozenset(_SPEC_BY_NAME)


def declared_names() -> frozenset[str]:
    return DECLARED_NAMES
