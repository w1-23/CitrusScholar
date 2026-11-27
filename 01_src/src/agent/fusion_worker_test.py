"""Host only fusion queue, input admission and acceptance regressions."""
from contextlib import nullcontext
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.agent import code_tools, fusion_worker as worker, fusion_worker_contract as contract
from src.agent.identity import ExecutionIdentity
from src.code_execution.protocol import ExecutionRequest, FileSnapshot
from src.session import files

IDENTITY = ExecutionIdentity(7, "fixture-thread", "fixture-turn", "fixture-run")
OTHER = ExecutionIdentity(7, "fixture-thread", "other-turn", "other-run")
RELEASE = Path("/fixture/release")
FILE = "a" * 32
DIGEST = "3" * 64


class WorkerContractTests(unittest.TestCase):
    def setUp(self):
        self.bundle = contract.Bundle("literature-verification", "b" * 40, "0.1.0", "0" * 64,
            "1" * 64, b"approved.py", {"bundle.json": b"bundle"}, "2" * 64,
            {"pypdf": "6.10.2"})
        self.live_attachment = {"filename": "cross-session.BIB", "visibility": "public",
                                "origin": files.ORIGIN_UPLOADED, "sha256": DIGEST, "size_bytes": 14}
        self.refs = {"bibliography": {"file_id": FILE, "sha256": DIGEST}}
        self.bound = {"bibliography": {**self.refs["bibliography"],
                                      "path": contract.INPUT_ROOT + "bibliography.bib", "size_bytes": 14}}

    def test_exact_task_pin_and_reserved_observations(self):
        with patch.object(files, "get_task_input_file", return_value=self.live_attachment) as admitted:
            self.assertEqual(worker._refs(IDENTITY, self.bundle, self.refs), self.bound)
            admitted.assert_called_once_with(7, "fixture-run", FILE, DIGEST)
            admitted.reset_mock()
            with self.assertRaisesRegex(PermissionError, "fusion_host_observations_only"):
                worker._refs(IDENTITY, self.bundle, {"observations": self.refs["bibliography"]})
            admitted.assert_not_called()
        with patch.object(files, "get_task_input_file", return_value=None):
            with self.assertRaisesRegex(PermissionError, "fusion_input_scope_denied"):
                worker._refs(IDENTITY, self.bundle, self.refs)
        with self.assertRaisesRegex(ValueError, "fusion_options_invalid"):
            contract.make_request(self.bundle.skill, self.bound, {"observations": {}}, None)
        raw, cassette = contract.make_request(self.bundle.skill, self.bound, {"strict": True}, {"http": {}})
        self.assertEqual(contract.decode(raw)["inputs"], [
            {"role": "bibliography", "path": "bibliography.bib", "sha256": DIGEST},
            {"role": "observations", "path": "observations.json", "sha256": contract.sha(cassette)}])

    def test_report_identity_and_lock_checked(self):
        raw, _ = contract.make_request(self.bundle.skill, self.bound, {}, None)
        payload = contract.decode(raw)
        runtime = {"python": "3.12.16", "platform": "linux-x86_64", "libseccomp_fchmodat2": True,
                   "dependencies": self.bundle.pins, "lock_sha256": self.bundle.lock_sha256}
        report = {key: [] for key in contract.REPORT_KEYS}
        report.update(status="partial", execution={"skill": self.bundle.skill, "version": self.bundle.version,
            "python": "3.12.16", "inputs": payload["inputs"], "options": {}}, worker={
                "schema": contract.SCOPE, "environment": runtime,
                "request_sha256": contract.sha(raw), "bundle_sha256": self.bundle.sha256})
        self.assertEqual(contract.validate_report(report, payload, self.bundle, contract.sha(raw)), runtime)
        report["execution"]["inputs"] = []
        with self.assertRaisesRegex(ValueError, "fusion_report_identity_mismatch"):
            contract.validate_report(report, payload, self.bundle, contract.sha(raw))
        report["execution"]["inputs"] = payload["inputs"]
        runtime["dependencies"] = {"pypdf": "999"}
        with self.assertRaisesRegex(ValueError, "fusion_report_environment_mismatch"):
            contract.validate_report(report, payload, self.bundle, contract.sha(raw))

    def test_no_network_policy_or_cross_run_snapshot_alias(self):
        task = worker._operation_key(IDENTITY, "tool-abc")
        payload, _ = contract.make_request(self.bundle.skill, self.bound, {}, None)
        payload_hash = contract.sha(payload)
        fingerprint = worker._content(self.bundle, self.bound, payload_hash)
        request = ExecutionRequest(identity=IDENTITY, task_id=task, workspace_id="e" * 32,
            workspace_revision=5, operation_id=task, idempotency_key=task,
            script=FileSnapshot(contract.LAUNCHER, "b" * 32, 1, contract.sha(self.bundle.launcher), 11),
            arguments=(self.bundle.skill,), outputs=(contract.OUTPUT,), limits=worker.LIMITS,
            inputs=(FileSnapshot(contract.REQUEST, "c" * 32, 1, payload_hash, len(payload)),
                    FileSnapshot(contract.BUNDLE_ROOT + "bundle.json", "d" * 32, 1, contract.sha(b"bundle"), 6),
                    FileSnapshot(self.bound["bibliography"]["path"], FILE, 1, DIGEST, 14)))
        service = SimpleNamespace(workspace=SimpleNamespace(open=lambda _: {"task_id": task, "metadata": {
            **worker._meta(self.bundle), "request_sha256": payload_hash, "content_sha256": fingerprint}}))
        row = {"request": request.serialize()}
        self.assertEqual(worker._load_bound(service, IDENTITY, row, self.bundle)[0], request)
        with self.assertRaisesRegex(PermissionError, "fusion_execution_scope_denied"):
            worker._load_bound(service, OTHER, row, self.bundle)
        with self.assertRaisesRegex(PermissionError, "fusion_execution_release_mismatch"):
            worker._load_bound(service, IDENTITY, row, self.bundle, content_sha256="1" * 64)
        modified = {**request.as_dict(), "network_policy": "research_https"}
        with self.assertRaisesRegex(PermissionError, "fusion_execution_scope_denied"):
            worker._load_bound(service, IDENTITY, {"request": contract.encode(modified).decode()}, self.bundle)

    def test_generic_code_cannot_reveal_or_reissue_private_analysis(self):
        manifest = {"task_id": "fixture", "metadata": {"code_scope": contract.SCOPE}}
        service = SimpleNamespace(workspace=SimpleNamespace(open=lambda *_: manifest),
                                  status=lambda *args: {"workspace_id": "e" * 32})
        with (code_tools.code_execution_context(IDENTITY, "tool-abc", skill_release=RELEASE),
              patch.object(code_tools, "_identity", return_value=(IDENTITY, "tool-abc")),
              patch.object(code_tools, "_service", return_value=service),
              patch("src.core.account_leases.account_lease", return_value=nullcontext()),
              patch.object(code_tools, "observe_code_rejection")):
            for action in ("status", "log", "read", "deliver", "wait", "cancel"):
                args = {"path": "/outputs/analysis.json"} if action in {"read", "deliver"} else {}
                _, result = code_tools._dispatch(action, execution_id="e" * 32, **args)
                self.assertEqual(result["error"], "workspace_scope_denied")
            for action in ("workspace", "execute", "bind_skill_script", "bind_input", "write"):
                _, result = code_tools._dispatch(action, workspace_id="e" * 32)
                self.assertEqual(result["error"], "workspace_scope_denied")

    def test_task_pin_local_store_excludes_unselected_and_foreign(self):
        with TemporaryDirectory() as value:
            root = Path(value)
            def register(session, filename, content, owner=7, visibility="public"):
                return files.register_file(session_id=session, owner_user_id=owner, filename=filename,
                                           content=content, visibility=visibility)
            with (patch.object(files, "DB_PATH", root / "files.db"),
                  patch.object(files, "FILES_ROOT", root / "data")):
                first = register("old-thread", "selected.bib", b"approved read")
                same = register("fixture-thread", "unselected.bib", b"unselected")
                hidden = register("fixture-thread", "internal.bib", b"internal", visibility="internal")
                foreign = register("fixture-thread", "other-owner.bib", b"foreign", owner=8)
                files.replace_resource_references(7, "task_input", IDENTITY.run_id, [first["file_id"], hidden["file_id"]])
                files.replace_resource_references(8, "task_input", IDENTITY.run_id, [foreign["file_id"]])
                selected = files.get_task_input_file(7, IDENTITY.run_id, first["file_id"], first["sha256"])
                self.assertEqual(selected["session_id"], "old-thread")
                self.assertEqual(hashlib.sha256(selected["path"].read_bytes()).hexdigest(), first["sha256"])
                for row in (same, hidden, foreign):
                    self.assertIsNone(files.get_task_input_file(7, IDENTITY.run_id, row["file_id"], row["sha256"]))
                self.assertIsNone(files.get_task_input_file(7, OTHER.run_id, first["file_id"], first["sha256"]))
                self.assertIsNone(files.get_task_input_file(7, IDENTITY.run_id, first["file_id"], "a" * 64))



