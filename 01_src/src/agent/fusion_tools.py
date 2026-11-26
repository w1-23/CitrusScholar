"""Small AgentCore adapters for pinned external Skill execution and delivery."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from src.graph.task_contract import FusionDelivery
from src.research.fusion_delivery import summary as _summary

SKILL_IDS = frozenset({"literature-verification", "data-audit", "research-integrity"})
FUSION_WAIT_TOOLS = frozenset({"run_external_skill", "continue_external_literature"})
REPLACED_TOOLS = frozenset({"verify_uploaded_references", "audit_uploaded_data", "audit_research_images",
                            "fetch_and_audit_open_supplements"})


_delivery = ContextVar("fusion_delivery", default=(FusionDelivery(), "", ""))


@contextmanager
def fusion_delivery_scope(contract, *, query="", previous_query=""):
    """Host scope only. A tool call cannot grant itself a delivery format."""
    token = _delivery.set((contract, query, previous_query))
    try:
        yield
    finally:
        _delivery.reset(token)


def external_fusion_required():
    return os.environ.get("CITRUS_EXTERNAL_FUSION") == "required"


_FINDINGS_REQUEST = re.compile(
    r"(?:(?:把|将)?[^，。！？!?]{0,30}(?:审查|发现|异常|重复|核验|具体位置)[^，。！？!?]{0,20}"
    r"(?:清单|名单|列表|列出|罗列|导出|下载|标红|标注|工作簿)|"
    r"(?:列出|罗列|给出|导出|下载)[^，。！？!?]{0,20}(?:审查|发现|异常|重复|核验|位置)|"
    r"\b(?:list|export|show|download)\b[^.?!]{0,45}\b(?:findings?|duplicate blocks?|audit results?|verification results?)\b)", re.I)
_SHORT_LIST = re.compile(r"(?:好[的吧]?\s*[，,]?\s*)?(?:请你?|帮我|麻烦你?)?\s*"
                         r"(?:给(?:我)?(?:一份|一个)?(?:完整)?(?:清单|列表)|列(?:出|一下|出来)(?:清单|结果|位置)?)"
                         r"(?:即可|就行|谢谢)?[。！!\s]*")
_CONTINUE = re.compile(r"(?:好[的吧]?\s*[，,]?\s*)?(?:请|你)?\s*(?:继续|再试(?:一次|一下)?)"
                       r"(?:试(?:试|一次|一下)?|吧|即可)?[。！!\s]*")
_DUPLICATE_REQUEST = re.compile(r"重复(?:数值|数字|数据)?(?:区段|区块|块|段|项)|\bduplicate (?:numeric |data )?blocks?\b", re.I)
_SOURCE_LABEL = re.compile(r"(?<![\w./])([A-Za-z0-9][\w. ()-]{0,79}\.(?:csv|xlsx|bib|ris|docx|txt))(?![\w/])", re.I)
_REPORT_ID = re.compile(r"(?<![a-fA-F0-9])[a-f0-9]{32}(?![a-fA-F0-9])")


_NEW_REFERENCE = re.compile(r"\b(?:10\.\d{4,9}/|arxiv\s*[:/]|arxiv\.org/)|(?:新(?:的|增)?|另[一个查]|再(?:查|核验|审查))[^，。！？!?]{0,20}(?:文献|引用|参考|数据|文件|DOI)", re.I)
_PRIOR_REPORT_REFERENCE = re.compile(
    r"(?:之前|此前|先前|刚才|上(?:一)?(?:份|次|轮|面)|原(?:来的)?(?:审查|核验|报告)|另存为|"
    r"那[一几\d\s]{0,12}(?:份|处|个|些|次)|已有(?:的)?(?:审查|报告)|"
    r"\b(?:previous|earlier|last|prior|existing|saved|that)\s+(?:findings?|report|audit|verification|results?|duplicate blocks?)\b)", re.I)


def fusion_report_followup(query, *, previous_query=""):
    from src.graph.intent_text import intent_with_negation, mask_literal_payload
    text = mask_literal_payload(query).strip()
    if _NEW_REFERENCE.search(text):
        return False
    if _SHORT_LIST.fullmatch(text):
        return True
    if (_PRIOR_REPORT_REFERENCE.search(text) or _REPORT_ID.search(text)) and intent_with_negation(text, _FINDINGS_REQUEST):
        return True
    return bool(previous_query and _CONTINUE.fullmatch(text)
                and fusion_report_followup(previous_query))


def _report_selectors(contract, query, previous_query):
    from src.graph.intent_text import mask_literal_payload

    source = previous_query if _CONTINUE.fullmatch(query.strip()) else query
    source = mask_literal_payload(source)
    ids = set(_REPORT_ID.findall(source))
    names = set(_SOURCE_LABEL.findall(source)) if contract.intent != "alternate_file" else set()
    return source, ids, names


def _verified_report_candidates(identity, release, allowed, *, ids, names):
    from .fusion_worker import consume_fusion, historical_report_candidates

    # Multiple selectors still need one real report lineage before routing
    # read-only; the resolver will ask the user to disambiguate.
    exact = next(iter(ids)) if len(ids) == 1 else None
    name = next(iter(names)) if len(names) == 1 else None
    matches = historical_report_candidates(identity, skill_release=release, skill_ids=allowed,
        exact_id=exact, requested_source=name)
    verified = []
    for execution_id, skill_id, source in matches[:64]:
        try:
            value = consume_fusion(identity, execution_id=execution_id, skill_release=release,
                                   skill_id=skill_id, source_identity=source)
        except (KeyError, LookupError, PermissionError, ValueError, OSError):
            continue
        if value.get("status") == "succeeded" and isinstance(value.get("report"), dict):
            verified.append((execution_id, skill_id, source, value))
            if len(verified) == 2:  # Only uniqueness matters.
                break
    return verified


def verified_report_route(identity, release, contract, query, previous_query):
    """Host-only preflight; intent alone cannot narrow the live tool set."""
    from src.core.capability_policy import allows
    from .code_tools import code_execution_context

    if not fusion_report_followup(query, previous_query=previous_query):
        return False
    source_query, ids, names = _report_selectors(contract, query, previous_query)
    try:
        allowed = {skill for skill in SKILL_IDS if allows("skill:" + skill)}
        if _DUPLICATE_REQUEST.search(source_query):
            allowed &= {"research-integrity"}
        with code_execution_context(identity, "fusion-report-route", skill_release=release):
            return bool(allowed and _verified_report_candidates(identity, release, allowed, ids=ids, names=names))
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return False


def installed_fusion(release, rows):
    return bool(release and SKILL_IDS <= {row["id"] for row in rows}
                and (release / "fusion" / "bundle.json").is_file())


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


FileID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
SkillID = Literal["literature-verification", "data-audit", "research-integrity"]


class InputRef(_Args):
    file_id: FileID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RunArgs(_Args):
    skill_id: SkillID
    inputs: dict[str, InputRef] = Field(default_factory=dict, max_length=24)
    options: dict = Field(default_factory=dict, max_length=20)


class LiteratureArgs(_Args):
    execution_id: FileID


class ReportPage(_Args):
    findings_offset: int = Field(default=0, ge=0, le=1_000_000)


class CollectArgs(LiteratureArgs, ReportPage):
    skill_id: SkillID
    publish_excel: bool = False  # Compatibility only. Authorization comes from the host scope.


def _bound(skill_id):
    from src.core.capability_policy import allows
    from .code_tools import _context, _enabled, _identity
    from .installed_skills import list_installed

    identity, call_id = _identity()
    release = _context.get()[2]
    if not (_enabled() and release and allows("skill:" + skill_id)
            and installed_fusion(Path(release), list_installed(Path(release)))):
        raise PermissionError("external_skill_unavailable")
    return identity, call_id, Path(release)


def _response(data, status="complete", reason="external_skill_ready", *, receipts=(), execution_id=None):
    from src.research.delivery_reconciliation import delivery_outcome

    artifact = {**data, "_tool_outcome": delivery_outcome(status, reason, list(receipts))}
    if execution_id:
        artifact["_code_execution_id"] = execution_id
    return json.dumps(data, ensure_ascii=False, allow_nan=False), artifact


def _queued(receipt):
    execution_id = receipt["execution_id"]
    return _response({"execution_id": execution_id, "status": receipt["status"], "instruction":
                      "Worker 已受理。等待框架恢复后，再读取执行结果。"}, execution_id=execution_id)


def _consumed(value):
    worker_status = value["status"]
    status = "unknown" if worker_status == "unknown" else "not_run" if worker_status in {
        "queued", "claimed", "preparing", "running", "collecting", "cancel_requested"} else "failed"
    return _response({"execution_id": value["execution_id"], "worker_status": worker_status},
                     status, "external_skill_result_not_available")


def _workbook_source(identity, reference):
    from src.session import files
    from hashlib import sha256

    metadata = files.get_task_input_file(identity.owner_user_id, identity.run_id,
                                         reference["file_id"], reference["sha256"])
    if not metadata or metadata.get("sha256") != reference["sha256"]:
        raise PermissionError("external_workbook_scope_changed")
    from .workspace_projection import bounded_bytes

    data = bounded_bytes(Path(metadata["path"]), 20 * 1024 * 1024)
    if len(data) != metadata["size_bytes"] or sha256(data).hexdigest() != reference["sha256"]:
        raise ValueError("external_workbook_source_changed")
    return data, metadata["filename"]


@tool(args_schema=RunArgs, response_format="content_and_artifact")
def run_external_skill(skill_id: SkillID, inputs=None, options=None):
    """Run the pinned external literature, XLSX data-audit, or research-integrity Skill. Use exact authorized file_id and sha256 per input role. Data audit needs workbook and scoped regions. Literature accepts bibliography and/or typed references. No raw observations or URLs are accepted."""
    from .fusion_worker import schedule_fusion
    from .fusion_worker_contract import confirmed_workbook_regions

    if skill_id == "data-audit":
        try:
            regions = (options or {}).get("regions")
            normalized = confirmed_workbook_regions(regions, _delivery.get()[1])
            options = {**options, "regions": [{**original, "range": region["bounds"]}
                                             for original, region in zip(regions, normalized)]}
        except (TypeError, ValueError):
            return _response({"error": "请确认真实工作表和有界区域，如 'Sheet 1'!A1:D20。默认区域首行为表头；若不同，请一并确认表头行。"},
                             "not_run", "external_workbook_regions_unconfirmed")
    try:
        identity, call_id, release = _bound(skill_id)
        refs = {role: ref.model_dump() if isinstance(ref, InputRef) else InputRef.model_validate(ref).model_dump()
                for role, ref in (inputs or {}).items()}
        if skill_id == "data-audit":
            from contextlib import closing
            from citrus_audit.readers import open_xlsx_readonly

            data, filename = _workbook_source(identity, refs["workbook"])
            with closing(open_xlsx_readonly(data, filename, data_only=True)) as workbook:
                if any(region["sheet"] not in workbook.sheetnames for region in options["regions"]):
                    return _response({"error": "确认的工作表不在源文件中。"}, "not_run", "external_workbook_sheet_missing")
        receipt = schedule_fusion(identity, operation_id=call_id, skill_release=release,
                                  skill_id=skill_id, inputs=refs, options=options or {})
        return _queued(receipt)
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "输入、权限或固定的 Skill 发行不可用。"}, "not_run", "external_skill_not_admitted")


@tool(args_schema=LiteratureArgs, response_format="content_and_artifact")
def continue_external_literature(execution_id: str):
    """Advance the host registry coverage of the original extraction execution. Collect the returned Worker execution_id. If coverage can advance, call again with the original extraction execution_id, not the latest child. Report any explicit stop reason and unavailable providers."""
    from src.research.fusion_observations import RegistryCoverage, advance_observations, observation_payload
    from .code_tools import _deadline
    from .fusion_worker import consume_fusion, find_fusion, schedule_fusion

    skill_id = "literature-verification"
    try:
        identity, _, release = _bound(skill_id)
        value = consume_fusion(identity, execution_id=execution_id, skill_release=release, skill_id=skill_id)
        if value["status"] != "succeeded":
            return _consumed(value)
        report = value["report"]
        if any(row.get("role") == "observations" for row in report.get("execution", {}).get("inputs", [])):
            return _deliver(identity, value, execution_id, skill_id)
        if not report.get("records"):
            # Observe a pre-cursor submission on a genuinely resumed old call;
            # a newly empty extraction does not authorize extra lookups.
            if existing := find_fusion(identity, operation_id="registry:" + execution_id,
                                       skill_release=release, skill_id=skill_id):
                return _queued(existing)
            return _deliver(identity, value, execution_id, skill_id)
        digest = value["receipt"]["result"]["sha256"]
        store = RegistryCoverage(identity)
        revision, child_id, state = store.get(execution_id, digest)
        if child_id:
            child = consume_fusion(identity, execution_id=child_id, skill_release=release, skill_id=skill_id)
            if child["status"] in {"queued", "claimed", "preparing", "running", "collecting", "cancel_requested"}:
                return _queued(child)
            if child["status"] != "succeeded":
                return _consumed(child)
            if state["stop_reason"] not in {"record_window", "lookup_window", "time_budget"}:
                return _deliver(identity, child, child_id, skill_id)
        if not state or child_id:
            deadline = time.monotonic() + max(0, _deadline(identity) - time.time())
            updated = advance_observations(report, state, identity=identity, deadline=deadline)
            if state and updated["offset"] == state["offset"] and observation_payload(updated) == observation_payload(state):
                return _response({"execution_id": child_id, "original_execution_id": execution_id,
                                  "considered": updated["offset"], "total": updated["total"],
                                  "stop_reason": updated["stop_reason"], "can_continue": False},
                                 "partial", "external_registry_no_progress")
            store.advance(execution_id, digest, revision, updated)
            revision, state = revision + 1, updated
        operation_id = f"registry:{execution_id}:{revision}"
        receipt = find_fusion(identity, operation_id=operation_id, skill_release=release, skill_id=skill_id)
        if receipt is None:
            receipt = schedule_fusion(identity, operation_id=operation_id, skill_release=release,
                skill_id=skill_id, inputs=value["inputs"], options=value["options"], observations=observation_payload(state))
        store.bind_child(execution_id, digest, revision, receipt["execution_id"], state)
        return _queued(receipt)
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "登记补查不可用。可读取已有结果并标注覆盖缺口。"}, "not_run", "external_registry_unavailable")


def _literature_coverage(identity, execution_id, report, *, historical=False, source_identity=None):
    from src.research.fusion_observations import RegistryCoverage
    from src.core.capability_policy import allows
    from src.core.tracing import web_search_enabled

    records = report.get("records") or []
    if not records:
        return None, {}
    child = RegistryCoverage(identity).child(execution_id, historical_identity=source_identity if historical else None)
    indexed = {row["record_index"]: row["status"] for row in (child or {}).get("coverage", [])}
    has_observations = any(row.get("role") == "observations" for row in report.get("execution", {}).get("inputs", []))
    for record in records:
        index = record["record_index"]
        if index not in indexed:
            # Old accepted reports preserve provider timestamps without a cursor.
            used = [row for row in (record.get("attempts") or []) if row.get("queried_at")]
            indexed[index] = (("queried" if all(row.get("status") in (200, 404) for row in used) else "provider_unavailable")
                              if used else "not_queried")
    considered = sum(value != "not_queried" for value in indexed.values())
    reason = child["stop_reason"] if child else ("legacy_observation_scope" if has_observations else "observations_not_started")
    return {"considered": considered, "total": len(records), "stop_reason": reason,
            "original_execution_id": child["original_execution_id"] if child else execution_id,
            "can_continue": (not historical and allows("bibliographic_lookup") and web_search_enabled()
                             and reason in {"record_window", "lookup_window", "time_budget", "observations_not_started"}),
            "counts": {state: sum(value == state for value in indexed.values()) for state in sorted(set(indexed.values()))}}, indexed


def _deliver(identity, value, execution_id, skill_id, *, findings_offset=0, historical=False):
    if value["status"] != "succeeded":
        return _consumed(value)
    report = value["report"]
    contract, query, previous_query = _delivery.get()
    curated = report
    duplicate_request = _DUPLICATE_REQUEST.search(previous_query if historical and _CONTINUE.fullmatch(query) else query)
    if historical and duplicate_request:
        curated = {**report, "findings": [row for row in report.get("findings", [])
                                         if row.get("rule_id") == "duplicate_numeric_block"]}
    public = {"execution_id": execution_id, **_summary(curated, findings_offset=findings_offset)}
    if skill_id == "research-integrity":
        public["result_lifecycle"] = "external_worker_report"
        public["lifecycle_note"] = ("这是同一账号与会话的执行报告，可查看或明确请求导出；"
                                     "并非旧版复核记录，不支持旧 review_id、确认或管理员复核流程。")
    if historical:
        public["historical_report"] = True
        public["findings_in_exact_report"] = len(report.get("findings", []))
        public["duplicate_numeric_block_findings"] = sum(row.get("rule_id") == "duplicate_numeric_block"
                                                         for row in report.get("findings", []))
        public["report_note"] = ("仅列本次已接受报告的证据；列级筛查不是单元格位置，"
                                 "无相符发现时不推断数量、不重跑或切换算法。")
        if duplicate_request and not public["duplicate_numeric_block_findings"]:
            public["report_note"] += " 原报告未记录任何重复数值区段，不能确认此前描述的数量或列出其位置。"
    status = {"complete": "complete", "partial": "partial", "unsupported": "not_run", "failed": "failed"}[report["status"]]
    registry = None
    indexed = {}
    if skill_id == "literature-verification":
        registry, indexed = _literature_coverage(identity, execution_id, report, historical=historical,
                                                 source_identity=(value.get("_source_snapshot") or {}).get("identity"))
        if registry:
            public["registry_coverage"] = registry
            from src.research.fusion_delivery import annotate_record_coverage
            for record in public.get("records", []):
                annotate_record_coverage(record, indexed)
            if any(value not in {"queried", "previously_observed"} for value in indexed.values()) and status == "complete":
                status = public["status"] = "partial"
                curated = {**curated, "status": "partial"}
    if findings_offset or status not in {"complete", "partial"}:
        return _response(public, status, "external_skill_report")
    try:
        selected_format = contract.format if contract.intent == "alternate_file" else (
            "xlsx" if contract.intent == "annotated_xlsx" and skill_id == "literature-verification" else "")
        if selected_format:
            from src.research.fusion_delivery import publish_report
            artifact = publish_report(curated, execution_id=execution_id, skill_id=skill_id,
                                      fmt=selected_format, receipt=value["receipt"], host_coverage=indexed)
            public["saved_report"] = {k: artifact[k] for k in ("filename", "url", "rows")}
            return _response(public, status, "external_skill_report", receipts=[artifact])
        if contract.intent != "annotated_xlsx" or skill_id == "literature-verification":
            return _response(public, status, "external_skill_report")
        from src.research.fusion_workbook import publish_fusion_workbook
        from src.core.account_leases import account_lease
        from .fusion_worker import historical_fusion_source

        ref = value["inputs"]["workbook" if skill_id == "data-audit" else "table"]
        with account_lease(identity.owner_user_id):
            raw, name = (historical_fusion_source(identity, value, ref) if historical else _workbook_source(identity, ref))
            artifact = publish_fusion_workbook(curated, source_file_id=ref["file_id"],
                source_sha256=ref["sha256"], source_name=name, source_bytes=raw)
        public["workbook"] = {k: artifact[k] for k in ("filename", "url", "cells", "source_format")}
        return _response(public, status, "external_skill_report", receipts=[artifact])
    except (KeyError, LookupError, ValueError, PermissionError, OSError):
        public["report_file_error"] = "请求的正式文件未生成；已接受审查结论仍可用。"
        return _response(public, "partial", "external_report_not_published")


@tool(args_schema=CollectArgs, response_format="content_and_artifact")
def collect_external_skill(execution_id: str, skill_id: SkillID, publish_excel: bool = False, findings_offset: int = 0):
    """Read the current turn's settled external Skill result by exact execution_id. The host decides whether to publish a requested file. When findings_page.next_offset is present, pass that offset to read the next page of the same exact report."""
    from .fusion_worker import consume_fusion

    try:
        identity, _, release = _bound(skill_id)
        value = consume_fusion(identity, execution_id=execution_id, skill_release=release, skill_id=skill_id)
        return _deliver(identity, value, execution_id, skill_id, findings_offset=findings_offset)
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "执行结果不可用。"}, "not_run", "external_skill_report_unavailable")


