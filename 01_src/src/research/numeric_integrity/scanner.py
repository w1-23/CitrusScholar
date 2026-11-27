"""Run the exact upstream scan in the sealed numeric Worker process."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from importlib.metadata import version
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

PAPERCONAN_COMMIT = "8d116455b23e858651250d8ed23ba41c1a87ab0d"
PAPERCONAN_SOURCE_SHA = "aab28242395654bd60b8e52e46c81cec07483b4a202cd1639509dd2b2af9a73d"
PINS = {"paperconan": "0.9.0", "python-calamine": "0.5.4", "scipy": "1.16.3",
        "numpy": "2.3.5", "openpyxl": "3.1.5", "et-xmlfile": "2.0.0"}


def assert_runtime():
    if any(version(name) != pin for name, pin in PINS.items()):
        raise ValueError("numeric_worker_dependency_mismatch")
    import paperconan
    import paperconan._audit as core

    if hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest() != PAPERCONAN_SOURCE_SHA:
        raise ValueError("paperconan_source_mismatch")
    return paperconan, core


def scan(bound):
    paperconan, core = assert_runtime()
    # The pinned scanner calls find_local_images unconditionally. Remove that one
    # inventory call, while expressly disabling both deeper image modes below.
    # Each invocation is one dedicated Worker process, not a shared import server.
    caps = {"_MAX_FILE_BYTES": 8 * 1024**2, "_MAX_CELLS": 250_000, "_MAX_BLOCK_COLS": 80,
            "_MAX_REPORT_BLOCKS": 300, "_MAX_TOTAL_FINDINGS": 1500, "_OOXML_FORMULA_INSPECT": True}
    with TemporaryDirectory(prefix="citrus-numeric-") as work:
        root = Path(work)
        inputs = root / "snapshot"
        inputs.mkdir(mode=0o700)
        for source, data in bound:
            (inputs / source.staged_name).write_bytes(data)
        # Never surface upstream logs (paths, workbook labels, snippets) as public output.
        with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink), redirect_stderr(sink):
            with patch.object(core, "find_local_images", return_value=[]), patch.multiple(core, **caps):
                return paperconan.audit_dir(str(inputs), str(root / "report"), write_md=False, write_html=False,
                                            write_json=False, evidence=False, images=False, image_diagnostics=False,
                                            runtime_metadata=False, paper={"scope": "authorized_numeric_snapshot"})
