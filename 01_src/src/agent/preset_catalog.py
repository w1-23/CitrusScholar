"""Installed tool contracts, projected from the single declarative ToolSpec table.

``src.tools.tool_specs`` declares every installed tool once: name, import target,
category, concurrency key, write scope, ``requires``, ``produces``,
``authorization``, recovery mode, extension validator and the presentation facts.
This module only projects those rows:

* the lazy Host ``CapabilityCatalog`` (``build_preset_catalog``);
* the readiness/authorization bindings (``preset_bindings``);
* the scheduling and presentation metadata (``preset_metadata``);
* the packaged JSON schema manifest (``generate_tool_manifest``).

The adjacent JSON is a code-owned package resource, generated at development time
from the same rows. It is never supplied by a request, skill, model, or workspace
document: the manifest cannot introduce executable targets, grant permissions or
change a capability. Implementations are imported only when one selected tool is
loaded, never while describing the catalogue.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import partial
from importlib.resources import files
import json
import inspect

from src.agent.capabilities import CapabilityCatalog, CapabilityDefinition, CapabilityLoadError
from src.tools.tool_specs import IMPORT_TARGETS, TOOL_DECLARATIONS, ToolSpec

# One process uses one installed contract snapshot, including lazily loaded tools.
# Updating package files requires a new process; no request hot-reloads metadata.
_INSTALLED_MANIFEST = files(__package__).joinpath("preset_tool_schemas.json").read_text(encoding="utf-8")

__all__ = ["build_preset_catalog", "canonical_tool_schema", "generate_tool_manifest",
           "preset_bindings", "preset_metadata"]

# Compatibility views of the one declaration table. They are projections of the
# ToolSpec rows, never a second hand-written copy: ``_TARGETS`` is the import
# target of each row and ``_PRESETS`` is the row itself. The manifest script and
# older callers read these names; new code should read the rows directly.
_TARGETS: Mapping[str, str] = IMPORT_TARGETS
_PRESETS: tuple[ToolSpec, ...] = TOOL_DECLARATIONS


def _rows(specs: Iterable[ToolSpec] | None) -> tuple[ToolSpec, ...]:
    """The declared rows, or an explicit set when a caller supplies one."""
    return TOOL_DECLARATIONS if specs is None else tuple(specs)


def preset_bindings(specs: Iterable[ToolSpec] | None = None
                    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], str]]:
    """Host readiness/authorization inputs; discovery itself grants no rights."""
    return {p.name: (p.requires, p.produces, p.authorization) for p in _rows(specs)}


def preset_metadata(specs: Iterable[ToolSpec] | None = None) -> dict[str, dict]:
    """Return independent policy and presentation records without loading tools."""
    return {p.name: {
        "category": p.category, "concurrency_key": p.concurrency_key,
        "write_scope": p.write_scope, "is_readonly": p.is_readonly,
        "authorization": p.authorization, "recovery_mode": p.recovery_mode,
        "execution_mode": p.execution_mode,
        # Presentation facts live on the same row, so a front end, an API tool
        # directory and the model schema cannot disagree about one tool.
        "display_label": p.display_label, "link_label": p.link_label,
        "capabilities": list(p.capabilities),
    } for p in _rows(specs)}


def _read_manifest(specs: Iterable[ToolSpec] | None = None) -> dict[str, dict]:
    rows = _rows(specs)
    # The fail-closed name check is against the projection being built: an
    # installed manifest may never name a tool that is not declared here, and a
    # caller that supplies explicit rows (tests, tooling) gets the same rule.
    targets = _TARGETS if specs is None else {p.name: p.import_target for p in rows}
    manifest = json.loads(_INSTALLED_MANIFEST)
    if set(manifest) != {"version", "tools"} or manifest["version"] != 1:
        raise ValueError("invalid_preset_manifest")
    if not isinstance(manifest["tools"], list):
        raise ValueError("invalid_preset_schemas")
    schemas = {}
    for schema in manifest["tools"]:
        if (not isinstance(schema, dict) or set(schema) != {"type", "function"}
                or schema["type"] != "function"):
            raise ValueError("invalid_preset_schema")
        function = schema["function"]
        if not isinstance(function, dict) or set(function) != {"name", "description", "parameters"}:
            raise ValueError("invalid_preset_function")
        name = function["name"]
        # ``targets`` is derived from the same rows; the lookup stays fail-closed
        # so a hand-edited manifest still cannot name an undeclared tool.
        if not isinstance(name, str) or name not in targets or name in schemas:
            raise ValueError("unknown_or_duplicate_preset")
        if not isinstance(function["description"], str) or not function["description"].strip():
            raise ValueError("invalid_preset_description")
        if (not isinstance(function["parameters"], dict)
                or function["parameters"].get("type") != "object"):
            raise ValueError("invalid_preset_parameters")
        schemas[name] = schema
    if set(schemas) != set(targets) or set(schemas) != {p.name for p in rows}:
        raise ValueError("incomplete_preset_manifest")
    return schemas


def canonical_tool_schema(tool):
    """Normalize only docstring indentation across Python 3.12 and 3.13."""
    from langchain_core.utils.function_calling import convert_to_openai_tool

    schema = convert_to_openai_tool(tool)
    function = schema['function']
    return {**schema, 'function': {**function,
        'description': inspect.cleandoc(function['description'])}}


def _load(preset: ToolSpec, expected: dict):
    # Import target and name are closed-over code-owned row values, never text
    # from a manifest, request, Skill or workspace document.
    tool = preset.resolve()
    actual = canonical_tool_schema(tool)
    if actual != expected:
        def difference(left, right, path=""):
            if isinstance(left, dict) and isinstance(right, dict):
                for key in sorted(set(left) | set(right)):
                    child = path + "/" + str(key)
                    if key not in left or key not in right:
                        return child.strip("/")
                    if left[key] != right[key]:
                        return difference(left[key], right[key], child)
            return path.strip("/") or "schema"
        raise CapabilityLoadError("preset_schema_drift", difference(actual, expected))
    # Register only the validated selected tool; preserve all existing specs.
    from src.tools.registry import register_declared_tool

    # Extension validators are server code, outside the provider schema and Skill
    # payload. The row names the validator; the runner invokes it before marking
    # execution dispatched. ``register_declared_tool`` is the same projection the
    # compatibility registry uses, so both paths register one identical policy.
    register_declared_tool(preset, tool)
    return tool


def build_preset_catalog(specs: Iterable[ToolSpec] | None = None) -> CapabilityCatalog:
    """Describe all declared tools without importing any implementation.

    The host must still supply its authorized/ready name set to the catalogue.
    A missing dependency or changed implementation fails closed on resolution,
    before registration/dispatch, and affects only the selected capability.
    """
    rows = _rows(specs)
    schemas = _read_manifest(rows)
    catalog = CapabilityCatalog()
    for preset in rows:
        schema = schemas[preset.name]
        function = schema["function"]
        catalog.register(CapabilityDefinition(
            name=preset.name, description=function["description"],
            parameters=function["parameters"], load=partial(_load, preset, schema),
            provider=preset.import_target, mutates=preset.mutates,
            execution_mode=preset.execution_mode,
        ))
    return catalog


def generate_tool_manifest(specs: Iterable[ToolSpec] | None = None) -> dict:
    """Regenerate the packaged schema manifest from the same declarative rows.

    This is the only writer of ``preset_tool_schemas.json``; ``--check`` compares
    this projection with the installed file so generation and loading can never
    disagree about a tool.
    """
    schemas = []
    for preset in _rows(specs):
        # The loader compares this exact normalized form; keep both sides equal.
        schema = canonical_tool_schema(preset.resolve())
        if schema["function"]["name"] != preset.name:
            raise ValueError("preset_tool_name_drift:" + preset.name)
        schemas.append(schema)
    return {"version": 1, "tools": schemas}


def allowed_presets(state: dict, *, has_tables: bool, has_pool: bool) -> set[str]:
    import os

    from src.config import settings
    from src.core.capability_policy import policy_for_state
    from src.core.tracing import web_search_enabled
    from src.graph.retrieval_router import attachment_processing_only, retrieval_denied, source_scope
    from src.graph.tool_availability import runtime_env, provider_denials
    query = str(state.get("query") or "")
    scope = source_scope(query, exclusive_only=True)
    local_processing = attachment_processing_only(query,
        has_source=bool(has_tables or state.get("attachment_refs")))
    env = runtime_env(source_scope=scope,
        attachments=int(has_tables), account_files=len(state.get("account_files") or []),
        session=bool(state.get("session_id")), network=web_search_enabled(),
        research_tools=os.getenv("CITRUS_RESEARCH_TOOLS_ENABLED", "true").strip().lower()
                       not in {"0", "false", "no", "off"},
        retrieval=not local_processing and not retrieval_denied(query, scope), candidate_pool=has_pool,
        denied=provider_denials(settings))
    policy = policy_for_state(state)
    names = set()
    # Capability facts, readiness preconditions and authorization are read from
    # the same rows the catalogue was built from.
    for preset in TOOL_DECLARATIONS:
        name = preset.name
        caps = frozenset(preset.capabilities)
        if local_processing and (name == "verify_uploaded_references" or
                                 caps & {"network", "local_corpus", "shared_corpus"}):
            continue
        if (not policy.tool_allowed(name) or name in env.denied
                or not all(env.has(requirement) for requirement in preset.requires)):
            continue
        if not state.get("visualization_enabled", True) and "visualization" in caps:
            continue
        names.add(name)
    return names
