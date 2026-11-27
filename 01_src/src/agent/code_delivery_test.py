"""Code delivery uses the real publisher and durable operation boundary."""
import asyncio
from contextlib import closing, contextmanager, nullcontext
import hashlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.agent import code_tools
from src.agent.identity import ExecutionIdentity
from src.core import account_leases, jobs, publication_reconcile, task_events, tracing
from src.research import artifacts, tools
from src.session import files
from src.tools import registry

OWNER = 73
SESSION = "code-delivery-test-session"
REQUEST = "code-delivery-test-client-request"
TURN = "code-delivery-test-server-turn"
EXECUTION = "a" * 32
SOURCE = "b" * 32
SOURCE_PATH = "/outputs/report.txt"
DATA = b"one verified report\n"
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    from src import auth
    from src.session.manager import session_manager

    monkeypatch.setenv("CITRUS_CODE_EXECUTION", "offline-development")
    monkeypatch.setattr(account_leases, "account_lease", lambda owner: nullcontext())
    monkeypatch.setattr(auth, "user_by_id", lambda owner: {"active": owner == OWNER})
    monkeypatch.setattr(session_manager, "get_session_owner", lambda sid: OWNER if sid == SESSION else None)
    monkeypatch.setattr(session_manager, "session_belongs_to", lambda sid, owner: (sid, owner) == (SESSION, OWNER))
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "FILES_ROOT", tmp_path / "state" / "session_files")
    monkeypatch.setattr(files, "DB_PATH", tmp_path / "state" / "files.sqlite3")
    monkeypatch.setattr(jobs, "_DB_PATH", tmp_path / "state" / "jobs.sqlite3")
    monkeypatch.setattr(task_events, "remaining_seconds", lambda _: 10)
    monkeypatch.setattr(registry, "_check_tool_sandbox", AsyncMock(return_value=None))
    files.init_store()
    db = tools._db_path()
    job = jobs.claim_job(SESSION, TURN, owner_user_id=OWNER, client_request_id=REQUEST, payload={})
    identity = ExecutionIdentity(OWNER, SESSION, TURN, job.job_id)
    workspace = SimpleNamespace(open=Mock(return_value={}))
    service = SimpleNamespace(status=Mock(return_value={"workspace_id": "c" * 32}), workspace=workspace)
    monkeypatch.setattr(code_tools, "_service", lambda _: service)

    source = {"path": SOURCE_PATH, "file_id": SOURCE, "sha256": DIGEST, "size_bytes": len(DATA),
              "mime_type": "text/plain"}
    accepted = Mock(return_value=(source, DATA))

    @contextmanager
    def accept(service_arg, identity_arg, execution_id, path):
        assert service_arg is service and identity_arg == identity
        assert (execution_id, path) == (EXECUTION, SOURCE_PATH)
        ref, data = accepted()
        yield None, ref, data

    monkeypatch.setattr(code_tools, "_accepted_file", accept)
    publisher = Mock(wraps=artifacts.publish_artifact)
    monkeypatch.setattr(artifacts, "publish_artifact", publisher)

    async def invoke(call_id):
        tracing.set_user_id(OWNER)
        tracing.set_session_id(SESSION)
        tracing.set_job_id(job.job_id)
        tracing.set_client_request_id(REQUEST)
        with code_tools.code_execution_context(identity, call_id):
            return await registry.run_tool_operation(code_tools.code_deliver, {"id": call_id,
                "name": "code_deliver", "args": {"execution_id": EXECUTION, "path": SOURCE_PATH}},
                require_durable=True, logical_key="test-delivery")

    return SimpleNamespace(invoke=lambda call_id="code-delivery-call": asyncio.run(invoke(call_id)),
                           accepted=accepted, source=source, service=service, publisher=publisher,
                           job=job.job_id, db=db, root=tmp_path)


def _operations(delivery):
    with task_events.read_transaction() as conn:
        return conn.execute("SELECT * FROM task_operations WHERE job_id=? ORDER BY updated_at",
                            (delivery.job,)).fetchall()


def _publications(delivery):
    with closing(sqlite3.connect(delivery.db)) as conn:
        return conn.execute("SELECT artifact_id,state FROM publication_outbox ORDER BY artifact_id").fetchall()


