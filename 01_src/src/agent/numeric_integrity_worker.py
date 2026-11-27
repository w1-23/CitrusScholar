"""Host-only adapter for the distinct Research Integrity numeric operation."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re

from src.code_execution.execution_bridge import identify
from src.code_execution.presence import worker_available
from src.code_execution.protocol import ExecutionLimits, ExecutionRequest
from src.core.account_leases import account_lease, acquire_account_lease
from src.research.numeric_integrity.contract import (REPORT_SCHEMA, REQUEST_SCHEMA, SHA, decode_request)
from src.research.numeric_integrity.scanner import PAPERCONAN_COMMIT, PINS
from src.session import files

from .code_tools import _accepted_file, _deadline, _key, _public, _service

SCOPE = "citrus.numeric-integrity/1"
ROOT = "/input/numeric-integrity/"
REQUEST = ROOT + "request.json"
SCRIPT = "/workspace/numeric-integrity/launcher.py"
RESULT = "/outputs/numeric-integrity/report.json"
LAUNCHER = Path(__file__).resolve().parents[3] / "02_config/integrity/launcher.py"
LIMITS = ExecutionLimits(timeout_sec=90, memory_mb=2048, cpu_millis=1000, pids=64,
                         scratch_bytes=32 << 20, log_bytes=32 << 10,
                         output_bytes=4 << 20, output_files=1)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def _source_ref(identity, file_id, digest):
    if (type(file_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", file_id)
            or type(digest) is not str or not SHA.fullmatch(digest)):
        raise ValueError("numeric_file_reference_invalid")
    ref = files.get_task_input_file(identity.owner_user_id, identity.run_id, file_id, digest)
    if (ref is None or ref.get("sha256") != digest or ref.get("visibility") != "public"
            or ref.get("origin") not in files.ORIGINS):
        raise PermissionError("numeric_file_not_admitted_to_run")
    return ref


def _prepare(identity, sources, enable_benford):
    if type(sources) not in (list, tuple) or not 1 <= len(sources) <= 4:
        raise ValueError("numeric_explicit_admitted_source_refs_required")
    if type(enable_benford) is not bool:
        raise ValueError("numeric_benford_flag_invalid")
    descriptors = []
    for source in sources:
        if type(source) is not dict or not {"file_id", "sha256"} <= source.keys() <= {"file_id", "sha256", "benford_columns"}:
            raise ValueError("numeric_file_reference_invalid")
        file_id, digest = source["file_id"], source["sha256"]
        ref = _source_ref(identity, file_id, digest)
        format_ = Path(ref["filename"]).suffix.lower().lstrip(".")
        descriptors.append({"id": file_id, "version": 1, "sha256": digest, "format": format_,
                            "size_bytes": ref["size_bytes"], "benford_columns": source.get("benford_columns", [])})
    raw = _encode({"schema": REQUEST_SCHEMA, "enable_benford": enable_benford, "sources": descriptors})
    parsed, _ = decode_request(raw)
    return raw, parsed


def _register(identity, path, data):
    digest = _digest(data)
    row = files.register_file(owner_user_id=identity.owner_user_id, session_id=identity.session_id,
                              filename=Path(path).name, content=data,
                              client_request_id=_key(SCOPE, identity.run_id, path, digest),
                              origin=files.ORIGIN_UPLOADED, visibility="internal", producer_tool="numeric_integrity")
    if row.get("sha256") != digest or row.get("size_bytes") != len(data):
        raise ValueError("numeric_internal_snapshot_mismatch")
    return row["file_id"]


def _fixed_release():
    release = os.environ.get("CITRUS_WORKER_RELEASE", "")
    if not SHA.fullmatch(release):
        raise PermissionError("numeric_pinned_worker_release_required")
    return release


def _authorize(identity):
    if identify(identity.owner_user_id, identity.session_id, identity.run_id) != identity:
        raise PermissionError("numeric_current_job_identity_required")


def _load_bound(service, identity, row, launcher, release):
    request = ExecutionRequest.parse(row["request"])
    if (request.identity != identity or request.task_id != request.operation_id
            or request.operation_id != request.idempotency_key or not request.operation_id.startswith("integrity_")
            or request.script.path != SCRIPT or request.script.sha256 != _digest(launcher)
            or request.arguments != () or request.outputs != (RESULT,) or request.network_policy != "offline"
            or request.environment_id != "base" or request.environment_revision != 1 or request.limits != LIMITS):
        raise PermissionError("numeric_execution_scope_denied")
    bindings = {entry.path: entry for entry in request.inputs}
    if REQUEST not in bindings:
        raise PermissionError("numeric_request_snapshot_missing")
    ref = bindings[REQUEST]
    meta = service.workspace.open(request.workspace_id)
    if (meta["task_id"] != request.task_id or meta["metadata"] != {
            "code_scope": SCOPE, "request_sha256": ref.sha256, "worker_release": release}):
        raise PermissionError("numeric_execution_release_mismatch")
    raw = service.workspace.read_bytes(request.workspace_id, path=REQUEST, version=ref.version,
                                       expected_sha256=ref.sha256)
    if len(raw) != ref.size_bytes or _digest(raw) != ref.sha256:
        raise ValueError("numeric_request_identity_mismatch")
    sources, _ = decode_request(raw)
    if bindings.keys() != {REQUEST, *(ROOT + source.staged_name for source in sources)}:
        raise PermissionError("numeric_execution_input_scope_mismatch")
    for source in sources:
        snapshot = bindings[ROOT + source.staged_name]
        live = _source_ref(identity, source.id, source.sha256)
        if (snapshot.file_id, snapshot.sha256, snapshot.size_bytes, snapshot.version, live["size_bytes"]) != (
                source.id, source.sha256, source.size_bytes, 1, source.size_bytes):
            raise ValueError("numeric_source_snapshot_mismatch")
    return request, ref.sha256, sources


def _bind(service, identity, task, workspace, raw, launcher, sources):
    wid, revision = workspace["workspace_id"], workspace["revision"]
    expected = {SCRIPT: (_digest(launcher), len(launcher), None), REQUEST: (_digest(raw), len(raw), None)}
    expected.update({ROOT + source.staged_name: (source.sha256, source.size_bytes, source.id) for source in sources})
    existing = {entry["path"]: entry for entry in service.workspace.snapshot(
        wid, task_id=task, expected_revision=revision)["files"]}
    if not existing.keys() <= expected.keys():
        raise PermissionError("numeric_workspace_unexpected_snapshot")
    for path, (digest, size, file_id) in sorted(expected.items()):
        if path in existing:
            item = existing[path]
            if (item["sha256"], item["size_bytes"], item["version"]) != (digest, size, 1) or file_id and item["file_id"] != file_id:
                raise PermissionError("numeric_workspace_snapshot_conflict")
            continue
        selected = file_id or _register(identity, path, launcher if path == SCRIPT else raw)
        result = service.workspace.bind(wid, path=path, file_id=selected, expected_revision=revision,
                                        expected_sha256=digest, source={"scope": SCOPE})
        revision = result["revision"]
    return revision, tuple(sorted(expected.keys() - {SCRIPT}))


def schedule_numeric_integrity(identity, *, operation_id, sources, enable_benford=False, regions=None):
    """Submit exact whole-source pins. No implicit lookup outside current attachments."""
    if regions is not None and regions != [] and regions != ():
        raise ValueError("numeric_bounded_regions_not_supported")
    if (type(operation_id) is not str or not 1 <= len(operation_id) <= 256
            or operation_id.strip() != operation_id or any(ord(ch) < 32 for ch in operation_id)):
        raise ValueError("numeric_operation_id_invalid")
    _authorize(identity)
    release = _fixed_release()
    launcher = LAUNCHER.read_bytes()
    service = _service(identity)
    task = "integrity_" + _key(identity.owner_user_id, identity.session_id, identity.user_turn_id, identity.run_id, operation_id)
    with account_lease(identity.owner_user_id):
        raw, parsed = _prepare(identity, sources, enable_benford)
        digest = _digest(raw)
        meta = {"code_scope": SCOPE, "request_sha256": digest, "worker_release": release}
        previous = [row for row in service.repository.run_executions(identity)
                    if row["task"] == row["operation"] == row["request_key"] == task]
        if len(previous) > 1:
            raise ValueError("numeric_duplicate_execution")
        if previous:
            row = previous[0]
        else:
            if not worker_available(service.repository.path, release=release):
                raise LookupError("numeric_pinned_worker_unavailable")
            deadline = _deadline(identity)
            workspace = service.workspace.create(task_id=task, request_id=task, make_current=False, metadata=meta)
            if workspace["metadata"] != meta:
                raise ValueError("numeric_operation_content_conflict")
            revision, paths = _bind(service, identity, task, workspace, raw, launcher, parsed)
            row = service.submit(task_id=task, workspace_id=workspace["workspace_id"], expected_revision=revision,
                                 script_path=SCRIPT, inputs=paths, outputs=(RESULT,), operation_id=task,
                                 idempotency_key=task, network_policy="offline", limits=LIMITS, absolute_deadline=deadline)
        request, actual, _ = _load_bound(service, identity, row, launcher, release)
        if actual != digest:
            raise ValueError("numeric_operation_content_conflict")
        return {**_public(service.status(row["execution_id"])), "request_fingerprint": request.fingerprint,
                "completion_scope": "submission_only"}


@contextmanager
def _epoch(identity, request):
    lease = acquire_account_lease(identity.owner_user_id, expected_epoch=request.account_epoch)
    try:
        yield
    finally:
        lease.release()


def collect_numeric_integrity(identity, *, execution_id):
    """Return canonical report only for an authentic accepted closed process."""
    _authorize(identity)
    release = _fixed_release()
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        row = service.repository.get(execution_id, owner=identity.owner_user_id, session=identity.session_id)
        request, digest, sources = _load_bound(service, identity, row, LAUNCHER.read_bytes(), release)
        with _epoch(identity, request):
            status = service.status(execution_id)
            result = {**_public(status), "request_fingerprint": request.fingerprint, "report": None, "receipt": None}
            if status["status"] != "succeeded":
                if (status.get("receipt") or {}).get("process_stopped") is True:
                    result["receipt"] = {"process_stopped": True}
                return result
            receipt = status.get("receipt") or {}
            proof = receipt.get("verified_environment") or {}
            versions = proof.get("dependency_versions") or {}
            # Base image receipt enumerates installed site-packages. Private add-on
            # wheels are validated inside the trusted scanner itself and in image %post.
            if (receipt.get("network_policy") != "offline" or receipt.get("process_stopped") is not True
                    or proof.get("environment_id") != "base" or proof.get("revision") != 1
                    or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(proof.get("image_id", "")))
                    or any(versions.get(pkg) != PINS[pkg] for pkg in ("scipy", "numpy", "openpyxl", "et-xmlfile"))):
                raise PermissionError("numeric_verified_worker_receipt_required")
            with _accepted_file(service, identity, execution_id, RESULT, max_bytes=LIMITS.output_bytes) as (bound, ref, data):
                if bound.fingerprint != request.fingerprint:
                    raise ValueError("numeric_accepted_output_mismatch")
                report = json.loads(data)
                if (type(report) is not dict or report.get("schema") != REPORT_SCHEMA
                        or report.get("image_review") != "not_in_scope" or report.get("signal_not_verdict") is not True
                        or type(report.get("findings")) is not list
                        or report.get("status") not in {"complete", "partial", "not_applicable", "failed"}):
                    raise ValueError("numeric_worker_report_schema_mismatch")
                if report["status"] == "failed" and "request_sha256" not in report:
                    if report["findings"] or set(report) != {"schema", "status", "image_review", "signal_not_verdict", "limitations", "findings"}:
                        raise ValueError("numeric_worker_failed_report_mismatch")
                elif (report.get("request_sha256") != digest or report.get("sources") != [src.reference for src in sources]
                        or (report.get("paperconan") or {}).get("commit") != PAPERCONAN_COMMIT):
                    raise ValueError("numeric_worker_report_lineage_mismatch")
                result.update(report=report, receipt={"process_stopped": True, "network_policy": "offline",
                              "image_id": proof["image_id"], "report_sha256": ref["sha256"],
                              "request_fingerprint": request.fingerprint})
                return result
