"""Exercise the host handoff without attaching real files or running a daemon."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

from src.agent import numeric_integrity_worker as host
from src.agent.identity import ExecutionIdentity
from src.code_execution.protocol import ExecutionRequest, FileSnapshot

IDENTITY = ExecutionIdentity(7, "fixture-session", "fixture-turn", "fixture-run")
FILE_ID = "a" * 32
SHA = "b" * 64
RELEASE = "c" * 64
SOURCE = {"file_id": FILE_ID, "sha256": SHA}
FILE = {"filename": "test.csv", "sha256": SHA, "size_bytes": 20, "visibility": "public",
        "origin": host.files.ORIGIN_UPLOADED}


class NumericHostTest(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.repository.path = Path("/fixture/code-executions")
        status = {"execution_id": "e" * 32, "task_id": "integrity_fixture", "workspace_id": "f" * 32,
                  "status": "queued", "fence": 1, "created": 1, "updated": 1, "deadline": 2, "reason": ""}
        self.status = status
        self.service.status.return_value = status
        self.scope = patch.multiple(host, _authorize=Mock(), _fixed_release=Mock(return_value=RELEASE),
                                    _service=Mock(return_value=self.service), account_lease=Mock(return_value=nullcontext()),
                                    acquire_account_lease=Mock(return_value=SimpleNamespace(release=Mock())))
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.source = patch.object(host.files, "get_task_input_file", return_value=FILE)
        self.source.start()
        self.addCleanup(self.source.stop)

    def prepare_row(self, digest=None):
        raw, sources = host._prepare(IDENTITY, [SOURCE], False)
        assert digest is None or digest == host._digest(raw)
        task = "integrity_fixture"
        request = ExecutionRequest(identity=IDENTITY, task_id=task, operation_id=task, idempotency_key=task,
            workspace_id="f" * 32, workspace_revision=5, account_epoch=1,
            script=FileSnapshot(host.SCRIPT, "1" * 32, 1, host._digest(host.LAUNCHER.read_bytes()), 200),
            inputs=(FileSnapshot(host.REQUEST, "2" * 32, 1, host._digest(raw), len(raw)),
                    FileSnapshot(host.ROOT + "source-01.csv", FILE_ID, 1, SHA, 20)),
            outputs=(host.RESULT,), limits=host.LIMITS)
        row = {"execution_id": "e" * 32, "task": task, "operation": task, "request_key": task,
               "request": request.serialize()}
        self.service.workspace.open.return_value = {"task_id": task, "metadata": {
            "code_scope": host.SCOPE, "worker_release": RELEASE, "request_sha256": host._digest(raw)}}
        self.service.workspace.read_bytes.return_value = raw
        return row, request, sources, host._digest(raw)

    def test_private_current_scope_and_region_rejection(self):
        with self.assertRaisesRegex(ValueError, "numeric_bounded_regions_not_supported"):
            host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[SOURCE], regions=["A1:B5"])
        self.service.submit.assert_not_called()
        with patch.object(host.files, "get_task_input_file", return_value=None):
            with self.assertRaisesRegex(PermissionError, "numeric_file_not_admitted"):
                host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[SOURCE])
        with self.assertRaisesRegex(ValueError, "explicit_admitted"):
            host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[])

    def test_submit_once_with_offline_fixed_paths_and_idempotency(self):
        row, request, _, digest = self.prepare_row()
        with patch.object(host, "_key", return_value="fixture"), patch.object(host, "_deadline", return_value=23), \
             patch.object(host, "_bind", return_value=(5, tuple(item.path for item in request.inputs))), \
             patch.object(host, "worker_available", return_value=True):
            self.service.workspace.create.return_value = {"workspace_id": "f" * 32, "metadata": {
                "code_scope": host.SCOPE, "worker_release": RELEASE, "request_sha256": digest}}
            self.service.submit.return_value = row
            self.service.repository.run_executions.side_effect = [[], [row], [row]]
            result = host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[SOURCE])
            replay = host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[SOURCE])
            self.assertEqual(result, replay)
            self.assertEqual(result["completion_scope"], "submission_only")
            self.service.submit.assert_called_once()
            self.assertEqual(self.service.submit.call_args.kwargs["network_policy"], "offline")
            self.assertEqual(self.service.submit.call_args.kwargs["absolute_deadline"], 23)
            with self.assertRaisesRegex(ValueError, "numeric_operation_content_conflict"):
                host.schedule_numeric_integrity(IDENTITY, operation_id="fixture", sources=[SOURCE], enable_benford=True)

    def test_closed_receipt_exact_report_and_rejection(self):
        row, request, sources, digest = self.prepare_row()
        self.service.repository.get.return_value = row
        result = host.collect_numeric_integrity(IDENTITY, execution_id="e" * 32)
        self.assertIsNone(result["report"])
        self.status.update(status="succeeded", receipt={"process_stopped": True, "network_policy": "offline"})
        with self.assertRaisesRegex(PermissionError, "verified_worker_receipt_required"):
            host.collect_numeric_integrity(IDENTITY, execution_id="e" * 32)
        self.status["receipt"]["verified_environment"] = {"environment_id": "base", "revision": 1,
             "image_id": "sha256:" + "d" * 64, "dependency_versions": host.PINS}
        data = {"schema": host.REPORT_SCHEMA, "image_review": "not_in_scope", "signal_not_verdict": True,
                "status": "partial", "request_sha256": digest, "sources": [src.reference for src in sources],
                "findings": [], "paperconan": {"commit": host.PAPERCONAN_COMMIT}}

        @contextmanager
        def accepted(*args, **kwargs):
            yield request, {"sha256": "a" * 64}, host._encode(data)

        with patch.object(host, "_accepted_file", side_effect=accepted):
            result = host.collect_numeric_integrity(IDENTITY, execution_id="e" * 32)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["report"]["status"], "partial")
            self.assertTrue(result["receipt"]["process_stopped"])
            data["request_sha256"] = "a" * 64
            with self.assertRaisesRegex(ValueError, "report_lineage_mismatch"):
                host.collect_numeric_integrity(IDENTITY, execution_id="e" * 32)
