"""Regenerate the code-owned preset schemas in an installed development env.

The manifest is projected from the single declarative ToolSpec table
(``src.tools.tool_specs``); no second declaration is parsed or kept in sync.
Add --check to verify the installed manifest without writing. No tool is invoked;
the rows name their import targets and this script imports them only to read the
provider schema.

From _scratch/local_service_c37f40a:
  PYTHONPATH=../../01_src python -B ../../01_src/scripts/update_tool_manifest.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    from src.agent.preset_catalog import generate_tool_manifest
    from src.core.tool_catalog import TOOL_METADATA
    from src.tools.tool_specs import TOOL_DECLARATIONS

    declared = {spec.name for spec in TOOL_DECLARATIONS}
    # The schema, the presentation catalog and the declarations are three
    # projections of one table. If they disagree, the manifest itself is wrong,
    # so refuse to write rather than shipping a tool without a label/capability.
    if set(TOOL_METADATA) != declared:
        raise ValueError("tool_metadata_name_drift")

    manifest = generate_tool_manifest()
    if {tool["function"]["name"] for tool in manifest["tools"]} != declared:
        raise ValueError("preset_manifest_name_drift")

    root = Path(__file__).resolve().parents[1]
    destination = root / "src/agent/preset_tool_schemas.json"
    total = len(manifest["tools"])
    if args.check:
        if json.loads(destination.read_text(encoding="utf-8")) != manifest:
            raise ValueError("preset_schema_manifest_outdated")
        print(f"Verified {total} preset schemas")
    else:
        destination.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"Updated {total} preset schemas")


if __name__ == "__main__":
    main()
