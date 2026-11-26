"""Host-only adapter to the existing Code Worker, queue, snapshots and receipts."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from src.code_execution.presence import worker_available
from src.code_execution.protocol import ExecutionLimits, ExecutionRequest
from src.core.account_leases import account_lease, acquire_account_lease
from src.session import files

from .code_tools import (_accepted_file, _deadline, _key, _public, _service,
                         authorize_fusion_code)
from .fusion_worker_contract import (BUNDLE_ROOT, INPUT_ROOT, LAUNCHER, MAX_INPUT, MAX_TOTAL_INPUT,
    OUTPUT, REQUEST, SCOPE, decode, encode, input_path, is_hash, load_bundle, make_request, sha,
    validate_reference, validate_report)
from .fusion_worker_entry import MAX_REPORT_BYTES
from .fusion_lineage import prior_report_jobs as _prior_report_jobs

LIMITS = ExecutionLimits(timeout_sec=90, memory_mb=2048, cpu_millis=1000, pids=64,
                         scratch_bytes=32 << 20, log_bytes=128 << 10,
                         output_bytes=9 << 20, output_files=1)
OBSERVATIONS = INPUT_ROOT + "observations.json"


@contextmanager
def _epoch(identity, request):
    lease = acquire_account_lease(identity.owner_user_id, expected_epoch=request.account_epoch)
    try:
        yield
    finally:
        lease.release()


def _operation_key(identity, operation_id):
    if (type(operation_id) is not str or not 1 <= len(operation_id) <= 256
            or operation_id != operation_id.strip() or any(ord(ch) < 32 for ch in operation_id)):
        raise ValueError("fusion_operation_id_invalid")
    return "fusion_" + _key(identity.owner_user_id, identity.session_id, identity.user_turn_id,
                            identity.run_id, operation_id)


def _refs(identity, bundle, inputs):
    if type(inputs) is not dict or len(inputs) > 32:
        raise ValueError("fusion_input_count_limit")
    refs = {}
    for role, ref in inputs.items():
        validate_reference(ref)
        if role == "observations":
            raise PermissionError("fusion_host_observations_only")
        row = files.get_task_input_file(identity.owner_user_id, identity.run_id, ref["file_id"], ref["sha256"])
        if (row is None or row.get("visibility") != "public" or row.get("origin") not in files.ORIGINS
                or row.get("sha256") != ref["sha256"]):
            raise PermissionError("fusion_input_scope_denied")
        size = row.get("size_bytes")
        if type(size) is not int or not 0 <= size <= MAX_INPUT:
            raise ValueError("fusion_input_size_limit")
        refs[role] = {"path": input_path(bundle.skill, role, row["filename"]),
                      "file_id": ref["file_id"], "sha256": ref["sha256"], "size_bytes": size}
    if sum(ref["size_bytes"] for ref in refs.values()) > MAX_TOTAL_INPUT:
        raise ValueError("fusion_input_byte_limit")
    return refs


def _meta(bundle):
    return {"code_scope": SCOPE, "skill_id": bundle.skill, "installed_version": bundle.installed_version,
            "analyzer_version": bundle.version, "release_sha256": bundle.release_sha256,
            "bundle_sha256": bundle.sha256, "lock_sha256": bundle.lock_sha256}


def _content(bundle, refs, request_digest):
    return sha(encode({**_meta(bundle), "inputs": refs, "request_sha256": request_digest,
                       "launcher_sha256": sha(bundle.launcher)}))


def _register(identity, path, data):
    key = _key(SCOPE, identity.owner_user_id, identity.session_id, path, sha(data))
    record = files.register_file(session_id=identity.session_id, owner_user_id=identity.owner_user_id,
                                 filename=Path(path).name, content=data, client_request_id=key,
                                 origin=files.ORIGIN_UPLOADED, visibility="internal",
                                 producer_tool="fusion_worker")
    if record.get("sha256") != sha(data) or record.get("size_bytes") != len(data):
        raise ValueError("fusion_registered_content_mismatch")
    return record["file_id"]


def _bind(identity, workspace, current, task, public_refs, blobs):
    wid, revision = current["workspace_id"], current["revision"]
    snapshot = workspace.snapshot(wid, task_id=task, expected_revision=revision)
    existing = {item["path"]: item for item in snapshot["files"]}
    expected = {item["path"]: item for item in public_refs.values()}
    expected.update({path: {"sha256": sha(data), "size_bytes": len(data)} for path, data in blobs.items()})
    if not existing.keys() <= expected.keys():
        raise PermissionError("fusion_workspace_unexpected_snapshot")
    for path, ref in sorted(expected.items()):
        if path in existing:
            saved = existing[path]
            if (any(saved[name] != ref[name] for name in ("sha256", "size_bytes")) or saved["version"] != 1
                    or "file_id" in ref and saved["file_id"] != ref["file_id"]):
                raise PermissionError("fusion_workspace_snapshot_conflict")
            continue
        file_id = ref["file_id"] if "file_id" in ref else _register(identity, path, blobs[path])
        bound = workspace.bind(wid, path=path, file_id=file_id, expected_revision=revision,
                               expected_sha256=ref["sha256"], source={"scope": SCOPE})
        revision = bound["revision"]
    return revision, tuple(sorted(expected.keys() - {LAUNCHER}))


def _load_bound(service, identity, row, bundle, *, content_sha256=None):
    request = ExecutionRequest.parse(row["request"])
    if (request.identity != identity or request.task_id != request.operation_id
            or request.operation_id != request.idempotency_key or not request.operation_id.startswith("fusion_")
            or request.script.path != LAUNCHER or request.script.sha256 != sha(bundle.launcher)
            or request.arguments != (bundle.skill,) or request.outputs != (OUTPUT,)
            or request.network_policy != "offline" or request.environment_id != "base"
            or request.environment_revision != 1 or request.limits != LIMITS):
        raise PermissionError("fusion_execution_scope_denied")
    current = service.workspace.open(request.workspace_id)
    meta, fixed = current["metadata"], _meta(bundle)
    if (current["task_id"] != request.task_id or meta.keys() != fixed.keys() | {"content_sha256", "request_sha256"}
            or any(meta[name] != val for name, val in fixed.items()) or not is_hash(meta["content_sha256"])
            or content_sha256 is not None and meta["content_sha256"] != content_sha256):
        raise PermissionError("fusion_execution_release_mismatch")
    bindings = {item.path: item for item in request.inputs}
    if (REQUEST not in bindings or bindings[REQUEST].sha256 != meta["request_sha256"]
            or any(BUNDLE_ROOT + path not in bindings or bindings[BUNDLE_ROOT + path].sha256 != sha(data)
                   for path, data in bundle.members.items())
            or len(bindings) != len(bundle.members) + 1 + sum(path.startswith(INPUT_ROOT) for path in bindings)):
        raise PermissionError("fusion_execution_snapshot_mismatch")
    return request, meta, bindings


def _submission(service, identity, row, bundle, expected_content=None):
    request, meta, _ = _load_bound(service, identity, row, bundle, content_sha256=expected_content)
    with _epoch(identity, request):
        result = _public(service.status(row["execution_id"]))
    return {**result, "_code_execution_id": row["execution_id"], "skill_id": bundle.skill,
            "installed_version": bundle.installed_version, "analyzer_version": bundle.version,
            "release_sha256": bundle.release_sha256, "bundle_sha256": bundle.sha256,
            "request_sha256": meta["request_sha256"], "content_sha256": meta["content_sha256"],
            "request_fingerprint": request.fingerprint, "completion_scope": "submission_only"}


def find_fusion(identity, *, operation_id, skill_release, skill_id):
    """Find the exact prior submission in this run without creating a Workspace."""
    authorize_fusion_code(identity, skill_release)
    operation = _operation_key(identity, operation_id)
    bundle = load_bundle(skill_release, skill_id)
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        rows = [row for row in service.repository.run_executions(identity)
                if row["task"] == row["operation"] == row["request_key"] == operation]
        if len(rows) > 1:
            raise ValueError("fusion_duplicate_execution")
        return _submission(service, identity, rows[0], bundle) if rows else None


def schedule_fusion(identity, *, operation_id, skill_release, skill_id, inputs, options, observations=None):
    """Schedule one content-identified offline child using only host-approved bytes."""
    authorize_fusion_code(identity, skill_release)
    task = _operation_key(identity, operation_id)
    bundle = load_bundle(skill_release, skill_id)
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        refs = _refs(identity, bundle, inputs)
        raw, cassette = make_request(skill_id, refs, options, observations)
        request_digest = sha(raw)
        content = _content(bundle, refs, request_digest)
        deadline = _deadline(identity)
        current = service.workspace.create(task_id=task, request_id=task, make_current=False,
            metadata={**_meta(bundle), "request_sha256": request_digest, "content_sha256": content})
        wid = current["workspace_id"]
        prior = service.repository.find_submission(owner=identity.owner_user_id, session=identity.session_id,
            workspace_id=wid, operation_id=task, idempotency_key=task)
        if prior is not None:
            return _submission(service, identity, prior, bundle, content)
        if not worker_available(service.repository.path):
            raise LookupError("code_worker_unavailable")
        blobs = {LAUNCHER: bundle.launcher, REQUEST: raw}
        blobs.update({BUNDLE_ROOT + path: data for path, data in bundle.members.items()})
        if cassette is not None:
            blobs[OBSERVATIONS] = cassette
        revision, selected = _bind(identity, service.workspace, current, task, refs, blobs)
        row = service.submit(task_id=task, workspace_id=wid, expected_revision=revision, script_path=LAUNCHER,
            inputs=selected, outputs=(OUTPUT,), arguments=(skill_id,), operation_id=task, idempotency_key=task,
            network_policy="offline", limits=LIMITS, absolute_deadline=deadline)
        return _submission(service, identity, row, bundle, content)


def _original_inputs(service, request, bindings, bundle, meta, *, historical=False):
    ref = bindings[REQUEST]
    raw = service.workspace.read_bytes(request.workspace_id, path=REQUEST, version=ref.version,
                                       expected_sha256=ref.sha256)
    if sha(raw) != ref.sha256 or len(raw) != ref.size_bytes:
        raise ValueError("fusion_request_identity_mismatch")
    payload = decode(raw)
    if type(payload) is not dict or set(payload) != {"interface_version", "inputs", "options"} or payload["interface_version"] != 1:
        raise ValueError("fusion_request_schema_mismatch")
    exact, public, scoped = {}, {}, set()
    for item in payload["inputs"]:
        role, relative = item["role"], item["path"]
        target = INPUT_ROOT + relative
        snap = bindings[target]
        if role in scoped or item["sha256"] != snap.sha256:
            raise ValueError("fusion_request_binding_mismatch")
        scoped.add(role)
        if role == "observations":
            if bundle.skill != "literature-verification" or target != OBSERVATIONS:
                raise PermissionError("fusion_request_observations_mismatch")
            continue
        if input_path(bundle.skill, role, relative) != target:
            raise ValueError("fusion_request_binding_mismatch")
        public[role] = {"file_id": snap.file_id, "sha256": snap.sha256}
        if not historical:
            live = files.get_task_input_file(request.identity.owner_user_id, request.identity.run_id,
                                             snap.file_id, snap.sha256)
            if live is None or live.get("size_bytes") != snap.size_bytes:
                raise PermissionError("fusion_input_scope_denied")
        exact[role] = {**public[role], "path": target, "size_bytes": snap.size_bytes}
    if (_content(bundle, exact, ref.sha256) != meta["content_sha256"]
            or len(scoped) != sum(path.startswith(INPUT_ROOT) for path in bindings)):
        raise ValueError("fusion_request_content_mismatch")
    return payload, public


def historical_report_candidates(identity, *, skill_release, skill_ids, exact_id=None, requested_source=None):
    """Identify prior successful fusion executions before exposing any output bytes."""
    authorize_fusion_code(identity, skill_release)
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        known = _prior_report_jobs(identity)
        if exact_id:
            rows = [service.repository.get(exact_id, owner=identity.owner_user_id, session=identity.session_id)]
        else:
            rows = [row for prior in known for row in service.repository.run_executions(prior)]
        chosen = []
        for row in rows:
            if row["status"] != "succeeded" or not row["operation"].startswith("fusion_"):
                continue
            request = ExecutionRequest.parse(row["request"])
            if request.identity not in known or len(request.arguments) != 1 or request.arguments[0] not in skill_ids:
                continue
            source_names = []
            for ref in request.inputs:
                if not ref.path.startswith(INPUT_ROOT) or ref.path == OBSERVATIONS:
                    continue
                file = files.get_account_file(ref.file_id, identity.owner_user_id)
                if file and file.get("sha256") == ref.sha256:
                    source_names.append(file["filename"])
            if requested_source and not any(name.casefold() == requested_source.casefold() for name in source_names):
                continue
            chosen.append((row["execution_id"], request.arguments[0], request.identity))
        if exact_id or len(chosen) < 2:
            return sorted(chosen, key=lambda x: x[0])
        # Durable children form one exact original report lineage, not a new
        # candidate per coverage window. A report from another extraction is
        # still ambiguous and must be explicitly selected by the user.
        from src.research.fusion_observations import RegistryCoverage
        groups = {}
        store = RegistryCoverage(identity)
        for selected in chosen:
            child_id, skill, origin = selected
            inspected = store.child(child_id, historical_identity=origin) if skill == "literature-verification" else None
            key = (origin, inspected["original_execution_id"] if inspected else child_id)
            rank = (inspected.get("revision", 0) + 1, inspected["offset"]) if inspected else (0, 0)
            if key not in groups or rank > groups[key][0]:
                groups[key] = rank, selected
            elif rank == groups[key][0]:
                raise LookupError("external_report_child_ambiguous")
        return sorted((selected for _, selected in groups.values()), key=lambda x: x[0])


def _historical_bundle(service, row, skill_id, current):
    """Use only read-only roots from trusted deployment configuration."""
    request = ExecutionRequest.parse(row["request"])
    stored = service.workspace.open(request.workspace_id)["metadata"]
    if all(stored.get(field) == value for field, value in _meta(current).items()):
        return current
    roots = os.environ.get("CITRUS_FUSION_REPORT_RELEASES", "").split(os.pathsep)
    if len(roots) > 12:
        raise ValueError("external_report_release_limit")
    candidates = []
    for raw in dict.fromkeys(roots):
        if not raw or not Path(raw).is_absolute():
            continue
        try:
            candidate = load_bundle(Path(raw).resolve(strict=True), skill_id)
        except (KeyError, LookupError, ValueError, OSError):
            continue
        if all(stored.get(field) == value for field, value in _meta(candidate).items()):
            candidates.append(candidate)
    if not candidates:
        raise PermissionError("external_report_pinned_release_unavailable")
    return candidates[0]


def historical_fusion_source(identity, report_value, reference):
    """Read exact retained input bytes. The current account must still own it."""
    source = report_value.get("_source_snapshot")
    if not source or source["identity"].owner_user_id != identity.owner_user_id \
            or source["identity"].session_id != identity.session_id:
        raise PermissionError("fusion_source_lineage_unavailable")
    matches = [snap for snap in source["refs"] if snap.file_id == reference["file_id"]
               and snap.sha256 == reference["sha256"]]
    if len(matches) != 1:
        raise ValueError("fusion_source_snapshot_ambiguous")
    snap = matches[0]
    with account_lease(identity.owner_user_id):
        metadata = files.get_account_file(snap.file_id, identity.owner_user_id)
        if not metadata or metadata.get("sha256") != snap.sha256 or metadata.get("size_bytes") != snap.size_bytes:
            raise PermissionError("fusion_source_no_longer_authorized")
        data = _service(identity).workspace.read_bytes(source["workspace_id"], path=snap.path,
                  version=snap.version, expected_sha256=snap.sha256)
        if len(data) != snap.size_bytes or sha(data) != snap.sha256:
            raise ValueError("fusion_source_snapshot_changed")
        return data, metadata["filename"]


def consume_fusion(identity, *, execution_id, skill_release, skill_id, content_sha256=None, source_identity=None):
    """Read the private report only after accepted bytes and current authorization."""
    authorize_fusion_code(identity, skill_release)
    bundle = load_bundle(skill_release, skill_id)
    service = _service(identity)
    with account_lease(identity.owner_user_id):
        row = service.repository.get(execution_id, owner=identity.owner_user_id, session=identity.session_id)
        historical = source_identity is not None and source_identity != identity
        if historical:
            if source_identity not in _prior_report_jobs(identity) or row["status"] != "succeeded":
                raise PermissionError("fusion_report_lineage_denied")
            if ExecutionRequest.parse(row["request"]).identity != source_identity:
                raise PermissionError("fusion_report_identity_denied")
            bundle = _historical_bundle(service, row, skill_id, bundle)
        request, meta, bindings = _load_bound(service, source_identity if historical else identity, row, bundle,
                                               content_sha256=content_sha256)
        with _epoch(identity, request):
            status = service.status(execution_id)
            result = {**_public(status), "content_sha256": meta["content_sha256"],
                      "request_fingerprint": request.fingerprint, "request_sha256": meta["request_sha256"],
                      "release_sha256": bundle.release_sha256, "bundle_sha256": bundle.sha256,
                      "skill_id": skill_id, "installed_version": bundle.installed_version,
                      "analyzer_version": bundle.version, "receipt": None, "report": None}
            if status["status"] != "succeeded":
                receipt = status.get("receipt")
                if isinstance(receipt, dict) and receipt.get("process_stopped") is True:
                    result["reason"] = _public({**status, "reason": receipt.get("reason", "")})["reason"]
                    result["receipt"] = {"process_stopped": True}
                return result
            receipt = status.get("receipt")
            if (type(receipt) is not dict or receipt.get("process_stopped") is not True
                    or receipt.get("network_policy") != "offline"):
                raise PermissionError("fusion_accepted_worker_receipt_required")
            payload, inputs = _original_inputs(service, request, bindings, bundle, meta, historical=historical)
            with _accepted_file(service, identity, execution_id, OUTPUT, max_bytes=MAX_REPORT_BYTES) as (observed, ref, data):
                if observed.fingerprint != request.fingerprint:
                    raise ValueError("fusion_accepted_output_identity_mismatch")
                report = decode(data)
                runtime = validate_report(report, payload, bundle, meta["request_sha256"])
                environment = receipt.get("verified_environment")
                if isinstance(environment, dict):
                    versions = environment.get("dependency_versions")
                    if (type(versions) is not dict or versions.get("python") != runtime.get("python")
                            or any(versions.get(name) != version for name, version in runtime["dependencies"].items())):
                        raise ValueError("fusion_worker_environment_receipt_mismatch")
                return {**result, "inputs": inputs, "options": payload["options"], "report": report,
                        "_account_epoch": request.account_epoch,
                        "_source_snapshot": {"identity": request.identity, "workspace_id": request.workspace_id,
                            "refs": tuple(snap for snap in request.inputs if snap.path.startswith(INPUT_ROOT)
                                          and snap.path != OBSERVATIONS)} if historical else None,
                        "receipt": {"execution_id": execution_id, "fence": status["fence"],
                            "request_fingerprint": request.fingerprint, "process_stopped": True,
                            "network_policy": "offline", "environment": runtime,
                            "verified_environment": environment, "result": ref}}
