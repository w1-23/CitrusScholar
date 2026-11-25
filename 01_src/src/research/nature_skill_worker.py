"""Host-only Code Worker scheduling and delivery for the pinned Nature CLI."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from src.agent.code_tools import (_accepted_file, _deadline, _key, _service,
                                  authorize_fusion_code)
from src.code_execution.presence import worker_available
from src.code_execution.protocol import ExecutionLimits, ExecutionRequest, TERMINAL
from src.core.account_leases import account_lease, acquire_account_lease, bound_account_lease
from src.session import files

from .nature_skill_contract import (CSV, EXPORTS, MAX_CSV_BYTES, PRODUCER, QA_MAX_BYTES,
                                   SCOPE, SCRIPT, SKILL_ID, RunArgs, argv,
                                   output_names, template_qa)

LIMITS = ExecutionLimits(timeout_sec=90, memory_mb=2048, pids=64, log_bytes=64 << 10,
                         scratch_bytes=96 << 20, output_bytes=64 << 20, output_files=4)


@contextmanager
def _execution_lease(identity, request):
    lease = acquire_account_lease(identity.owner_user_id, expected_epoch=request.account_epoch)
    try:
        with bound_account_lease(lease):
            yield
    finally:
        lease.release()


def _meta(pin, spec):
    return {"code_scope": SCOPE, "release_sha256": pin.release_sha256,
            "upstream_commit": pin.commit, "script_sha256": pin.sha256,
            "spec": spec.model_dump()}


def _source(identity, ref):
    row = files.get_task_input_file(identity.owner_user_id, identity.run_id, ref.file_id, ref.sha256)
    if (not row or row.get("visibility") != "public" or row.get("origin") not in files.ORIGINS
            or row.get("sha256") != ref.sha256):
        raise PermissionError("nature_plot_input_scope_denied")
    if (Path(row["filename"]).suffix.lower() != ".csv"
            or type(row.get("size_bytes")) is not int or not 0 < row["size_bytes"] <= MAX_CSV_BYTES):
        raise ValueError("nature_plot_requires_nonempty_utf8_csv_within_worker_limit")
    return {"file_id": ref.file_id, "sha256": ref.sha256, "size_bytes": row["size_bytes"]}


def _bind(service, identity, current, pin, source):
    task, wid, revision = current["task_id"], current["workspace_id"], current["revision"]
    snapshot = service.workspace.snapshot(wid, task_id=task, expected_revision=revision)
    entries = {item["path"]: item for item in snapshot["files"]}
    expected = {CSV: source, SCRIPT: {"sha256": pin.sha256, "size_bytes": len(pin.content)}}
    if not entries.keys() <= expected.keys():
        raise PermissionError("nature_plot_private_workspace_changed")
    for path, ref in expected.items():
        if path in entries:
            if entries[path]["version"] != 1 or any(entries[path][key] != value for key, value in ref.items()):
                raise PermissionError("nature_plot_private_workspace_changed")
            continue
        if path == SCRIPT:
            row = files.register_file(session_id=identity.session_id, owner_user_id=identity.owner_user_id,
                filename=Path(SCRIPT).name, content=pin.content, mime="text/plain",
                origin=files.ORIGIN_UPLOADED, visibility="internal",
                producer_tool=PRODUCER, client_request_id=_key(SCOPE, identity.owner_user_id,
                                                               identity.session_id, pin.sha256))
            if any(row[key] != value for key, value in ref.items()):
                raise ValueError("nature_plot_script_registration_mismatch")
            file_id = row["file_id"]
        else:
            file_id = ref["file_id"]
        result = service.workspace.bind(wid, path=path, file_id=file_id, expected_revision=revision,
                                      expected_sha256=ref["sha256"], source={"scope": SCOPE})
        revision = result["revision"]
    return revision


def _bound_request(service, identity, row, pin):
    request = ExecutionRequest.parse(row["request"])
    if request.identity != identity or not request.task_id.startswith("nature_"):
        raise PermissionError("nature_plot_execution_scope_denied")
    current = service.workspace.open(request.workspace_id)
    metadata = current["metadata"]
    if metadata.get("code_scope") != SCOPE:
        raise PermissionError("nature_plot_execution_scope_denied")
    spec = RunArgs.model_validate(metadata.get("spec"))
    stem, paths = output_names(spec.plot.template, request.task_id)
    if (metadata != _meta(pin, spec) or spec.backend != "python"
            or current["task_id"] != request.task_id
            or request.operation_id != request.task_id or request.idempotency_key != request.task_id
            or request.script.path != SCRIPT or request.script.sha256 != pin.sha256
            or request.script.size_bytes != len(pin.content) or request.script.version != 1
            or len(request.inputs) != 1 or request.inputs[0].path != CSV
            or request.inputs[0].version != 1 or request.inputs[0].file_id != spec.input.file_id
            or request.inputs[0].sha256 != spec.input.sha256
            or not 0 < request.inputs[0].size_bytes <= MAX_CSV_BYTES
            or request.arguments != argv(spec, stem) or request.outputs != paths
            or request.environment_id != "base" or request.environment_revision != 1
            or request.network_policy != "offline" or request.limits != LIMITS):
        raise PermissionError("nature_plot_execution_contract_mismatch")
    return request, spec, stem


class NatureSubmissionUncertain(RuntimeError):
    """A submit may have committed, or its existing receipt could not be checked."""


def schedule_plot(identity, *, release, pin, spec):
    call_id = authorize_fusion_code(identity, release)
    if spec.backend != "python":
        raise ValueError("nature_plot_backend_unavailable")
    task = "nature_" + _key(SCOPE, *identity.turn_key, identity.run_id, call_id)
    stem, paths = output_names(spec.plot.template, task)
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        source = _source(identity, spec.input)
        deadline = _deadline(identity)
        current = service.workspace.create(task_id=task, request_id=task, make_current=False,
                                           metadata=_meta(pin, spec))
        wid = current["workspace_id"]
        row = service.repository.find_submission(owner=identity.owner_user_id, session=identity.session_id,
            workspace_id=wid, operation_id=task, idempotency_key=task)
        if row is None:
            if not worker_available(service.repository.path):
                raise LookupError("nature_plot_code_worker_unavailable")
            revision = _bind(service, identity, current, pin, source)
            try:
                row = service.submit(task_id=task, workspace_id=wid, expected_revision=revision,
                    operation_id=task, idempotency_key=task, script_path=SCRIPT, arguments=argv(spec, stem),
                    inputs=(CSV,), outputs=paths, limits=LIMITS, absolute_deadline=deadline)
            except (LookupError, PermissionError, ValueError, OSError) as exc:
                raise NatureSubmissionUncertain() from exc
        try:
            request, saved, _ = _bound_request(service, identity, row, pin)
            if saved != spec:
                raise PermissionError("nature_plot_submission_mismatch")
            with _execution_lease(identity, request):
                return service.status(row["execution_id"])
        except (LookupError, PermissionError, ValueError, OSError) as exc:
            raise NatureSubmissionUncertain() from exc


def _accepted_exports(service, identity, execution_id, request, spec, stem):
    from src.core.deliverable_check import validate_deliverable

    data, qa = {}, None
    for path in request.outputs:
        name = Path(path).name
        is_qa = name.endswith(".qa.json")
        with _accepted_file(service, identity, execution_id, path,
                            max_bytes=QA_MAX_BYTES if is_qa else LIMITS.output_bytes) as (actual, ref, raw):
            if actual.fingerprint != request.fingerprint:
                raise PermissionError("nature_plot_accepted_request_changed")
            if is_qa:
                qa = template_qa(raw, spec, stem)
                continue
            fmt = Path(path).suffix[1:]
            mime = EXPORTS[fmt]
            if ref["mime_type"] != mime or (fmt == "tiff" and raw[:4] not in {b"II*\x00", b"MM\x00*"}):
                raise ValueError("nature_plot_export_format_mismatch")
            validate_deliverable(filename=name, content=raw, mime=mime)
            data[name] = (raw, mime, ref["sha256"])
    return data, qa


def _request_id(identity):
    from src.core import jobs

    job = jobs.get_job(identity.run_id)
    if (not job or job["owner_user_id"] != identity.owner_user_id
            or job["session_id"] != identity.session_id or job["request_id"] != identity.user_turn_id):
        raise PermissionError("nature_plot_publication_job_mismatch")
    return job.get("client_request_id") or identity.user_turn_id


def _record_artifact(identity, request_id, filename, item, producer, receipts):
    from .tools import validate_artifact_links

    link = item["artifact_url"]
    checked = validate_artifact_links([link], session_id=identity.session_id,
                owner_user_id=identity.owner_user_id, request_id=request_id)
    if (len(checked) != 1 or not checked[0]["valid"] or checked[0]["producer_tool"] != producer
            or checked[0]["filename"] != filename or item["filename"] != filename):
        raise ValueError("nature_plot_publication_receipt_mismatch")
    receipts.append({"url": link})


def _publish(identity, request_id, execution_id, source, data, receipts):
    from .artifacts import publish_artifact

    originals = {}
    for filename, (raw, mime, expected) in data.items():
        item = publish_artifact(owner=identity.owner_user_id, session_id=identity.session_id,
            filename=filename, content=raw, mime=mime, producer_tool=PRODUCER,
            request_id=request_id, logical_task_id=execution_id,
            source_file_id=source.file_id, source_sha256=source.sha256)
        if item["sha256"] != expected or item["size"] != len(raw):
            raise ValueError("nature_plot_publication_receipt_mismatch")
        _record_artifact(identity, request_id, filename, item, PRODUCER, receipts)
        originals[filename] = item
    return originals


def collect_plot(identity, *, release, pin, execution_id, receipts):
    """Publish only the verified accepted files of this turn's exact protected execution."""
    from src.figures.nature_plot_project import open_nature_plot

    authorize_fusion_code(identity, release)
    service = _service(identity)
    owner, session = identity.owner_user_id, identity.session_id
    with account_lease(owner):
        row = service.repository.get(execution_id, owner=owner, session=session)
        request, spec, stem = _bound_request(service, identity, row, pin)
        with _execution_lease(identity, request):
            status = service.status(execution_id)
            result = {"execution_id": execution_id, "worker_status": status["status"],
                      "backend": "python", "template": spec.plot.template}
            if status["status"] != "succeeded":
                if status["status"] in TERMINAL:
                    return result, "failed", "nature_plot_worker_failed"
                return result, "not_run", "nature_plot_worker_not_settled"
            receipt = status.get("receipt")
            if (type(receipt) is not dict or receipt.get("process_stopped") is not True
                    or receipt.get("network_policy") != "offline"
                    or not isinstance(receipt.get("files"), list)
                    or [item.get("path") for item in receipt["files"]] != list(request.outputs)):
                raise PermissionError("nature_plot_accepted_worker_receipt_required")
            data, qa = _accepted_exports(service, identity, execution_id, request, spec, stem)
            request_id = _request_id(identity)
            originals = _publish(identity, request_id, execution_id, request.inputs[0], data, receipts)
            result.update(original_exports=originals, template_data_qa=qa,
                          provenance={"skill": SKILL_ID, "upstream_commit": pin.commit,
                                      "script_sha256": pin.sha256, "release_sha256": pin.release_sha256,
                                      "request_fingerprint": request.fingerprint,
                                      "input_sha256": request.inputs[0].sha256})
            from src.figures.projects import ProjectError
            try:
                result["workbench"] = open_nature_plot(owner=owner, session_id=session,
                    title=spec.title or spec.plot.template, backend="python", original_exports=originals,
                    primary_svg=stem + ".svg", vendor_root=pin.vendor_root)
            except ProjectError:
                result["workbench"] = {"editable": False, "reason_code": "nature_plot_workbench_unavailable"}
                return result, "partial", "nature_plot_workbench_unavailable"
            if result["workbench"].get("editable") is True:
                derived = result["workbench"]["exports"]["svg"]
                _record_artifact(identity, request_id, derived["filename"], derived, "figure_workspace", receipts)
            return result, "complete", "nature_plot_originals_verified"
