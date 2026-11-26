"""Tool package with no eager domain-plugin imports.

The deployed legacy runner still requests its registry through this compatibility
facade. Importing a foundational submodule (registry, outcome, account_files)
must not import every scientific tool. The new agent capability catalog does not
depend on this legacy registry.

``src.core.tool_catalog`` imports ``src.tools.tool_specs``, which imports this
package, so this module must not import ``src.core.tool_catalog`` at import time.
The declared tool names come straight from the single ToolSpec table instead;
that keeps the package import cheap and the dependency graph acyclic.
"""
from __future__ import annotations

from importlib import import_module

from src.tools.tool_specs import DECLARED_NAMES

_REGISTRY_EXPORTS = frozenset({
    "register_tool", "get_tool_spec", "clear_tool_registry",
    "get_tools_by_category", "cleanup_offload_files", "get_offload_file_list",
})
_LEGACY_EXPORTS = frozenset({
    "get_tool_names", "_TOOL_REGISTRY", "_TOOL_REGISTRY_BY_NAME",
    "_TOOL_BINDINGS", "_TOOL_DECLARATIONS", "install_registry", "tool_bindings",
})


def __getattr__(name):
    if name in _REGISTRY_EXPORTS:
        return getattr(import_module("src.tools.registry"), name)
    if name in _LEGACY_EXPORTS or name in DECLARED_NAMES:
        return getattr(import_module("src.tools.legacy_registry"), name)
    # Python's from-package import machinery can now import ordinary submodules
    # without first loading the optional scientific registry.
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _REGISTRY_EXPORTS | _LEGACY_EXPORTS | set(DECLARED_NAMES))


__all__ = sorted(_REGISTRY_EXPORTS | _LEGACY_EXPORTS)
