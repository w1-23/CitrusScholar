"""Production operation/publishing adapters for the generic capability scheduler.

These adapters reuse account permissions, durable operation leases, artifact
verification and full-result storage. They do not own a model loop.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path
import uuid

from langchain_core.messages import ToolMessage

from .execution import ToolExecution
from .identity import ExecutionIdentity


@dataclass
class HostTools:
    identity: ExecutionIdentity
    state: dict
    records: list[dict] = field(default_factory=list)
    messages: dict[str, ToolMessage] = field(default_factory=dict)
    dispatched: dict[str, str] = field(default_factory=dict)
    main_results: list = field(default_factory=list)
    web_results: list = field(default_factory=list)
    pending_question: dict | None = None
    allowed_names: set[str] = field(default_factory=set)
    published_resolutions: dict[str, str] = field(default_factory=dict)
    pending_result_reads: dict[str, str] = field(default_factory=dict)
    skill_release: Path | None = None
    turn_skills: "TurnSkills | None" = None
    # Populated only by the send seam after native context editing and admission.
    historical_result_offers: set[str] = field(default_factory=set)

    def historical_result_reference(self, message):
        """Prove exact older inline bytes are available through the existing reader."""
        from sqlite3 import Error as StorageError
        from src.core import jobs, tool_results
        from .installed_skills import is_rqa_tool_result

        if is_rqa_tool_result(message.name, message.content):
            return None
        meta = message.additional_kwargs
        record, ref = meta.get("host_publication"), meta.get("result_reference")
        if (meta.get("context_trusted") is not True or meta.get("context_origin") != "tool"
                or meta.get("session_id") != self.identity.session_id
                or not isinstance(record, dict) or not isinstance(ref, dict)
                or record.get("tool_call_id") != message.tool_call_id
                or record.get("status") not in {"complete", "partial", "no_results", "failed", "not_run"}
                or record.get("reason_code") == "batch_observation_not_stored"
                or meta.get("operation_uncertain") or meta.get("model_delivery_issue")
                or not isinstance(message.content, str)):
            return None
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        if artifact.get("user_question") or artifact.get("_code_execution_id") or any(
                isinstance(artifact.get(key), dict) and artifact[key].get("status") in
                {"unknown", "pending", "waiting", "running", "unfinished", "submitted"}
                for key in ("_tool_outcome", "op_receipt")):
            return None
        try:
            from src.research.result_tools import _shared_result_accessible

            origin = ExecutionIdentity(self.identity.owner_user_id, self.identity.session_id,
                                       meta["context_turn_id"], meta["run_id"])
            if origin.turn_key == self.identity.turn_key or origin.run_id == self.identity.run_id:
                return None
            key = tool_results.execution_receipt_key(origin, message.tool_call_id)
            if ref.get("tool_call_id") != key or ref.get("provider_call_id") != message.tool_call_id:
                return None
            job = jobs.get_job(origin.run_id)
            if (not job or job["owner_user_id"] != origin.owner_user_id
                    or job["session_id"] != origin.session_id or job["request_id"] != origin.user_turn_id
                    or job["status"] not in jobs.TERMINAL_STATUS - {"awaiting_input"}):
                return None
            row = tool_results.fetch_for_current_request(key, strict=True)
            if (not row or row["tool_call_id"] != key or row["provider_call_id"] != message.tool_call_id
                    or row["session_id"] != origin.session_id or row["tool_name"] != message.name
                    or row["request_id"] != (job.get("client_request_id") or origin.user_turn_id)
                    or ref.get("request_id") != row["request_id"]
                    or row["status"] != record["status"] or row["result_text"] != message.content
                    or not row["pinned"] or not tool_results.storage_integrity(row)["complete"]
                    or ref.get("full_sha256") != row["full_sha256"]
                    or not _shared_result_accessible(row, message.content, tool_results.fetch_for_current_request,
                                                     allow_rqa=False)):
                return None
        except (ImportError, KeyError, OSError, StorageError, TypeError, ValueError, RecursionError):
            return None
        return {"tool_call_id": key, "full_sha256": row["full_sha256"],
                "original_chars": len(message.content), "execution_status": row["status"]}

    def result_readbacks(self):
        """Index issued continuation cursors for authorized result offers."""
        from src.core.tool_results import observation_receipt_key

        eligible = {key: {} for key in self.historical_result_offers}
        eligible.update({observation_receipt_key(message): {} for message in self.messages.values()
                         if message.additional_kwargs.get("first_delivery_reference") is True})
        for message in self.messages.values():
            if message.name != "read_tool_result":
                continue
            try:
                observation = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if not isinstance(observation, dict) or observation.get("status") != "ok":
                continue
            target = observation.get("tool_call_id")
            fields, page = observation.get("selected_fields"), observation.get("page")
            cursor = page.get("next_cursor") if isinstance(page, dict) else None
            if (isinstance(target, str) and target in eligible and isinstance(fields, list)
                    and all(isinstance(field, str) for field in fields) and isinstance(cursor, str)
                    and type(page.get("next_start_char")) is int):
                eligible[target][cursor] = observation
        return eligible

    def has_result_readback(self):
        return bool(self.result_readbacks())

    def _prepare_result_readback(self, args, call_id):
        target, token = args.get("tool_call_id"), args.get("cursor", "")
        start = args.get("start_char", 0)
        if not isinstance(target, str) or type(start) is not int or start < 0:
            return False
        selections = self.result_readbacks().get(target)
        if selections is None or target in self.pending_result_reads:
            return False
        if token:
            if not isinstance(token, str) or args.get("fields") is not None or start:
                return False
            previous = selections.get(token)
            if previous is None:
                return False
            args["start_char"] = previous["page"]["next_start_char"]
            args["fields"] = previous["selected_fields"] or None
        else:
            fields = args.get("fields")
            if fields is not None and (not isinstance(fields, list)
                    or any(not isinstance(field, str) for field in fields)):
                return False
        self.pending_result_reads[target] = call_id
        return True

    def publish_resolutions(self, outcome=None):
        from src.core.task_result import resolved_tool_attempts
        from src.core.progress_bus import emit_encoded
        resolutions = {item['tool_call_id']: item for item in resolved_tool_attempts(self.records, outcome)}
        changes = []
        for item in resolutions.values():
            cid, target = item["tool_call_id"], item["resolved_by_tool_call_id"]
            if self.published_resolutions.get(cid) != target:
                changes.append(item)
                self.published_resolutions[cid] = target
        if changes:
            emit_encoded("tool_resolution", {"resolutions": changes})

    def _result_request_id(self):
        """Result storage uses the job's client turn key, not its server trace ID."""
        from src.core import jobs
        from src.core.task_events import TaskConflict
        job = jobs.get_job(self.identity.run_id)
        if (not job or job['owner_user_id'] != self.identity.owner_user_id
                or job['session_id'] != self.identity.session_id
                or job['request_id'] != self.identity.user_turn_id):
            raise TaskConflict('agent_execution_identity_conflict')
        return str(job.get('client_request_id') or self.identity.user_turn_id)

    def proven_no_dispatch(self, cid):
        observation = self.messages.get(cid)
        if observation is None or observation.additional_kwargs.get('operation_uncertain'):
            return False
        if cid not in self.dispatched and not observation.additional_kwargs.get('execution_dispatched'):
            return True
        marker = observation.additional_kwargs.get('host_preflight_reconciled')
        if not isinstance(marker, dict):
            return False
        from src.core import task_events, tool_results
        operation_id = marker.get('operation_id')
        if (marker.get('schema_version') != 1 or not operation_id
                or operation_id != observation.additional_kwargs.get('operation_id')):
            return False
        with task_events.transaction() as conn:
            row = conn.execute("SELECT input_hash, inputs, input_refs, status, result FROM task_operations "
                "WHERE operation_id=? AND job_id=? AND owner_user_id=? AND tool_name=? AND tool_call_id=?",
                (operation_id, self.identity.run_id, self.identity.owner_user_id, observation.name, cid)).fetchone()
        if row is None or row['status'] != 'not_run' or not row['result']:
            return False
        args, refs = json.loads(row['inputs']), json.loads(row['input_refs'])
        if row['input_hash'] != task_events.input_hash({'tool': observation.name, 'args': args, 'refs': refs}):
            return False
        result = json.loads(row['result'])
        stored = tool_results.fetch(tool_results.observation_receipt_key(observation))
        return bool(stored and stored.get('session_id') == self.identity.session_id
            and stored.get('request_id') == self._result_request_id()
            and tool_results.storage_integrity(stored)['complete']
            and result.get('content') == stored['result_text']
            and marker.get('result_sha256') == hashlib.sha256(stored['result_text'].encode('utf-8')).hexdigest())

    def assert_context(self):
        from src.core import jobs, tracing
        from src.core.task_events import TaskConflict
        job = jobs.get_job(self.identity.run_id)
        if (tracing.get_job_id() != self.identity.run_id
                or tracing.get_user_id() != self.identity.owner_user_id
                or tracing.get_session_id() != self.identity.session_id
                or not job or job["owner_user_id"] != self.identity.owner_user_id
                or job["session_id"] != self.identity.session_id
                or job["request_id"] != self.identity.user_turn_id
                or job["status"] not in {"running", "detached"}):
            raise TaskConflict("agent_execution_identity_conflict")

    @staticmethod
    def _retry_rejected(call):
        reason = 'browse_retry_rejected'
        return ToolExecution(ToolMessage(content=json.dumps({'error': reason}), status='error',
            name=call['name'], tool_call_id=call['id'],
            artifact={'_tool_outcome': {'status': 'not_run', 'reason_code': reason}}))

    def _browse_receipt(self, message):
        from src.core.task_events import TaskConflict, operation_result
        operation_id = message.additional_kwargs.get('operation_id')
        if not operation_id or message.additional_kwargs.get('operation_uncertain'):
            return None
        try:
            receipt = operation_result(self.identity.run_id, operation_id, include_retry=True)
        except TaskConflict:
            return None
        reference = message.additional_kwargs.get('result_reference') or {}
        digest = hashlib.sha256(receipt['result']['content'].encode('utf-8')).hexdigest()
        return receipt if reference.get('full_sha256') == digest else None

    def _retry_target(self, message, record):
        from src.tools.browse_web import NAVIGATION_FAILURES
        if (message.name != 'browse_web' or record.get('status') != 'failed'
                or record.get('reason') not in NAVIGATION_FAILURES):
            return None
        self.validate_observation(message)
        receipt = self._browse_receipt(message)
        if (not receipt or receipt['outcome'].get('status') != 'failed'
                or receipt['outcome'].get('reason_code') != record['reason']):
            return None
        target = receipt['result']['artifact'].get('_browse_retry_target')
        return {**target, 'operation_id': receipt['operation_id']} if target else None

    def _find_retry_parent(self, operation_id):
        for record in self.records:
            message = self.messages.get(record.get('tool_call_id'))
            if message and message.additional_kwargs.get('operation_id') == operation_id:
                return message, record
        return None, None

    def _claim_target(self, call, retry_of):
        if not isinstance(retry_of, str) or not re.fullmatch(r'[a-f0-9]{32}', retry_of):
            return None
        parent, row = self._find_retry_parent(retry_of)
        if parent is None:
            return None
        try:
            target = self._retry_target(parent, row)
        except RuntimeError:
            return None
        args = call['args']
        if (not target or args.get('action') != target['action']
                or (args.get('tab_id') and args['tab_id'] != target['tab_id'])):
            return None
        from src.browse.relay import BrowseError, Relay
        from urllib.parse import urlsplit
        try:
            host = urlsplit(target['url']).hostname
            destination = Relay(frozenset({host})).authorize(args.get('url'))
        except BrowseError:
            return None
        return target if destination.url == target['url'] else None

    def _link_successful_retry(self, message, record):
        record.pop('retry_resolves_call_id', None)
        if message.name != 'browse_web' or record.get('status') != 'complete':
            return
        receipt = self._browse_receipt(message)
        if not receipt or not receipt['retry_of'] or receipt['outcome'].get('status') != 'complete':
            return
        parent, original = self._find_retry_parent(receipt['retry_of'])
        if parent and self._retry_target(parent, original):
            record['retry_resolves_call_id'] = parent.tool_call_id

    async def invoke(self, tool, call, identity):
        if identity != self.identity:
            raise ValueError("operation_identity_mismatch")
        self.assert_context()
        from src.tools.registry import run_tool_operation
        from src.core.execution_ledger import current_execution_ledger
        from src.core.progress_bus import emit_tool_call_start, emit_tool_executing
        from src.tools.interaction import bind_question_context, reset_question_context
        from src.core.task_events import TaskConflict
        call = deepcopy(call)
        if call["name"] == "read_tool_result" and not self._prepare_result_readback(call["args"], call["id"]):
            reason = "result_readback_not_offered"
            return ToolExecution(ToolMessage(content="回执无需回读，或续读标记失效。请使用已有结果。",
                status="error", name=call["name"], tool_call_id=call["id"],
                additional_kwargs={"execution_dispatched": False},
                artifact={"_tool_outcome": {"status": "not_run", "reason_code": reason}}))
        logical_key = 'host:' + uuid.uuid4().hex
        retry_of = call['args'].get('retry_of') if call['name'] == 'browse_web' else None
        retry_target = self._claim_target(call, retry_of) if retry_of else None
        if retry_of and retry_target is None:
            return self._retry_rejected(call)
        attempt = 1
        if retry_target:
            parent_message, parent = self._find_retry_parent(retry_of)
            attempt += parent['attempt']
            # The parent was verified against its durable operation before
            # reusing its trace group. Independent same-name calls never merge.
            parent_trace = (parent_message.artifact or {}).get('_citrus_execution') or {}
            logical_key = parent_trace.get('logical_key') or logical_key
        ledger = current_execution_ledger()
        if ledger:
            ledger.start(call["id"], call["name"], call["args"], source="provider",
                         logical_key=logical_key, attempt=attempt)
        emit_tool_call_start(call["name"], call["args"], call["id"],
                             logical_key=logical_key, attempt=attempt)
        emit_tool_executing("正在执行所选操作", call["name"], call["id"],
                            logical_key=logical_key, attempt=attempt)
        token = bind_question_context(self.question_context())
        from src.core import tracing
        from src.graph.retrieval_router import retrieval_denied, source_scope
        query = str(self.state.get("query") or "")
        previous_network = tracing.web_search_enabled()
        # Composite tools (e.g. a durable reference audit) also make HTTP calls.
        # Propagate explicit source restrictions to their existing network gate;
        # catalogue metadata alone cannot enforce a composite tool's internals.
        tracing.set_web_search_enabled(previous_network
            and source_scope(query, exclusive_only=True) != "local"
            and not retrieval_denied(query, "web"))
        def dispatched():
            self.dispatched[call["id"]] = call["name"]
            if call['name'] == 'verify_uploaded_references' and call['args'].get('operation', 'run') == 'run':
                from src.core.public_progress import emit_tool_milestone
                emit_tool_milestone('checking_references', job_id=identity.run_id, tool_call_id=call['id'])
        try:
            from contextlib import nullcontext
            from .code_tools import CODE_TOOL_NAMES, EXECUTION_WAIT_TOOL_NAMES, code_execution_context
            from .fusion_tools import FUSION_TOOL_NAMES, FUSION_WAIT_TOOLS
            from .nature_skill_tools import NATURE_TOOL_NAMES, NATURE_WAIT_TOOLS
            from .installed_skills import TurnSkills, bind_turn_skills
            binding = (code_execution_context(identity, call['id'], skill_release=self.skill_release)
                       if call['name'] in (CODE_TOOL_NAMES | FUSION_TOOL_NAMES | NATURE_TOOL_NAMES) or call['name'] == 'audit_uploaded_data'
                       else nullcontext())
            retry_binding = nullcontext()
            if retry_target:
                from src.tools.browse_web import bind_navigation_retry
                retry_binding = bind_navigation_retry(retry_target)
            with binding, retry_binding, bind_turn_skills(self.turn_skills or TurnSkills(self.skill_release)):
                message = await run_tool_operation(tool, call, require_durable=True,
                                                 on_dispatch=dispatched, retry_of=retry_of or None,
                                                 logical_key=logical_key, attempt=attempt)
        except TaskConflict as exc:
            if str(exc) != 'browse_retry_rejected':
                raise
            return self._retry_rejected(call)
        finally:
            tracing.set_web_search_enabled(previous_network)
            reset_question_context(token)
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        question = artifact.get("user_question")
        if call["name"] == "request_user_input" and isinstance(question, dict):
            self.pending_question = question
        execution_waits = ()
        if (call['name'] in (EXECUTION_WAIT_TOOL_NAMES | FUSION_WAIT_TOOLS | NATURE_WAIT_TOOLS)
                and artifact.get('_code_execution_id')
                and (artifact.get('_tool_outcome') or {}).get('status') == 'complete'):
            import asyncio
            from src.code_execution.service import ExecutionService
            from src.code_execution.waits import ExecutionWaits
            # Only the trusted adapter's authenticated execution reference is
            # adopted. The model cannot supply wait IDs, claims or acknowledgments.
            waits = ExecutionWaits(ExecutionService(identity))
            watched = await asyncio.to_thread(waits.watch, artifact['_code_execution_id'],
                                              tool_call_id=call['id'])
            execution_waits = (watched['wait_id'],)
        return ToolExecution(message,
            operation_id=str(message.additional_kwargs.get("operation_id") or ""),
            uncertain=bool(message.additional_kwargs.get("operation_uncertain")),
            pending_question=self.pending_question is not None,
            execution_waits=execution_waits, logical_key=logical_key, attempt=attempt)

    async def publish(self, call, execution, identity):
        if identity != self.identity:
            raise ValueError("publication_identity_mismatch")
        from src.graph.agent_operations import _emit_completed_tool_step
        from src.core.progress_bus import emit_encoded
        from src.core.tool_result_view import delivery_issue, first_delivery
        from .batch_journal import persistent_message, retain_observation
        original = execution.message
        message = original.model_copy(update={"additional_kwargs": dict(original.additional_kwargs)})
        records = []
        _emit_completed_tool_step(call, message, records,
                                  logical_key=execution.logical_key or 'host:' + uuid.uuid4().hex,
                                  attempt=execution.attempt, receipt_identity=identity)
        if not records:
            raise RuntimeError("operation_publication_unavailable")
        record = records[0]
        storage = message.additional_kwargs.get("result_storage") or {}
        if not storage.get("complete"):
            # The operation receipt and its model delivery settle separately.
            emit_encoded("context_degraded", {"stage": "tool_delivery", "status": "degraded",
                "reason_code": "result_storage_unavailable", "tool_call_id": message.tool_call_id,
                "message": "操作已记录，但暂时无法核验回执原文。请先查看已有记录。"})
            raise RuntimeError("operation_result_not_persisted")
        from src.core.tool_results import observation_receipt_key, pin_references
        if not pin_references(identity.session_id, [observation_receipt_key(message)]):
            raise RuntimeError("checkpoint_evidence_pin_failed")
        delivered = first_delivery(message, call=call,
                                   reader_available="read_tool_result" in self.allowed_names)
        issue = delivery_issue(delivered) if delivered is not message else None
        if issue:
            emit_encoded("context_degraded", {"stage": "tool_delivery", "status": "degraded",
                "reason_code": issue[0], "tool_call_id": message.tool_call_id, "message": issue[1]})
        if isinstance(record.get('op_receipt'), dict):
            from .batch_journal import observation_json
            try:
                record['op_receipt'] = json.loads(observation_json(record['op_receipt']))
            except (ValueError, TypeError, RecursionError):
                record['op_receipt'] = {}
        delivered.additional_kwargs['host_publication'] = record
        if delivered.name == 'browse_web':
            self.validate_observation(delivered)
            self._link_successful_retry(delivered, record)
            if target := self._retry_target(delivered, record):
                payload = json.loads(delivered.content)
                payload['host_browse_retry'] = {'retry_of': target['operation_id'], 'tab_id': target['tab_id']}
                delivered = delivered.model_copy(update={'content': json.dumps(payload, ensure_ascii=False)})
        delivered.additional_kwargs['host_publication'] = dict(record)
        try:
            if original.artifact is not None and not isinstance(original.artifact, dict):
                raise TypeError("unsupported_tool_artifact")
            # The JSON boundary recorded any wrapper it could not encode
            # losslessly. Refuse a clean success in that case so the turn keeps
            # the honest partial/unknown outcome instead of claiming success.
            if (isinstance(delivered.artifact, dict)
                    and delivered.artifact.get("_unsupported_types")):
                raise TypeError("unsupported_tool_artifact_value")
            delivered = persistent_message(delivered)
        except (TypeError, ValueError, RecursionError):
            status = 'unknown' if execution.uncertain else 'partial' if record['status'] in {'complete', 'partial'} else record['status']
            record.update(status=status, reason='batch_observation_not_stored')
            delivered.additional_kwargs['host_publication'] = dict(record)
            metadata = {key: delivered.additional_kwargs[key] for key in (
                'operation_id', 'operation_uncertain', 'execution_dispatched', 'result_reference',
                'result_storage', 'host_publication', 'host_preflight_reconciled',
            ) if key in delivered.additional_kwargs}
            delivered = persistent_message(ToolMessage(
                content="回执已保存，但部分观察无法编码。请按原调用读取回执、查看已登记成果，勿重复执行。",
                tool_call_id=call['id'], name=call['name'], status='error', additional_kwargs=metadata,
                artifact={'_tool_outcome': {'status': status, 'reason_code': 'batch_observation_not_stored'},
                          '_artifact_validations': record['artifact_validations']},
            ))
            emit_encoded("context_degraded", {"stage": "tool_delivery", "status": "degraded",
                "reason_code": "batch_observation_not_stored", "tool_call_id": call['id'],
                "message": "部分工具观察无法编码，已登记的成果仍可查看。"})
        artifact = delivered.artifact if isinstance(delivered.artifact, dict) else {}
        has_inline_figure = (delivered.name == "draw_svg" or
            delivered.name == "run_research_figure_script" and (
                artifact.get("inline_registered") is True or bool(artifact.get("figure_placeholders"))))
        if has_inline_figure and execution.operation_id and not execution.uncertain:
            from src.core.figures import export_recovery
            retained = delivered.model_copy(update={"additional_kwargs": {
                **delivered.additional_kwargs, "host_figure_recovery": export_recovery(identity)}})
            try:
                # This copy is private to the owner-scoped operation outbox.
                retain_observation(identity, execution, retained, call)
            except (TypeError, ValueError, RecursionError):
                # A too-large outbox copy must not undo a published drawing.
                # A missing early receipt prevents recovery from rerunning it.
                import logging
                logging.getLogger(__name__).warning("figure_observation_recovery_state_too_large")
        else:
            retain_observation(identity, execution, delivered, call)
        self.records.append(record)
        self._adopt_observation(delivered)
        if call["name"] == "read_tool_result":
            target = call["args"].get("tool_call_id")
            if isinstance(target, str) and self.pending_result_reads.get(target) == call["id"]:
                self.pending_result_reads.pop(target)
        if record.get('status') == 'complete':
            category = ''
            if call['name'] in {'inspect_uploaded_table', 'audit_uploaded_table', 'audit_uploaded_data', 'audit_uploaded_data_bundle'}:
                category = 'table_processed'
            elif call['name'] in {'run_research_figure_script', 'draw_svg'} and record.get('artifact_count'):
                category = 'figure_registered'
            if category:
                from src.core.public_progress import emit_tool_milestone
                emit_tool_milestone(category, job_id=identity.run_id, tool_call_id=call['id'])
        self.publish_resolutions()
        return delivered

    def validate_observation(self, message):
        """Check retained original bytes before adopting checkpoint evidence."""
        self.assert_context()
        from src.core import tool_results
        key = tool_results.observation_receipt_key(message)
        stored = tool_results.fetch(key)
        reference = message.additional_kwargs.get("result_reference") or {}
        scoped = tool_results.execution_receipt_key(self.identity, message.tool_call_id)
        if (not isinstance(reference, dict) or key not in {message.tool_call_id, scoped}
                or (reference.get("provider_call_id") and key != scoped)
                or (key == scoped and (stored or {}).get("provider_call_id") != message.tool_call_id)):
            raise RuntimeError("recovery_result_identity_mismatch")
        record = message.additional_kwargs.get("host_publication")
        if (not stored or stored.get("session_id") != self.identity.session_id
                or stored.get("request_id") != self._result_request_id()
                or not tool_results.storage_integrity(stored)["complete"]
                or stored.get("tool_name") != message.name
                or reference.get("full_sha256") != stored.get("full_sha256")
                or not isinstance(record, dict) or record.get("tool_call_id") != message.tool_call_id):
            raise RuntimeError("recovery_result_evidence_unavailable")
        return record

    def restore_observation(self, message):
        """Hydrate a committed outbox result without re-publishing bounded text."""
        record = dict(self.validate_observation(message))
        self._link_successful_retry(message, record)
        self.records.append(record)
        self._adopt_observation(message)

    def _adopt_observation(self, message):
        from src.graph.agent_operations import _capture_interaction_refs
        self.messages[message.tool_call_id] = message
        _capture_interaction_refs(self.state, [message])
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        self.main_results.extend(artifact.get("main_results") or [])
        self.web_results.extend(artifact.get("web_results") or [])
        if message.name == "request_user_input" and isinstance(artifact.get("user_question"), dict):
            self.pending_question = artifact["user_question"]

    def working_state(self):
        """References survive; historical queries do not become permanent constraints."""
        from src.graph.agent_operations import _task_working_state
        working = _task_working_state(self.state, str(self.state.get("query") or ""), self.records, [])
        # Legacy state already contains whole queries disguised as constraints.
        # Keep real scoped constraints, but don't perpetuate the legacy ':user'
        # entries; their original text remains in searchable session history.
        previous = self.state.get("context_working_state") or {}
        working["retired_constraint_ids"] = [item["source_id"] for item in previous.get("constraints", [])
            if isinstance(item, dict) and str(item.get("source_id") or "").endswith(":user")]
        working["constraints"] = [item for item in previous.get("constraints", [])
            if isinstance(item, dict) and not str(item.get("source_id") or "").endswith(":user")]
        # Host-internal projection reuse metadata; it is filtered from the model
        # prompt and must survive turn/recovery boundaries with the working state.
        if isinstance(previous.get("projection_cache"), dict) and previous["projection_cache"]:
            working["projection_cache"] = previous["projection_cache"]
        return working

    def question_context(self):
        working = self.working_state()
        from .installed_skills import stored_release_pin

        return {"query": str(self.state.get("query") or ""),
            **stored_release_pin(self.state),
            "context_working_state": working,
            **{key: working.get(key, []) for key in ("source_refs", "dataset_refs", "script_refs", "run_refs")},
            "attachment_refs": self.state.get("attachment_refs") or [],
            "visualization_enabled": bool(self.state.get("visualization_enabled")),
            "browse_requested": self.state.get("browse_requested") is True,
            "selected_skills": list(self.state.get("selected_skills") or []),
            "context_compression": bool(self.state.get("context_compression", True)),
            "tool_status": [dict(r) for r in self.records], "no_replay": True}
