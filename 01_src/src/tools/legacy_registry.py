"""Compatibility reader over the single declarative ToolSpec table.

Historically this module held a second hand-written declaration of every active
tool — route category, concurrency key, write scope, ``requires``, ``produces``
and ``authorization`` — beside ``src.agent.preset_catalog``. Both copies are gone.
The one declaration is ``src.tools.tool_specs``; this module only *resolves* the
declared import targets for the deployed legacy runner and exposes the same names
it always did.

Importing this module imports no tool implementation. The tool objects are
materialized on first access to ``_TOOL_DECLARATIONS``, ``_TOOL_REGISTRY``,
``_TOOL_REGISTRY_BY_NAME`` or one declared tool name, and that materialization
rebuilds the process registry exactly as the old import-time side effect did. The
cheap views — ``_TOOL_BINDINGS``, ``get_tool_names`` — are projections of the rows
and never import an implementation. ``install_registry()`` is the explicit entry
point for a legacy host that wants the registry populated up front.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Iterable

from src.tools.registry import (
    clear_tool_registry,
    cleanup_offload_files,
    get_offload_file_list,
    get_tool_spec,
    get_tools_by_category,
    register_declared_tool,
    register_tool,
)
from src.tools.tool_specs import (
    DECLARED_NAMES,
    TOOL_DECLARATIONS,
    ToolSpec,
    spec_by_name,
)

# Attributes that only exist once the declared implementations are resolved.
_LAZY_ATTRIBUTES = frozenset({
    "_TOOL_DECLARATIONS", "_TOOL_REGISTRY", "_TOOL_REGISTRY_BY_NAME",
})


def _rows(specs: Iterable[ToolSpec] | None) -> tuple[ToolSpec, ...]:
    return TOOL_DECLARATIONS if specs is None else tuple(specs)


def tool_bindings(specs: Iterable[ToolSpec] | None = None
                  ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], str]]:
    """name -> (requires, produces, authorization) straight from the rows.

    This is what ``tool_availability`` and the tool directory read; it never
    imports a tool implementation.
    """
    return {spec.name: (spec.requires, spec.produces, spec.authorization)
            for spec in _rows(specs)}


# name -> (requires, produces, authorization), read by tool_availability and by
# the tests that keep the declaration honest.
_TOOL_BINDINGS = tool_bindings()


def get_tool_names() -> list[str]:
    """Declared names in declaration order; no implementation is imported."""
    return [spec.name for spec in TOOL_DECLARATIONS]


_MATERIALIZED: tuple | None = None


def install_registry(specs: Iterable[ToolSpec] | None = None):
    """Resolve every row once and rebuild the legacy process tool registry."""
    global _MATERIALIZED
    rows = _rows(specs)
    names = [spec.name for spec in rows]
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate active tool name in the General Agent registry")
    if any(not spec.display_label.strip() for spec in rows):
        raise RuntimeError("Every declared tool needs one catalog label")
    tools = {spec.name: spec.resolve() for spec in rows}
    # Rebuild only this code-owned registry. No stale YAML name list or previously
    # loaded registration can silently shadow these declarations.
    clear_tool_registry()
    for spec in rows:
        register_declared_tool(spec, tools[spec.name])
    declarations = tuple(
        (tools[spec.name], spec.category, spec.concurrency_key, spec.write_scope,
         spec.requires, spec.produces, spec.authorization)
        for spec in rows)
    _MATERIALIZED = (
        declarations,
        tuple(tools[spec.name] for spec in rows),
        MappingProxyType(dict(tools)),
    )
    return _MATERIALIZED


def _materialized() -> tuple:
    if _MATERIALIZED is None:
        return install_registry()
    return _MATERIALIZED


def __getattr__(name: str):
    # Registered tools are resolved on demand: describing or authorizing a tool
    # must not import it. A tool object is still reachable as a module attribute,
    # exactly like the old eager imports, but only that one module is imported.
    if name in _LAZY_ATTRIBUTES:
        declarations, registry, by_name = _materialized()
        return {"_TOOL_DECLARATIONS": declarations, "_TOOL_REGISTRY": registry,
                "_TOOL_REGISTRY_BY_NAME": by_name}[name]
    if name in DECLARED_NAMES:
        return spec_by_name(name).resolve()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _LAZY_ATTRIBUTES | set(DECLARED_NAMES))


__all__ = [
    "register_tool", "get_tool_spec", "clear_tool_registry",
    "get_tools_by_category", "cleanup_offload_files", "get_offload_file_list",
    "get_tool_names", "install_registry", "tool_bindings",
    "_TOOL_REGISTRY", "_TOOL_REGISTRY_BY_NAME", "_TOOL_BINDINGS", "_TOOL_DECLARATIONS",
]
