"""Capability facts and public labels, derived from the one ToolSpec table.

``src.tools.tool_specs`` declares every installed tool once, including the
capability facts and the user-facing labels. This module projects that table into
``TOOL_METADATA`` and derives the source/route capability groups from it, so the
execution contract, the front end labels and ``requires``/``produces`` cannot
disagree about one tool.

Importing this module never imports a tool implementation: the projection reads
declared values only. It also stays free of application imports, so the low level
progress bus and the routing tables can use it without creating an import cycle.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.tools.tool_specs import TOOL_DECLARATIONS, ToolSpec


@dataclass(frozen=True)
class ToolMetadata:
    display_label: str
    link_label: str = ""
    capabilities: frozenset[str] = frozenset()


# Capabilities are environment facts, never topic keywords.
NETWORK = "network"
LOCAL_CORPUS = "local_corpus"
WEB_RETRIEVAL = "web_retrieval"
LOCAL_RETRIEVAL = "local_retrieval"
LITERATURE_RETRIEVAL = "literature_retrieval"
ROUTE_WEB = "route_web"
ROUTE_LOCAL = "route_local"
RESEARCH = "research"
ARTIFACT = "artifact"
VISUALIZATION = "visualization"
SHARED_CORPUS = "shared_corpus"


def metadata_catalog(specs: Iterable[ToolSpec] | None = None) -> dict[str, ToolMetadata]:
    """Project the declared rows into presentation metadata.

    Every row produces exactly one record: the key set of the result equals the
    declared name set by construction, and a missing label fails closed instead of
    silently degrading to a bare tool name.
    """
    rows = TOOL_DECLARATIONS if specs is None else tuple(specs)
    catalog: dict[str, ToolMetadata] = {}
    for spec in rows:
        name = spec.name
        if name in catalog:
            raise ValueError("duplicate_tool_metadata:" + name)
        if not spec.display_label.strip():
            raise ValueError("tool_metadata_label_required:" + name)
        catalog[name] = ToolMetadata(spec.display_label, spec.link_label,
                                     frozenset(spec.capabilities))
    if set(catalog) != {spec.name for spec in rows}:
        # Never a silent union/difference: a partial projection is a bug.
        raise ValueError("tool_metadata_name_drift")
    return catalog


# One projection of the declared rows; no second hand-written table.
TOOL_METADATA: dict[str, ToolMetadata] = metadata_catalog()


def capability_groups() -> dict[str, frozenset[str]]:
    """Derive all source/route groups from one catalog."""
    groups: dict[str, set[str]] = {
        key: set() for key in (
            "network", "local_corpus", "web_retrieval", "local_retrieval",
            "literature_retrieval", "route_web", "route_local", "research",
            "artifact", "visualization",
        )
    }
    for name, metadata in TOOL_METADATA.items():
        for capability in metadata.capabilities:
            groups.setdefault(capability, set()).add(name)
    groups["web"] = groups["network"]
    groups["local"] = groups["local_corpus"]
    groups["retrieval"] = (groups["web_retrieval"] | groups["local_retrieval"]
                           | groups["literature_retrieval"])
    groups["web_route"] = groups["route_web"]
    groups["local_route"] = groups["route_local"]
    return {key: frozenset(value) for key, value in groups.items()}


# Host-owned tools are registered outside the research preset catalog. Keep
# their presentation metadata here without changing any permission groups.
HOST_TOOL_LABELS = {
    "update_task_plan": "更新工作计划",
    "write_todos": "更新工作计划",
    "read_skill": "读取科研方法",
    "load_skill": "加载科研方法",
    "read_context_evidence": "回读对话证据",
    "workspace_list": "列出工作区文件",
    "workspace_glob": "查找工作区文件",
    "workspace_search": "搜索工作区内容",
    "workspace_read": "读取工作区文件",
    "workspace_write": "保存工作区文件",
    "run_external_skill": "运行外部科研方法",
    "continue_external_literature": "查询公开文献登记",
    "collect_external_skill": "读取并交付审查结果",
    "run_nature_plot": "使用上游 Nature 模板绘图",
    "collect_nature_plot": "交付图件和工作台项目",
    "workspace_edit": "修改工作区文件",
}


def display_labels() -> dict[str, str]:
    return {**{name: metadata.display_label for name, metadata in TOOL_METADATA.items()},
            **HOST_TOOL_LABELS}


def artifact_link_labels() -> dict[str, str]:
    return {name: metadata.link_label for name, metadata in TOOL_METADATA.items()
            if metadata.link_label}


def capabilities(name: str) -> frozenset[str]:
    return TOOL_METADATA.get(str(name or ""), ToolMetadata("")).capabilities


__all__ = [
    "ARTIFACT", "LITERATURE_RETRIEVAL", "LOCAL_CORPUS", "LOCAL_RETRIEVAL",
    "NETWORK", "RESEARCH", "ROUTE_LOCAL", "ROUTE_WEB", "SHARED_CORPUS", "TOOL_METADATA",
    "ToolMetadata", "VISUALIZATION", "WEB_RETRIEVAL", "artifact_link_labels",
    "capabilities", "capability_groups", "display_labels", "metadata_catalog",
]
