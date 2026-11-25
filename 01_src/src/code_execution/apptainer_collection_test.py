"""Trusted collection protocol and the controller's one-stop receipt semantics."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from src.agent.identity import ExecutionIdentity
from src.code_execution import controller
from src.code_execution.apptainer import ApptainerConfig, ApptainerHandle, ApptainerStatus, OfflineApptainerBackend
from src.code_execution.apptainer_service import collect_outputs
from src.code_execution.oci import OCICollection, OCIError, OutputRejected, _COLLECTOR
from src.code_execution.protocol import ExecutionRequest, FileSnapshot


OUTPUT = "/outputs/result.txt"
MISSING = "execution_declared_output_missing"
EXECUTION_ID = "3" * 32


def execution_request():
    return ExecutionRequest(ExecutionIdentity(7, "session", "turn", "run"), "task", "1" * 32,
        1, "operation", "key", FileSnapshot("/workspace/script.py", "2" * 32, 1, "4" * 64, 1),
        outputs=(OUTPUT,))


class CollectorTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "collector requires Linux process controls")
    def test_regular_missing_and_partial_file_sets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "outputs").mkdir()
            (root / "outputs/result.txt").write_bytes(b"result")
            (root / "outputs/extra.txt").write_bytes(b"extra")
            collector = _COLLECTOR.replace("os.open('/' + parts[0],", f"os.open({directory!r} + '/' + parts[0],")
            settings = dict(collector=collector, output_bytes=4096, collection_bytes=8192, host_env=os.environ)
            valid = {OUTPUT: base64.b64encode(b"result").decode()}
            extra = "/outputs/extra.txt"
            absent = "/outputs/absent.txt"
            self.assertEqual(collect_outputs({**settings, "outputs": (OUTPUT,)}), {"outputs": valid})
            self.assertEqual(collect_outputs({**settings, "outputs": (OUTPUT, extra)}),
                             {"outputs": {**valid, extra: base64.b64encode(b"extra").decode()}})
            for paths in [(absent,), (OUTPUT, absent), (absent, "/outputs/sub/absent.txt")]:
                with self.subTest(paths=paths):
                    self.assertEqual(collect_outputs({**settings, "outputs": paths}),
                                     {"collection_error": MISSING,
                                      "missing_paths": [path for path in paths if path != OUTPUT]})
            self.assertEqual(collect_outputs({**settings, "outputs": (OUTPUT, extra), "output_bytes": 10}),
                             {"collection_error": "execution_output_byte_limit"})

    def test_unconfirmed_collector_errors_omit_process_text(self):
        settings = dict(collector="", output_bytes=4096, collection_bytes=10, host_env={}, outputs=(OUTPUT,))
        cases = [
            (subprocess.TimeoutExpired("private command", 10), "apptainer_collector_timeout"),
            (OSError("private path"), "apptainer_collector_unconfirmed"),
            (subprocess.CompletedProcess([], 2, b"private output", b"private error"), "apptainer_collector_unconfirmed"),
            (subprocess.CompletedProcess([], 0, b" " * 11, b""), "apptainer_collection_record_limit"),
            (subprocess.CompletedProcess([], 0, b"bad record", b""), "apptainer_collector_record_invalid"),
        ]
        for completed, code in cases:
            with self.subTest(code=code), patch("src.code_execution.apptainer_service.subprocess.run",
                **({"side_effect": completed} if isinstance(completed, Exception) else {"return_value": completed})):
                self.assertEqual(collect_outputs(settings), {"collection_error": code})


class ApptainerCollectionTests(unittest.TestCase):
    def setUp(self):
        root = "/fixture/stage"
        image = "sha256:" + "0" * 64
        self.backend = OfflineApptainerBackend(ApptainerConfig(image, root, "/fixture/image.sif"))
        name = f"citrus-code-appt-{EXECUTION_ID}-1-{'0' * 24}.service"
        self.handle = ApptainerHandle(EXECUTION_ID, 1, name, image, f"{root}/{EXECUTION_ID}",
                                     f"{root}/{EXECUTION_ID}", execution_request(), "/fixture/image.sif")
        self.identity = dict(backend="apptainer-v1", unit=name, invocation="a" * 32, cgroup="/fixture/" + name,
                             device=1, inode=2, handle_sha256=self.handle.digest)
        self.summary = dict(phase="complete", exit_code=0, timed_out=False, truncated=False, error="",
                            stdout="b2s=", stderr="")
        self.retained = ApptainerStatus("retained", identity=self.identity, child_exit_code=0, summary=self.summary)

    def collect_payload(self, response, *, include_outputs=True):
        client = Mock()
        client.recv.side_effect = [json.dumps(response).encode(), b""]
        with patch.object(self.backend, "inspect", return_value=self.retained), \
                patch("src.code_execution.apptainer.socket.socket") as factory:
            factory.return_value.__enter__.return_value = client
            return self.backend.collect(self.handle, include_outputs=include_outputs)

    def test_error_requires_exact_identity_and_retained_diagnostics(self):
        response = dict(identity=self.identity, phase="retained", summary=self.summary, collection_error=MISSING)
        with self.assertRaisesRegex(OutputRejected, MISSING):
            self.collect_payload(response)
        with self.assertRaises(OutputRejected) as result:
            self.collect_payload({**response, "missing_paths": [OUTPUT]})
        self.assertEqual(result.exception.missing_paths, (OUTPUT,))
        cases = [
            ({**response, "missing_paths": ["/outputs/undeclared.txt"]}, "invalid_collected_output_rejection"),
            ({**response, "missing_paths": [OUTPUT, OUTPUT]}, "invalid_collected_output_rejection"),
            ({**response, "missing_paths": []}, "invalid_collected_output_rejection"),
            ({k: v for k, v in response.items() if k != "identity"}, "apptainer_control_identity_unconfirmed"),
            ({**response, "identity": {**self.identity, "invocation": "b" * 32}}, "apptainer_control_identity_unconfirmed"),
            ({**response, "identity": {**self.identity, "device": True}}, "invalid_apptainer_runtime_identity"),
            ({**response, "summary": {**self.summary, "stdout": ""}}, "apptainer_summary_changed"),
            ({**response, "phase": "running"}, "apptainer_summary_changed"),
            ({**response, "outputs": {}}, "apptainer_collector_record_invalid"),
            ({**response, "collection_error": "unknown_detail"}, "apptainer_collector_record_invalid"),
            ({**response, "collection_error": "apptainer_collector_timeout"}, "apptainer_collector_timeout"),
            ({"identity": self.identity, "error": "service_observation_unconfirmed"}, "apptainer_service_observation_unconfirmed"),
        ]
        for frame, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(OCIError, code) as result:
                self.collect_payload(frame)
            self.assertNotIsInstance(result.exception, OutputRejected)
        with self.assertRaisesRegex(OCIError, "apptainer_collector_record_invalid"):
            self.collect_payload(response, include_outputs=False)
        success = dict(identity=self.identity, phase="retained", summary=self.summary, outputs={OUTPUT: "YQ=="})
        self.assertEqual(self.collect_payload(success).outputs, {OUTPUT: b"a"})


class ControllerCollectionTests(unittest.TestCase):
    def setUp(self):
        self.worker = object.__new__(controller.ExecutionController)
        repository, backend = Mock(), Mock()
        self.worker.repository, self.worker.backend = repository, backend
        self.worker.worker_id = "fixture-worker"
        self.worker.base_dependency_versions = {}
        self.worker._uncertain_leases = {}
        self.worker.workspace_factory = Mock()
        self.worker.workspace_factory.return_value.open.return_value = {"task_id": "task"}
        self.worker._materialize = Mock(return_value=Path("/fixture/stage"))
        self.worker._record = Mock()
        self.worker._record_state = Mock()
        self.worker._terminal_callback = Mock()
        self.worker._release_hold = Mock(return_value="released")
        self.worker._cleanup_stopped = Mock()
        self.worker.resources = Mock()
        self.worker.logs = Mock()
        self.worker.logs.store.return_value = {"stdout": {"execution_id": EXECUTION_ID, "fence": 1}}
        self.row = dict(execution_id=EXECUTION_ID, fence=1, owner=7, session="session",
                        deadline=30, request=execution_request().serialize())
        repository.claim.return_value = self.row
        repository._now.return_value = 0
        repository.get.return_value = {"status": "collecting"}
        backend.inspect.return_value = SimpleNamespace(state="retained", child_exit_code=0, timed_out=False)
        backend.kill.return_value = SimpleNamespace(stopped=True)
        self.diagnostics = OCICollection({}, b"script out", b"script err", 0, False, False)
        self.lease = Mock()

    def run_worker(self):
        with patch.object(controller, "acquire_account_lease", return_value=self.lease), \
                patch.object(controller, "current_store") as store, patch.object(controller, "accept_outputs") as accept:
            store.return_value.check_epoch.return_value = True
            result = self.worker.run_one()
            accept.assert_not_called()
            self.worker.backend.start.assert_called_once()
            self.worker.backend.kill.assert_called_once()
            self.worker.repository.complete_success.assert_not_called()
            return result

    def test_preserves_diagnostics_and_never_accepts_partial_outputs(self):
        self.worker.backend.collect.side_effect = [self.diagnostics, OutputRejected(MISSING, [OUTPUT])]
        result = self.run_worker()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["receipt"]["reason"], "execution_output_rejected")
        self.assertEqual(result["receipt"]["error_code"], MISSING)
        self.assertEqual(result["receipt"]["missing_output_paths"], [OUTPUT])
        self.assertEqual(result["receipt"]["declared_output_paths"], [OUTPUT])
        self.assertNotIn("files", result["receipt"])
        self.assertEqual(result["receipt"]["stdout"]["log_ref"], {"execution_id": EXECUTION_ID, "fence": 1})
        self.assertTrue(result["receipt"]["process_stopped"])
        self.worker.logs.store.assert_called_once_with(self.row, stdout=b"script out", stderr=b"script err", capture_truncated=False)
        self.worker.backend.collect.assert_has_calls([call(self.worker.backend.prepare.return_value, include_outputs=False),
                                                     call(self.worker.backend.prepare.return_value)])
        self.assertEqual(self.worker.backend.collect.call_count, 2)
        self.worker.repository.finish.assert_called_once()
        self.lease.release.assert_called_once()

    def test_output_observation_failure_is_not_a_declaration_rejection(self):
        self.worker.backend.collect.side_effect = [self.diagnostics, OCIError("apptainer_control_identity_unconfirmed")]
        result = self.run_worker()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["receipt"]["reason"], "execution_output_collection_unconfirmed")
        self.assertEqual(result["receipt"]["error_code"], "apptainer_control_identity_unconfirmed")
        self.assertTrue(result["receipt"]["process_stopped"])

    def test_unconfirmed_stop_never_retries_or_finishes(self):
        self.worker.backend.collect.side_effect = [self.diagnostics, OCICollection({OUTPUT: b"a"},
                                                    b"script out", b"script err", 0, False, False)]
        self.worker.backend.kill.return_value = SimpleNamespace(stopped=False)
        result = self.run_worker()
        self.assertEqual(result["status"], "unknown")
        self.worker.repository.mark_unknown.assert_called_once_with(EXECUTION_ID, "fixture-worker", 1,
                                                                     reason="container_stop_unconfirmed")
        self.worker.repository.finish.assert_not_called()
        self.worker._cleanup_stopped.assert_not_called()
        self.worker._release_hold.assert_not_called()
        self.lease.release.assert_not_called()
        self.assertIs(self.worker._uncertain_leases[(EXECUTION_ID, 1)], self.lease)


if __name__ == "__main__":
    unittest.main()
