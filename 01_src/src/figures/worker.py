"""The sandbox worker: the only place that touches an untrusted script.

This file runs **inside** the container. It is the boundary: the script's
process, its filesystem view and its network access are all confined to the
container that runs this file.

What it does
------------
1. Reads the job description (which script, which inputs, which outputs).
2. Runs the script with Matplotlib in the Agg backend, capturing a legacy
   single figure or explicitly emitted v2 tables, figures and report.
3. Writes host-allocated outputs, with per-item/per-format v2 statuses.
4. Commits a bounded v2 result.json receipt; stdout is diagnostic only.

What it refuses to do
---------------------
* Run a script that asks for the network, a subprocess, or a file outside
  the job's own directory. Those are blocked at the container level (no
  network, no extra processes) and re-checked here, because a defence that
  exists in only one place is a defence that can be misconfigured away.
* Accept a legacy single-figure job that made none (v2 explicitly permits zero).
* Emit output larger than the budget.

The worker depends on the figure engine, Matplotlib and the pinned nature-figure
QA modules (including PyMuPDF for final-PDF collision checks). It must not import
the service, because the service is not in this image and must not be reachable
from here.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import keyword
import os
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_SCRIPT_ERROR = 10
EXIT_NO_FIGURE = 11
EXIT_MULTIPLE_FIGURES = 12
EXIT_OUTPUT_TOO_LARGE = 14
EXIT_FORBIDDEN = 15

# Modules a figure script has no business importing. The container has no
# network and no privileges, but the *intent* to escape is worth reporting
# rather than merely surviving.
_FORBIDDEN_MODULES = (
    "socket", "http", "urllib", "requests", "ftplib", "smtplib", "telnetlib",
    "subprocess", "multiprocessing", "ctypes", "pty", "webbrowser", "asyncio",
)

_MAX_OUTPUT_BYTES = 24 * 1024 * 1024

_INPUT_CONTRACT_VERSION = 1
_INPUT_TABLE_FORMATS = ("csv", "tsv", "excel", "dataset")
_INPUT_NAME = re.compile("[A-Za-z_][A-Za-z0-9_]{0,63}", re.ASCII)
_INPUT_RESERVED = frozenset({"__name__", "__builtins__", "plt", "matplotlib", "show", "emit",
                             "load_dataset", "parameters", "_citrus_parameters_json"})
_INPUT_MAX_BYTES = 64 * 1024 * 1024


def _input_name(value, role: str, *, binding: bool = True) -> str:
    """Validate a contract name without echoing untrusted text."""
    if (not isinstance(value, str) or not _INPUT_NAME.fullmatch(value)
            or (binding and keyword.iskeyword(value))):
        raise ValueError(f"Input contract {role} names must be ASCII identifiers.")
    return value


def normalize_input_contract(value) -> dict:
    """Normalize the opt-in v1 input namespace; empty keeps the legacy path.

    The host declares each sandbox input's role before execution. The worker
    never guesses a name or a format from an extension, a filename or bytes.
    """
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or set(value) - {"version", "raw", "tables", "frozen", "render"}:
        raise ValueError("Invalid input contract fields.")
    if type(value.get("version")) is not int or value["version"] != _INPUT_CONTRACT_VERSION:
        raise ValueError("Input contract version must be 1.")
    raw = value.get("raw", [])
    tables = value.get("tables", [])
    frozen = value.get("frozen", [])
    render = value.get("render", {})
    if not isinstance(raw, list) or len(raw) > 8:
        raise ValueError("Input contract raw bindings must be a list of at most eight names.")
    if not isinstance(tables, list) or len(tables) > 8:
        raise ValueError("Input contract tables must be a list of at most eight entries.")
    if not isinstance(frozen, list) or len(frozen) > 8:
        raise ValueError("Input contract frozen tables must be a list of at most eight names.")
    if not isinstance(render, dict) or len(render) > 8:
        raise ValueError("Input contract render mapping must be an object of at most eight entries.")
    normalized = {"version": _INPUT_CONTRACT_VERSION, "raw": [], "tables": [],
                  "frozen": [], "render": {}}
    names: set[str] = set()
    for item in raw:
        name = _input_name(item, "raw binding")
        if name in names or name in _INPUT_RESERVED:
            raise ValueError("Input contract names must be unique and not reserved.")
        names.add(name)
        normalized["raw"].append(name)
    for entry in tables:
        if not isinstance(entry, dict) or set(entry) - {"name", "input", "format", "sheet", "header"}:
            raise ValueError("Invalid input contract table entry.")
        name = _input_name(entry.get("name"), "table")
        source = _input_name(entry.get("input"), "table source")
        fmt = entry.get("format")
        if fmt not in _INPUT_TABLE_FORMATS:
            raise ValueError("Input contract table format must be declared as csv, tsv, excel or dataset.")
        if name in names or name in _INPUT_RESERVED:
            raise ValueError("Input contract names must be unique and not reserved.")
        names.add(name)
        item = {"name": name, "input": source, "format": fmt}
        if "sheet" in entry:
            sheet = entry["sheet"]
            if fmt != "excel" or (type(sheet) is not int and not isinstance(sheet, str)):
                raise ValueError("Input contract sheet is only valid for a named Excel table.")
            if isinstance(sheet, str) and (not sheet or len(sheet) > 256
                                           or any(ord(char) < 32 for char in sheet)):
                raise ValueError("Input contract sheet name is invalid.")
            item["sheet"] = sheet
        header = entry.get("header", "infer")
        if header != "infer" and (type(header) is not int or header < 0):
            raise ValueError("Input contract header must be 'infer' or a nonnegative row index.")
        if fmt == "dataset" and "header" in entry:
            raise ValueError("Dataset tables do not accept header options.")
        if fmt != "dataset":
            item["header"] = header
        normalized["tables"].append(item)
    for item in frozen:
        name = _input_name(item, "frozen table", binding=False)
        if name in names:
            raise ValueError("Input contract names must be unique.")
        names.add(name)
        normalized["frozen"].append(name)
    for key, source in render.items():
        render_name = _input_name(key, "render", binding=False)
        _input_name(source, "render source", binding=False)
        if source in normalized["render"].values():
            raise ValueError("Input contract render sources must be unique.")
        normalized["render"][render_name] = source
    return normalized


def _read_declared_input(path: Path, allowed: set) -> bytes:
    """Read one declared input with the same bounded, no-symlink discipline."""
    import stat
    candidate = Path(path)
    if candidate.resolve() not in allowed:
        raise ValueError("Declared input is not bound by the job.")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    before = candidate.lstat()
    with os.fdopen(os.open(candidate, flags), "rb") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not os.path.samestat(before, info) or info.st_size > _INPUT_MAX_BYTES):
            raise ValueError("Declared input is not a bounded regular file.")
        data = handle.read(_INPUT_MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        if (len(data) != info.st_size or after.st_size != info.st_size
                or after.st_mtime_ns != info.st_mtime_ns or after.st_ctime_ns != info.st_ctime_ns):
            raise ValueError("Declared input changed during reading.")
    return data


def _ingest_table(data: bytes, fmt: str, *, sheet=None, header="infer"):
    """Ingest one explicitly declared table; the format is never inferred."""
    import io as _io
    if fmt not in _INPUT_TABLE_FORMATS:
        raise ValueError("Input contract table format is not supported.")
    if fmt == "dataset":
        if sheet is not None or header != "infer":
            raise ValueError("Dataset tables do not accept sheet or header options.")
        try:
            from src.figures.dataset_codec import decode_frame
            from src.figures.script_outputs import strict_json
        except ImportError:
            from figures.dataset_codec import decode_frame
            from figures.script_outputs import strict_json
        try:
            return decode_frame(strict_json(data))
        except Exception:
            raise ValueError("Declared dataset table input could not be ingested.") from None
    import pandas as pd
    options = {"header": header}
    try:
        if fmt == "csv":
            return pd.read_csv(_io.BytesIO(data), **options)
        if fmt == "tsv":
            return pd.read_csv(_io.BytesIO(data), sep=chr(9), **options)
        if header == "infer":
            options["header"] = 0
        return pd.read_excel(_io.BytesIO(data),
                             sheet_name=sheet if sheet is not None else 0, **options)
    except Exception:
        raise ValueError("Declared " + fmt + " table input could not be ingested.") from None


def _contract_namespace(job: dict, contract: dict, paths: dict) -> dict:
    """Build the opt-in namespace; legacy names and behavior stay unchanged."""
    replay = bool(job.get("output_contract") and job.get("output_key"))
    namespace: dict = {"__name__": "__citrusfig__"}
    if replay:
        return namespace
    allowed = {path.resolve() for path in paths.values()}
    for name in contract["raw"]:
        if name not in paths:
            raise ValueError("Input contract raw binding is not bound by the job.")
        namespace[name] = paths[name]
    for entry in contract["tables"]:
        if entry["input"] not in paths:
            raise ValueError("Input contract table source is not bound by the job.")
        try:
            data = _read_declared_input(paths[entry["input"]], allowed)
        except OSError:
            raise ValueError("Declared input bytes could not be read.") from None
        # _ingest_table owns a generic, non-echoing diagnostic for every
        # backend failure; no pandas or file detail escapes this boundary.
        namespace[entry["name"]] = _ingest_table(
            data, entry["format"], sheet=entry.get("sheet"),
            header=entry.get("header", "infer"))
    return namespace


def _bootstrap_engine_path() -> tuple[Path, Path | None]:
    """Make the staged figure engine importable and return its roots.

    A sandbox image also contains an older ``citrus_figures`` distribution in
    site-packages.  ``python -I`` deliberately ignores ``PYTHONPATH``, so the
    staged directory has to be placed on ``sys.path`` explicitly.  Returns
    ``(engine_root, support_root)`` and fails closed when no staged engine is
    present instead of silently importing the image's copy.
    """
    candidates = []
    support_root = None
    root = os.environ.get("CITRUS_FIGURES_ROOT", "").strip()
    if root:
        source = Path(root)
        candidates.append(source)
        # LocalBackend points at the trusted service checkout. Its subprocess
        # must pair that checkout with its support source during development.
        packages = source.parent / "packages"
        if (packages / "citrus_figures" / "__init__.py").is_file():
            sys.path.insert(0, str(packages))
            support_root = packages
        elif (source / "citrus_figures" / "__init__.py").is_file():
            support_root = source
    here = Path(__file__).resolve().parent
    candidates.extend([here.parent, here])
    for candidate in candidates:
        if (candidate / "src" / "figures" / "core.py").is_file() \
                or (candidate / "figures" / "core.py").is_file():
            text = str(candidate)
            if text not in sys.path:
                sys.path.insert(0, text)
            if support_root is None and (candidate / "citrus_figures" / "__init__.py").is_file():
                support_root = candidate
            if not root and support_root is None:
                raise RuntimeError("figure_support_root_not_found")
            return candidate, support_root
    raise RuntimeError("figure_engine_root_not_found")


def _verify_support_origin(engine_root: Path, support_root: Path | None) -> None:
    """Reject a worker that would execute an image's stale support package."""
    if support_root is None:
        return
    import citrus_figures  # noqa: PLC0415

    origin = Path(citrus_figures.__file__).resolve()
    if not origin.is_relative_to(support_root.resolve()):
        raise RuntimeError(
            "figure_support_origin_mismatch: expected staged support under "
            + str(support_root) + ", got " + str(origin))
    manifest = engine_root / "support-manifest.json"
    if os.environ.get("CITRUS_FIGURES_ROOT", "").strip() or not manifest.is_file():
        return
    record = json.loads(manifest.read_text(encoding="utf-8"))
    members = record.get("members")
    if not isinstance(members, dict) or not members:
        raise RuntimeError("figure_support_manifest_invalid")
    for name, digest in members.items():
        path = engine_root / name
        if (not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise RuntimeError("figure_support_manifest_mismatch: " + name)


def _engine():
    """Import the figure engine under whichever name is available.

    In the service the engine is ``src.figures``; in the sandbox image it is
    ``figures``. Both are handled so the worker's body is identical in the
    two places.
    """
    try:
        from src.figures import core, exporter, manifest  # noqa: PLC0415
        return core, exporter, manifest
    except ImportError:
        from figures import core, exporter, manifest  # noqa: PLC0415
        return core, exporter, manifest


def _emit(payload: dict) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.flush()
    return EXIT_OK


# The failure messages the model reads. They name the problem and the fix,
# because a model that knows what went wrong can write a working script on
# the next attempt — and a model that only sees "exit code 11" cannot.
FAILURE_TEXT = {
    EXIT_NO_FIGURE: "[FIGURE_NO_FIGURE] 脚本没有生成任何图。请确保脚本最后留下一个 Figure（例如调用 plt.subplots() 并不调用 plt.close()）。",
    EXIT_MULTIPLE_FIGURES: "[FIGURE_MULTIPLE_FIGURES] 脚本生成了多张图，无法判断该用哪一张。请只保留一张图（用 plt.close('all') 关闭其余的）。",
    EXIT_FORBIDDEN: "[FIGURE_SCRIPT_FORBIDDEN] 脚本尝试了被禁止的操作（联网、创建子进程或导入敏感模块）。绘图脚本只能使用本地数据与绘图库。",
    EXIT_OUTPUT_TOO_LARGE: "[FIGURE_SCRIPT_OUTPUT] 脚本的输出超过体积上限。",
}


def _report(code: int, detail: str = "", script_stdout: str = "",
            nature_skill_receipt: dict | None = None, *, diagnostic: dict | None = None) -> int:
    """Report a failure and return the exit code.

    The message goes to stdout as JSON as well as to stderr as text. The
    parent reads the JSON, because stderr also carries warnings and
    tracebacks, and "the first line of stderr" is not a reliable way to
    find the explanation. Whatever the script printed is carried along, so
    a script that printed its own diagnosis is not silenced by the failure.
    """
    text = FAILURE_TEXT.get(code, "[FIGURE_SCRIPT_ERROR] 绘图脚本执行失败。")
    if detail:
        text = f"{text}\n{detail}"
    try:
        sys.stdout.write(json.dumps({"ok": False, "code": code, "message": text,
                                     "script_stdout": str(script_stdout or "")[:4000],
                                     **({"diagnostic": diagnostic} if diagnostic else {}),
                                     **({"nature_skill_receipt": nature_skill_receipt}
                                        if nature_skill_receipt is not None else {})},
                                    ensure_ascii=False))
        sys.stdout.flush()
    except Exception:
        pass
    sys.stderr.write(text + "\n")
    return code


def _promote_current_qa(manifest: dict, container=None, *, receipt=None, layout=None) -> None:
    """Expose this export's current-byte QA evidence at the manifest top level.

    ``render_receipt`` keeps the same nested evidence for callers that only
    transport an acknowledgement; a current-byte reaudit accepts either
    location. Nothing is recomputed or rerendered here.
    """
    if isinstance(container, dict):
        receipt = container.get("current_qa_receipt", receipt)
        layout = container.get("current_qa_layout", layout)
    if isinstance(receipt, dict):
        manifest["current_qa_receipt"] = receipt
    if isinstance(layout, dict):
        manifest["current_qa_layout"] = layout


def _check_script(script: str) -> tuple[str, str, int | None, int | None] | None:
    """Refuse a script that imports a forbidden module.

    A plain textual check: it does not pretend to be a security boundary
    (the container is that), but it turns an obvious escape attempt into a
    clear refusal instead of an opaque traceback.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return f"脚本语法错误：{exc.msg}", "script_syntax", exc.lineno, exc.offset
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            root = str(name).split(".", 1)[0]
            if root in _FORBIDDEN_MODULES:
                return f"脚本导入了被禁止的模块「{root}」。", "forbidden_import", node.lineno, node.col_offset + 1
    return None


def _guard_imports(script: str) -> str | None:
    issue = _check_script(script)
    return issue[0] if issue else None


def _script_diagnostic(stage, error_code, line=None, bindings=None, *, column=None):
    diagnostic = {"stage": stage, "error_code": error_code}
    if type(line) is int and line > 0:
        diagnostic["line"] = line
    if type(column) is int and column > 0:
        diagnostic["column"] = column
    if bindings:
        diagnostic["inputs"] = bindings
    return diagnostic


def _script_bindings(namespace):
    from pandas import DataFrame
    return {name: "Path" if isinstance(value, Path) else "DataFrame"
            for name, value in namespace.items() if isinstance(value, (Path, DataFrame))}


def _resolve(base: Path, relative: str) -> Path:
    """Resolve a job path, refusing anything outside the job directory."""
    candidate = (base / relative).resolve()
    root = base.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"路径越界：{relative}")
    return candidate


def run(job_path: Path) -> int:
    engine_root, support_root = _bootstrap_engine_path()
    _verify_support_origin(engine_root, support_root)
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"job.json 无法读取：{exc}\n")
        return EXIT_SCRIPT_ERROR

    # /work/in and /work/out are siblings: the input directory holds the job
    # and the inputs, the output directory holds what the job produces.
    in_dir = job_path.parent
    out_dir = in_dir.parent / "out"

    script_text = str(job.get("script") or "")
    if not script_text.strip():
        sys.stderr.write("作业没有提供脚本。\n")
        return EXIT_SCRIPT_ERROR

    issue = _check_script(script_text)
    if issue:
        message, error_code, line, column = issue
        exit_code = EXIT_SCRIPT_ERROR if error_code == "script_syntax" else EXIT_FORBIDDEN
        return _report(exit_code, message,
                       diagnostic=_script_diagnostic("analysis", error_code, line, column=column))

    # The inputs the script may read, bound to names the script chooses.
    try:
        input_contract = normalize_input_contract(job.get("input_contract"))
    except (ValueError, TypeError) as exc:
        return _report(EXIT_FORBIDDEN, str(exc),
                       diagnostic=_script_diagnostic("input", "invalid_input_contract"))
    paths: dict[str, Path] = {}
    for name, relative in (job.get("inputs") or {}).items():
        try:
            path = _resolve(in_dir, str(relative))
        except ValueError as exc:
            sys.stderr.write(str(exc) + "\n")
            return EXIT_FORBIDDEN
        if not path.is_file():
            return _report(EXIT_SCRIPT_ERROR, "脚本原始输入文件缺失，不能重放。")
        expected = (job.get("expected_input_sha256") or {}).get(str(relative))
        if expected:
            import hashlib
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected:
                return _report(EXIT_FORBIDDEN, "脚本输入快照摘要不匹配，拒绝重放。")
        paths[str(name)] = path
    try:
        if input_contract:
            namespace = _contract_namespace(job, input_contract, paths)
        else:
            namespace = {"__name__": "__citrusfig__", **paths}
    except (ValueError, OSError) as exc:
        return _report(EXIT_FORBIDDEN, str(exc),
                       diagnostic=_script_diagnostic("input", "input_binding_failed"))

    core, exporter, manifest_module = _engine()
    try:
        if core.__name__.startswith("src."):
            from src.figures.render_policy import render_mode
        else:
            from figures.render_policy import render_mode
        mode = render_mode(job.get("render_mode", "initial"))
        if mode != "initial" and not job.get("replay_overrides"):
            raise ValueError("manual render mode requires host replay")
    except ValueError as exc:
        return _report(EXIT_FORBIDDEN, str(exc))

    if job.get("output_contract"):
        namespace["load_dataset"] = _dataset_loader(tuple(paths.values()))

    if job.get("output_contract"):
        return _run_outputs(job, script_text, namespace, out_dir, core, exporter,
                            manifest_module, inputs_contract=input_contract, paths=paths)

    journal_strict = (job.get("qa_profile") or {}).get("journal_strict") is True
    result = core.capture_figure(script_text, namespace=namespace,
                                 check_layout=mode == "initial" and journal_strict)
    if result.error:
        if result.error_type == "no_figure":
            return _report(EXIT_NO_FIGURE, script_stdout=result.stdout)
        return _report(EXIT_SCRIPT_ERROR,
                       result.stdout[-2000:] + "\n" + result.error,
                       script_stdout=result.stdout,
                       diagnostic=_script_diagnostic("analysis", result.error_type,
                           result.script_line, _script_bindings(namespace)))
    if result.figures_seen > 1:
        return _report(EXIT_MULTIPLE_FIGURES,
                       f"脚本生成了 {result.figures_seen} 张图。")

    figure = result.figure
    if mode == "initial" and not journal_strict:
        from citrus_figures.styling import prepare_figure_fonts
        prepare_figure_fonts(figure)
    axes = list(getattr(figure, "axes", []) or []) if figure is not None else []
    rendered = manifest_module.assign_locators(
        core.RenderedFigure(figure=figure, axes=axes, qa_profile=job.get("qa_profile") or {}))
    manifest = manifest_module.build_manifest(rendered, source="script", include_qa=False)
    applied = []
    accepted = []
    if job.get("replay_overrides"):
        try:
            # The host never receives Python objects. Artist mutation happens
            # here, after capture and against this run's actual object list.
            if core.__name__.startswith("src."):
                from src.figures.overrides import validate_overrides, apply_overrides, merge_overrides
            else:
                from figures.overrides import validate_overrides, apply_overrides, merge_overrides
            incoming = validate_overrides(job.get("overrides") or [], manifest)
            base = validate_overrides(job.get("base_overrides") or [], manifest)
            accepted = validate_overrides(merge_overrides(base, incoming), manifest)
            applied = apply_overrides(rendered, manifest, accepted)
            manifest = manifest_module.build_manifest(rendered, source="script", include_qa=False)
        except Exception as exc:
            return _report(EXIT_SCRIPT_ERROR, f"样式修改被拒绝：{exc}")

    outputs = job.get("outputs") or {}
    formats = []
    for relative in outputs.values():
        suffix = Path(str(relative)).suffix.lower().lstrip(".")
        if suffix in {"svg", "png", "pdf", "tiff"} and suffix not in formats:
            formats.append(suffix)
    if not formats:
        formats = ["svg"]

    try:
        if core.__name__.startswith("src."):
            from src.figures.render_policy import export_figure
        else:
            from figures.render_policy import export_figure
        blobs, nature_receipt, render_receipt = export_figure(rendered, mode=mode, exporter=exporter,
            formats=formats, dpi=int(job.get("dpi") or 300), source=script_text)
    except Exception as exc:
        receipt = getattr(exc, "receipt", None)
        current = getattr(exc, "current_qa_receipt", None)
        _promote_current_qa(manifest, receipt=current,
                            layout=getattr(exc, "current_qa_layout", None))
        if receipt is not None:
            manifest["nature_skill_receipt"] = receipt
        if receipt is not None or current is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            data = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(data) <= 4 * 1024 * 1024:
                with (out_dir / "objects.json").open("xb") as handle:
                    handle.write(data)
        return _report(EXIT_SCRIPT_ERROR, f"导出或图件审计失败：{exc}",
                       script_stdout=result.stdout, nature_skill_receipt=receipt)

    total = 0
    written: dict[str, int] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for logical, relative in outputs.items():
        suffix = Path(str(relative)).suffix.lower().lstrip(".")
        blob = blobs.get(suffix)
        if blob is None:
            continue
        try:
            path = _resolve(out_dir, str(relative))
        except ValueError as exc:
            sys.stderr.write(str(exc) + "\n")
            return EXIT_FORBIDDEN
        if total + len(blob) > _MAX_OUTPUT_BYTES:
            return _report(EXIT_OUTPUT_TOO_LARGE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        total += len(blob)
        written[str(logical)] = len(blob)

    manifest = manifest_module.build_manifest(rendered, source="script", include_qa=mode != "preview")
    manifest["render_receipt"] = render_receipt
    _promote_current_qa(manifest, render_receipt)
    if nature_receipt is not None:
        manifest["nature_skill_receipt"] = nature_receipt
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
    override_bytes = json.dumps(accepted, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if (len(manifest_bytes) > 4 * 1024 * 1024 or len(override_bytes) > 256 * 1024
            or total + len(manifest_bytes) + len(override_bytes) > _MAX_OUTPUT_BYTES):
        return _report(EXIT_OUTPUT_TOO_LARGE)
    (out_dir / "objects.json").write_bytes(manifest_bytes)
    if job.get("replay_overrides"):
        (out_dir / "overrides.json").write_bytes(override_bytes)

    report = {
        "ok": True,
        "stdout": result.stdout[-4000:],
        "outputs": written,
        "figure_count": result.figures_seen,
    }
    if outputs.get("manifest") == "objects.json":
        report["manifest_file"] = "objects.json"
    else:
        report["manifest"] = manifest
    if job.get("replay_overrides"):
        report.update(override_protocol=1, applied_count=len(applied))
    return _emit(report)


def _dataset_loader(inputs):
    """Container-only helper: decode a bounded, declared read-only input."""
    allowed = {path.resolve() for path in inputs if isinstance(path, Path)}

    def load_dataset(path):
        import stat
        candidate = Path(path)
        if candidate.resolve() not in allowed:
            raise ValueError("load_dataset only accepts a declared input Path.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        before = candidate.lstat()
        with os.fdopen(os.open(candidate, flags), "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not os.path.samestat(before, info) or info.st_size > 64 * 1024 * 1024):
                raise ValueError("Dataset input is not a bounded regular file.")
            data = handle.read(64 * 1024 * 1024 + 1)
            after = os.fstat(handle.fileno())
            if (len(data) != info.st_size or after.st_size != info.st_size
                    or after.st_mtime_ns != info.st_mtime_ns or after.st_ctime_ns != info.st_ctime_ns):
                raise ValueError("Dataset input changed during reading.")
        try:
            from src.figures.dataset_codec import decode_frame
            from src.figures.script_outputs import strict_json
        except ImportError:
            from figures.dataset_codec import decode_frame
            from figures.script_outputs import strict_json
        return decode_frame(strict_json(data))

    return load_dataset


def _run_outputs(job, script_text, namespace, out_dir, core, exporter, manifest_module,
                 *, inputs_contract=None, paths=None) -> int:
    """v2: execute to completion, then serialize each host-declared slot."""
    import hashlib
    if core.__name__.startswith("src."):
        from src.figures.script_outputs import (RenderScriptEmitUnavailable,
                                               normalize_output_contract, output_slots,
                                               validate_receipt, MAX_RECEIPT_BYTES)
    else:
        from figures.script_outputs import (RenderScriptEmitUnavailable,
                                           normalize_output_contract, output_slots,
                                           validate_receipt, MAX_RECEIPT_BYTES)
    try:
        contract = normalize_output_contract(job["output_contract"])
        target = job.get("output_key", "")
        slots = output_slots(contract, output_key=target)
        if slots != job.get("outputs") or (job.get("replay_overrides") and not target):
            raise ValueError("Output slots or replay target do not match the host contract.")
        separated = [entry for entry in contract["figures"] if "render_script" in entry
                     and (not target or entry["name"] == target)]
        for entry in separated:
            issue = _check_script(entry["render_script"])
            if issue:
                message, error_code, line, column = issue
                exit_code = EXIT_SCRIPT_ERROR if error_code == "script_syntax" else EXIT_FORBIDDEN
                return _report(exit_code, message,
                               diagnostic=_script_diagnostic("render", error_code, line, column=column))
        frozen_tables = None
        render_mapping = None
        if inputs_contract:
            if target and separated:
                required = set(separated[0]["parents"])
                if inputs_contract["raw"] or inputs_contract["tables"]:
                    raise ValueError("Frozen replay accepts only declared frozen tables.")
                if set(inputs_contract["frozen"]) != required:
                    raise ValueError("Input contract frozen tables must match the figure parents.")
                if (not inputs_contract["render"]
                        or set(inputs_contract["render"].values()) != required):
                    raise ValueError("Input contract render mapping must cover the figure parents.")
                bound = paths or {}
                if any(name not in bound for name in required):
                    raise ValueError("Input contract frozen table is not bound by the job.")
                load = _dataset_loader(tuple(bound[name] for name in required))
                frozen_tables = {name: load(bound[name]) for name in required}
                render_mapping = dict(inputs_contract["render"])
            else:
                if inputs_contract["frozen"]:
                    raise ValueError("Frozen tables require a separated render replay.")
                render_mapping = dict(inputs_contract["render"]) or None
            parents = {name for entry in separated for name in entry["parents"]}
            if render_mapping is not None and set(render_mapping.values()) != parents:
                raise ValueError("Input contract render mapping must cover the figure parents.")
        elif target and separated:
            required = set(separated[0]["parents"])
            supplied = set(namespace) - {"__name__", "load_dataset"}
            if supplied != required or any(not isinstance(namespace[name], Path) for name in required):
                raise ValueError("Frozen replay requires exactly the declared parent table Paths.")
            load = _dataset_loader(tuple(namespace[name] for name in required))
            frozen_tables = {name: load(namespace[name]) for name in required}
    except (ValueError, TypeError, OSError) as exc:
        if isinstance(exc, RenderScriptEmitUnavailable):
            return _report(EXIT_SCRIPT_ERROR, f"[RENDER_SCRIPT_EMIT_NOT_AVAILABLE] {exc}")
        return _report(EXIT_FORBIDDEN, str(exc))
    result = core.capture_outputs(script_text, namespace=namespace,
                                  output_contract=contract, output_key=target,
                                  frozen_tables=frozen_tables,
                                  render_mapping=render_mapping)
    if result.error:
        inputs = ({"data": "DataFrame mapping"} if result.error_stage == "render"
                  else _script_bindings(namespace))
        return _report(EXIT_SCRIPT_ERROR, result.error, script_stdout=result.stdout,
                       diagnostic={**_script_diagnostic(result.error_stage, result.error_type,
                                                        result.script_line, inputs), **result.error_details})

    states = {key: {"status": "missing", "error": "Output was not emitted."}
              for key in slots if key != "result"}
    total = 0
    applied_count = 0
    out_dir.mkdir(parents=True, exist_ok=True)

    def error(key, exc):
        # Bound worst-case UTF-8 across all 57 states plus script stdout.
        code = getattr(exc, "code", None)
        detail = f"[{code}] {exc}" if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code) else str(exc)
        states[key] = {"status": "error", "error": detail[:160]}

    def write(key, blob, limit=_MAX_OUTPUT_BYTES):
        nonlocal total
        if len(blob) > limit or total + len(blob) + MAX_RECEIPT_BYTES > _MAX_OUTPUT_BYTES:
            raise ValueError("Output exceeds the worker byte budget.")
        # Exclusive create refuses files/symlinks planted by the script. The
        # host independently validates file handles and content digests.
        with (out_dir / slots[key]).open("xb") as handle:
            handle.write(blob)
        total += len(blob)
        states[key] = {"status": "ok", "size": len(blob),
                       "sha256": hashlib.sha256(blob).hexdigest()}

    def write_json(key, value, limit=_MAX_OUTPUT_BYTES):
        blob = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        write(key, blob, limit)

    if not target:
        for name, frame in result.tables.items():
            key = f"table:{name}"
            try:
                if core.__name__.startswith("src."):
                    from src.figures.dataset_codec import encode_frame
                else:
                    from figures.dataset_codec import encode_frame
                write_json(key, encode_frame(frame))
            except Exception as exc:
                error(key, exc)
        if result.report_emitted:
            try:
                write_json("report", result.report, 256 * 1024)
            except Exception as exc:
                error("report", exc)

    for entry in contract["figures"]:
        name = entry["name"]
        if target and target != name:
            continue
        if name not in result.figures:
            continue
        prefix = f"figure:{name}:"
        try:
            figure = result.figures[name]
            from matplotlib.figure import Figure
            if not isinstance(figure, Figure):
                raise ValueError("emit.figure requires a Matplotlib Figure.")
            # Object-oriented Figure() does not acquire an Agg canvas until
            # explicitly bound; pyplot-managed figures already have one.
            if not callable(getattr(figure.canvas, "get_renderer", None)):
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                FigureCanvasAgg(figure)
            mode = job.get("render_mode", "initial")
            frozen_receipt = result.frozen_render_receipts.get(name)
            if mode == "initial" or frozen_receipt is not None:
                if core.__name__.startswith("src."):
                    from src.figures.styling import prepare_figure_fonts
                else:
                    from figures.styling import prepare_figure_fonts
                prepare_figure_fonts(figure)
            journal_strict = (job.get("qa_profile") or {}).get("journal_strict") is True
            problems = core._tick_label_problems(figure, strict_boxes=False) if mode == "initial" and journal_strict else []
            if problems:
                raise ValueError("完整刻度标签重叠或超出固定画布：" + ", ".join(problems)
                                 + "；请沿用 figure-export/nature-figure 工作流修正布局，保留完整数据。")
            rendered = manifest_module.assign_locators(core.RenderedFigure(
                figure=figure, axes=list(figure.axes), qa_profile=job.get("qa_profile") or {}))
            manifest = manifest_module.build_manifest(rendered, source="script", include_qa=False)
            accepted = []
            replay_proof = None
            if frozen_receipt is not None:
                from citrus_figures.style_replay import replay_baseline
                frozen_receipt = {**frozen_receipt, "style_replay": replay_baseline(figure, manifest["objects"])}
            if job.get("replay_overrides"):
                from citrus_figures.style_replay import (REPLAY_ERRORS, coordinate_sha256,
                    replay_mismatch, typed_payload_sha256, valid_baseline)
                from citrus_figures.overrides import OverrideError
                expected = job.get("expected_frozen_receipt")
                baseline = (frozen_receipt or {}).get("style_replay")
                refusal = (replay_mismatch(expected, frozen_receipt) if expected is not None
                           else None if valid_baseline(baseline) else "replay_evidence_unavailable")
                if refusal:
                    raise OverrideError(refusal, REPLAY_ERRORS[refusal])
                if core.__name__.startswith("src."):
                    from src.figures.overrides import validate_overrides, apply_overrides, merge_overrides
                else:
                    from figures.overrides import validate_overrides, apply_overrides, merge_overrides
                incoming = validate_overrides(job.get("overrides") or [], manifest)
                base = validate_overrides(job.get("base_overrides") or [], manifest)
                accepted = validate_overrides(merge_overrides(base, incoming), manifest)
                applied = apply_overrides(rendered, manifest, accepted)
                if len(applied) != len(accepted):
                    raise OverrideError("override_acknowledgement_invalid", "工作器未应用全部样式修改。")
                replay_proof = {"coordinates_sha256": baseline["coordinates_sha256"],
                                "overrides_sha256": typed_payload_sha256(accepted)}
            manifest = manifest_module.build_manifest(rendered, source="script", include_qa=False)
            if frozen_receipt is not None:
                manifest["frozen_render_receipt"] = frozen_receipt
            try:
                if core.__name__.startswith("src."):
                    from src.figures.render_policy import export_figure
                else:
                    from figures.render_policy import export_figure
                blobs, nature_receipt, render_receipt = export_figure(rendered, mode=mode, exporter=exporter,
                    formats=entry["formats"], dpi=int(job.get("dpi") or 300),
                    source=entry.get("render_script", script_text), allow_format_failure=True)
            except Exception as exc:
                receipt = getattr(exc, "receipt", None)
                current = getattr(exc, "current_qa_receipt", None)
                _promote_current_qa(manifest, receipt=current,
                                    layout=getattr(exc, "current_qa_layout", None))
                if receipt is not None:
                    manifest["nature_skill_receipt"] = receipt
                if receipt is not None or current is not None:
                    write_json(prefix + "manifest", manifest, 4 * 1024 * 1024)
                    if job.get("replay_overrides"):
                        write_json(prefix + "overrides", accepted, 256 * 1024)
                raise
            manifest = manifest_module.build_manifest(rendered, source="script", include_qa=mode != "preview")
            if replay_proof is not None:
                if coordinate_sha256(figure) != replay_proof["coordinates_sha256"]:
                    raise OverrideError("override_data_changed", REPLAY_ERRORS["override_data_changed"])
                manifest["style_replay_proof"] = replay_proof
                applied_count += len(accepted)
            if frozen_receipt is not None:
                manifest["frozen_render_receipt"] = frozen_receipt
            manifest["render_receipt"] = render_receipt
            _promote_current_qa(manifest, render_receipt)
            if nature_receipt is not None:
                manifest["nature_skill_receipt"] = nature_receipt
            write_json(prefix + "manifest", manifest, 4 * 1024 * 1024)
            if job.get("replay_overrides"):
                write_json(prefix + "overrides", accepted, 256 * 1024)
        except Exception as exc:
            for key in slots:
                if key.startswith(prefix) and states[key]["status"] != "ok":
                    error(key, exc)
            continue
        for fmt in entry["formats"]:
            key = prefix + fmt
            try:
                if fmt not in blobs:
                    detail = render_receipt.get("export_errors", {}).get(fmt, {})
                    raise ValueError(detail.get("error") or "Requested export was not produced.")
                write(key, blobs[fmt])
            except Exception as exc:
                error(key, exc)

    partial = any(state["status"] == "error" or
                  (state["status"] == "missing" and key != "report" and not key.endswith(":overrides"))
                  for key, state in states.items())
    receipt = {"version": 2, "ok": True, "status": "partial" if partial else "ok",
               "job_id": job.get("job_id", ""), "output_key": target, "slots": states,
               "stdout": result.stdout[-4000:], "figure_count": len(result.figures),
               "applied_count": applied_count}
    data = json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode("utf-8")
    try:
        validate_receipt(data, contract, job_id=job.get("job_id", ""), output_key=target)
        with (out_dir / "result.json").open("xb") as handle:
            handle.write(data)
    except (OSError, ValueError, TypeError) as exc:
        return _report(EXIT_FORBIDDEN, "Output receipt could not be committed: " + str(exc))
    # stdout is diagnostic only. The host collects the bounded fixed receipt.
    return _emit({"ok": True, "version": 2, "status": receipt["status"], "result_file": "result.json"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CitrusScalor figure sandbox worker")
    parser.add_argument("--job", required=True, help="path to job.json")
    args = parser.parse_args(argv)
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        return run(Path(args.job))
    except Exception as exc:  # noqa: BLE001 - the worker reports, never crashes
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return EXIT_SCRIPT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