@tool(args_schema=ReportPage, response_format="content_and_artifact")
def resolve_external_report(findings_offset: int = 0):
    """Read only the exact accepted external report authorized by a findings-list or report-delivery follow-up. No algorithm is rerun. Continue with findings_page.next_offset; never invent missing findings or positions."""
    from src.core.capability_policy import allows
    from src.graph.intent_text import mask_literal_payload
    contract, query, previous_query = _delivery.get()
    query = mask_literal_payload(query).strip()
    if not fusion_report_followup(query, previous_query=previous_query):
        return _response({"error": "本轮没有明确要求读取先前审查发现。"}, "not_run", "external_report_not_requested")
    source_query, ids, names = _report_selectors(contract, query, previous_query)
    if len(ids) > 1 or len(names) > 1:
        return _response({"error": "请指定要读取的一份审查结果。"}, "not_run", "external_report_ambiguous")
    try:
        allowed = {skill for skill in SKILL_IDS if allows("skill:" + skill)}
        if _DUPLICATE_REQUEST.search(source_query):
            allowed &= {"research-integrity"}
        if not allowed:
            raise PermissionError("external_report_scope_unavailable")
        identity, _, release = _bound(sorted(allowed)[0])
        matches = _verified_report_candidates(identity, release, allowed, ids=ids, names=names)
        if len(matches) > 1:
            return _response({"error": "同一会话有多份已接受审查结果。请指明源文件或精确执行 ID。"},
                             "not_run", "external_report_ambiguous")
        if not matches:
            raise LookupError("external_report_unavailable")
        execution_id, skill_id, _, value = matches[0]
        return _deliver(identity, value, execution_id, skill_id, findings_offset=findings_offset, historical=True)
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "没有可验证的原审查私有报告。没有推断位置、数量或另行执行。"},
                         "not_run", "external_skill_report_unavailable")


_TOOLS = (run_external_skill, continue_external_literature, collect_external_skill, resolve_external_report)
FUSION_TOOL_NAMES = frozenset(item.name for item in _TOOLS)


def _load(item):
    from src.tools.registry import register_tool

    register_tool(item, category="research", is_readonly=False, concurrency_key="external_skill",
                  write_scope="session_artifact", recovery_mode="never")
    return item


def register_fusion_tools(catalog):
    from .capabilities import CapabilityDefinition

    for item in _TOOLS:
        catalog.register(CapabilityDefinition(item.name, item.description, item.args_schema.model_json_schema(),
            partial(_load, item), provider="external_skill", mutates=True, execution_mode="sequential"))
