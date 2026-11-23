"""Pytest bootstrap for the in-repository package layout.

The distribution installs ``packages/*`` into site-packages while the
regression job also collects the same modules from the working tree.  Put the
working-tree packages first so both collection paths resolve to one module.
"""
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent
for _entry in (_ROOT / "01_src", _ROOT / "packages"):
    _path = str(_entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)
