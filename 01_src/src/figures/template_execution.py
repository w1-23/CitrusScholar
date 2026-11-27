"""The single host bridge from a fixed template to the one figure execution chain.

Audit F-04 retired the *second* execution chain, not the templates. This module
is where the retired entry points meet the surviving one: a typed template
request becomes ``run_research_figure_script(mode="template", ...)`` with the
same frozen input snapshot, the same Worker, the same timeout policy, the same
output contract, the same current-QA and the same Figure Workbench project.

There is deliberately no second scheduler here: no submission record, no
workspace, no worker protocol. The unified tool owns the run id, and the run id
in its receipt is the only execution id a caller may report.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

UNIFIED_TOOL = "run_research_figure_script"


def template_payload(parameters: Any) -> str:
    """Canonical JSON for typed template parameters."""
    return json.dumps(parameters or {}, ensure_ascii=False, sort_keys=True, allow_nan=False)


def execute_template(*, template_id: str, parameters: Any, file_id: str,
                     title: str = "", formats: Iterable[str] = ("svg", "pdf"),
                     binding: str = "data") -> tuple[str, dict]:
    """Run one registry template through the unified tool; return its receipt.

    ``file_id`` is the exact authorized account file. No path, no argv and no
    executable code crosses this bridge: the registry compiles the template from
    typed parameters, and the unified chain snapshots the file itself.
    """
    from src.research import figure_tools

    if not isinstance(file_id, str) or len(file_id) != 32:
        raise ValueError("template_binding_requires_file_id")
    bindings = json.dumps({str(binding or "data"): {"file_id": file_id}}, ensure_ascii=False)
    content, artifact = figure_tools.run_research_figure_script.func(
        mode="template", template_id=str(template_id),
        template_parameters_json=template_payload(parameters),
        inputs_json=bindings, title=str(title or ""),
        formats_json=json.dumps([str(fmt) for fmt in formats], ensure_ascii=False))
    return content, artifact


def forwarded_receipt(artifact: dict[str, Any]) -> tuple[str, str]:
    """(execution id, status) of a unified execution, without inventing either."""
    ticket = artifact.get("_tool_outcome") if isinstance(artifact, dict) else None
    status = str((ticket or {}).get("status") or "unknown")
    execution_id = str((artifact or {}).get("run_id") or (artifact or {}).get("execution_id") or "")
    return execution_id, ("succeeded" if status == "complete" else
                          "failed" if status in {"failed", "not_run"} else "partial")


__all__ = ["UNIFIED_TOOL", "execute_template", "forwarded_receipt", "template_payload"]