@pytest.mark.parametrize("rejection", ["missing_execution", "private_workspace", "accepted_source", "invalid_format"])
def test_known_rejection_is_a_durable_not_run_without_artifact_write(delivery, rejection):
    if rejection == "missing_execution":
        delivery.service.status.side_effect = LookupError("execution_not_found")
    elif rejection == "private_workspace":
        delivery.service.workspace.open.return_value = {"metadata": {"code_scope": code_tools.FUSION_CODE_SCOPE}}
    elif rejection == "accepted_source":
        delivery.accepted.side_effect = PermissionError("code_output_binding_mismatch")
    else:
        delivery.accepted.return_value = (delivery.source | {"sha256": hashlib.sha256(b" ").hexdigest(),
                                                          "size_bytes": 1}, b" ")

    message = delivery.invoke()
    assert message.additional_kwargs["execution_dispatched"]
    assert message.additional_kwargs["host_preflight_reconciled"]["operation_id"] == message.additional_kwargs["operation_id"]
    assert not message.additional_kwargs.get("operation_uncertain")
    assert message.artifact["op_receipt"]["status"] == "not_run"
    assert _operations(delivery)[0]["status"] == "not_run"
    assert not _publications(delivery)
    assert delivery.publisher.call_count == (1 if rejection == "invalid_format" else 0)


def test_unattested_not_run_cannot_bypass_durable_reconciliation(delivery, monkeypatch):
    monkeypatch.setattr(code_tools, "_deliver", lambda **kwargs: code_tools._result(
        {"error": "code_delivery_preflight_rejected"}, reason="code_delivery_preflight_rejected", status="not_run"))
    message = delivery.invoke()
    assert message.additional_kwargs["operation_uncertain"]
    assert "host_preflight_reconciled" not in message.additional_kwargs
    assert _operations(delivery)[0]["status"] == "unknown"


def test_verified_delivery_reuses_one_immutable_publication(delivery):
    first = delivery.invoke()
    second = delivery.invoke("code-delivery-deliberate-second-call")
    assert first.artifact["artifact_url"] == second.artifact["artifact_url"]
    assert first.artifact["file_id"] == second.artifact["file_id"] != SOURCE
    [artifact] = first.artifact["op_receipt"]["coverage"]["publication"]
    assert artifact["stage"] == "confirmed" and artifact["sha256"] == DIGEST
    assert artifact["url"] == first.artifact["artifact_url"]
    assert _publications(delivery) == [(artifact["artifact_id"], "committed")]
    assert [op["status"] for op in _operations(delivery)] == ["complete", "complete"]


@pytest.mark.parametrize("failure", ["registration", "link_verification", "before_stage_observed"])
def test_ambiguous_delivery_is_query_only_and_never_resubmits(delivery, monkeypatch, failure):
    register = publication_reconcile.register_publication
    if failure == "registration":
        monkeypatch.setattr(publication_reconcile, "register_publication", Mock(side_effect=OSError("interrupted")))
    elif failure == "link_verification":
        monkeypatch.setattr(tools, "validate_artifact_links", lambda *args, **kwargs: [{"valid": False}])
    else:
        delivery.publisher.side_effect = OSError("operation state not observable")
    message = delivery.invoke()
    assert message.additional_kwargs["operation_uncertain"]
    assert "host_preflight_reconciled" not in message.additional_kwargs
    assert message.artifact["_tool_outcome"]["reason_code"] == "write_requires_reconciliation"
    assert not message.artifact["_tool_outcome"]["artifacts"] and "artifact_url" not in message.artifact
    [operation] = _operations(delivery)
    assert (operation["status"], operation["recovery_mode"]) == ("unknown", "never")
    with pytest.raises(task_events.TaskConflict, match="recovery_requires_reconciliation"):
        delivery.invoke()
    assert delivery.publisher.call_count == 1
    evidence = publication_reconcile.operation_publication_evidence(owner=OWNER, session_id=SESSION,
                                                                       job_id=delivery.job)
    if failure == "before_stage_observed":
        assert evidence == [] and _publications(delivery) == []
        return
    [item] = evidence
    assert (item["stage"], item["sha256"], item["url"]) == ("committed", DIGEST, "")
    assert item["operation_id"] == operation["operation_id"]
    state = "pending" if failure == "registration" else "committed"
    assert _publications(delivery) == [(item["artifact_id"], state)]  # querying changed nothing
    restored = register(db_path=delivery.db, artifact_id=item["artifact_id"], project_root=delivery.root,
                        expected_scope=(OWNER, SESSION, REQUEST, DIGEST))
    assert restored["sha256"] == DIGEST
    assert _publications(delivery) == [(item["artifact_id"], "committed")]
    assert delivery.publisher.call_count == 1 and _operations(delivery)[0]["status"] == "unknown"