def test_private_projection_is_bound_but_not_submitted(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from src.agent import fusion_worker as worker

    raw = b"fixture selected cells"
    bound = {}

    def bind(wid, *, path, file_id, expected_revision, expected_sha256, source):
        bound[path] = expected_sha256
        return {"revision": expected_revision + 1}

    workspace = SimpleNamespace(snapshot=lambda *a, **kw: {"files": []}, bind=bind)
    monkeypatch.setattr(worker, "_register", lambda identity, path, data: "a" * 32)
    revision, submitted = worker._bind(object(), workspace, {"workspace_id": "ws", "revision": 0},
        "job", {}, {worker.LAUNCHER: b"script", worker.REQUEST: b"request", worker.PLOT_SNAPSHOT: raw})
    assert revision == 3 and submitted == (worker.REQUEST,)
    pin = {"plot_projection_sha256": bound[worker.PLOT_SNAPSHOT]}

    def read(wid, *, path, version, expected_sha256):
        assert (wid, path, version, expected_sha256) == ("ws", worker.PLOT_SNAPSHOT, 1, worker.sha(raw))
        return raw

    service = SimpleNamespace(workspace=SimpleNamespace(read_bytes=read))
    request = SimpleNamespace(workspace_id="ws")
    assert worker._accepted_plot_projection(service, request, {}) == {}
    assert worker._accepted_plot_projection(service, request, pin) == {"_plot_projection": raw}
    service.workspace.read_bytes = lambda *a, **kw: b"altered"
    with pytest.raises(ValueError, match="fusion_frozen_projection_mismatch"):
        worker._accepted_plot_projection(service, request, pin)
