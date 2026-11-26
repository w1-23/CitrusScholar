#!/usr/bin/env python3
"""Stable entrypoint for the audited 8043 diagnostic exporter."""
from pathlib import Path
from runpy import run_path

_TARGET = Path('/home/WZQ/citrusscholar/releases/8043-reliability-619180208def/source/04_scripts/export_8043_turns.py')
if __name__ == "__main__":
    run_path(str(_TARGET), run_name="__main__")
else:
    _module = run_path(str(_TARGET))
    HEADER = _module["HEADER"]
    JOB_ID = _module["JOB_ID"]
    SHANGHAI = _module["SHANGHAI"]
    runtime_root = _module["runtime_root"]
    save_export = _module["save_export"]
