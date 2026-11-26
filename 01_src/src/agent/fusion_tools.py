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


_delivery = ContextVar("fusion_delivery", default=(FusionDelivery(), "", "", ""))


@contextmanager
def fusion_delivery_scope(contract, *, query="", previous_query="", workbook_confirmation=""):
    """Host scope only. A tool call cannot grant itself a delivery format."""
    token = _delivery.set((contract, query, previous_query, workbook_confirmation))
    try:
        yield
    finally:
        _delivery.reset(token)


def external_fusion_required():
    return os.environ.get("CITRUS_EXTERNAL_FUSION") == "required"


CAPABILITY_NEW = "new_capability"
CAPABILITY_LEGACY = "legacy_tools_retained"
CAPABILITY_UNAVAILABLE = "capability_unavailable"


def fusion_capability_state(*, release, installed_rows, worker_ready, allowed_skills,
                            external_release=None, external_required=None, report_followup=False):
    """One atomic decision for the Fusion tool surface.

    The new tools are enabled only when the pinned release, the Code Worker and
    at least one authorized Skill capability are all ready. Otherwise the caller
    keeps the still-safe old tools (``legacy_tools_retained``) or states
    ``capability_unavailable`` instead of leaving a silent empty window between
    withdrawn old tools and unopened new ones.

    Returns ``(state, tool_names, reason)``. Pure: it reads no environment and
    mutates no host state beyond the explicitly supplied readiness facts.
    """
    if external_release is None:
        external_release = installed_fusion(release, installed_rows) if release is not None else False
    if external_required is None:
        external_required = external_fusion_required()
    skills = frozenset(allowed_skills or ()) & SKILL_IDS
    fallback = CAPABILITY_UNAVAILABLE if external_required else CAPABILITY_LEGACY
    if not external_release:
        return fallback, frozenset(), ("fusion_required_without_release" if external_required
                                       else "fusion_release_not_installed")
    if not worker_ready:
        return fallback, frozenset(), "code_worker_unavailable"
    if not skills:
        return fallback, frozenset(), "skill_capability_unauthorized"
    return CAPABILITY_NEW, fusion_tool_names_for(CAPABILITY_NEW, report_followup=report_followup), "external_skill_ready"


def fusion_tool_names_for(state, *, report_followup=False):
    """Tool names for an already decided capability state (no host mutation)."""
    if state != CAPABILITY_NEW:
        return frozenset()
    names = FUSION_TOOL_NAMES - {"resolve_external_report"}
    return frozenset(names | ({"resolve_external_report"} if report_followup else set()))


def fusion_retires_legacy_tools(state):
    """Old analyzers may only be withdrawn after the replacement surface opened."""
    return state == CAPABILITY_NEW


def fusion_capability_notice(state, reason):
    """Prompt text for a non-ready state; never claims a Skill can be invoked."""
    if state == CAPABILITY_NEW:
        return ""
    if state == CAPABILITY_UNAVAILABLE:
        return ("\n外部审查运行能力暂不可用（%s），已安装的审查 Skill 本轮不可调用；"
                "不得替换为旧分析器或引用未经验证的检查结果。" % reason)
    return "\n本轮未启用外部审查 Skill（%s）；可继续使用仍然安全可用的既有工具。" % reason


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


_NEW_REFERENCE = re.compile(r"\b(?:10\.\d{4,9}/|arxiv\s*[:/]|arxiv\.org/|pmid\s*[:/]|pubmed\.ncbi\.nlm\.nih\.gov/)|(?:新(?:的|增)?|另[一个查]|再(?:查|核验|审查))[^，。！？!?]{0,20}(?:文献|引用|参考|数据|文件|DOI)", re.I)
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


def _queued(receipt, contract=None):
    execution_id = receipt["execution_id"]
    payload = {"execution_id": execution_id, "status": receipt["status"], "instruction":
               "Worker 已受理。等待框架恢复后，再读取执行结果。"}
    if contract is not None:
        payload["input_contract"] = contract
    return _response(payload, execution_id=execution_id)


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


_EVIDENCE_KEYS = frozenset({"dataset_ref", "evidence_ref", "evidence", "freeze_receipt", "audit_receipt"})
_EVIDENCE_SUFFIXES = ("_ref", "_receipt")


def _evidence_fields(artifact):
    """Pass upstream evidence refs and receipts through unchanged.

    F-06 freezes them inside the generic adapter; this module never derives a
    second identity and never re-publishes a dataset the adapter already froze.
    """
    return {key: value for key, value in artifact.items()
            if (key in _EVIDENCE_KEYS or key.endswith(_EVIDENCE_SUFFIXES))
            and key not in {"publication_receipts", "plot_datasets", "integrity_review"}}


def _plot_fields(options):
    """Optional model-chosen columns the frozen dataset should project."""
    fields = (options or {}).get("plot_fields")
    if fields is None:
        return []
    if (type(fields) is not list or not 1 <= len(fields) <= 64
            or any(type(name) is not str or not name.strip() or len(name) > 120 for name in fields)):
        raise ValueError("csv_table_plot_fields_invalid")
    return list(dict.fromkeys(name.strip() for name in fields))


def _table_findings(report, digest, filename):
    """Project saved standalone-audit findings into the shared Finding Schema.

    The projection is the existing ``audit_projection`` view of the immutable
    report; coordinates stay logical CSV records and fields, with no worksheet
    name because a CSV table has none.
    """
    from src.research.audit_projection import project_findings

    projection = project_findings(report)
    findings = []
    for item in projection["findings"]:
        locations = []
        for location in item.get("locations") or ():
            rows = [row for row in location.get("rows") or () if type(row) is int]
            columns = [entry.get("index") for entry in location.get("columns") or ()
                       if type(entry.get("index")) is int]
            mapped = {"source_file_hash": digest, "filename": filename}
            if rows:
                mapped["row_index"], mapped["end_row_index"] = min(rows), max(rows)
            if columns:
                mapped["column_index"], mapped["end_column_index"] = min(columns), max(columns)
            mapped["scope"] = ("csv_range" if rows and columns else
                               "column" if columns else "row" if rows else "table")
            locations.append(mapped)
        evidence = dict(item.get("evidence") or {})
        if item.get("detail"):
            evidence["message"] = item["detail"]
        if item.get("rule_version"):
            evidence.setdefault("method_version", item["rule_version"])
        finding = {"rule_id": item["rule"], "severity": item.get("severity") or "review",
                   "evidence": evidence, "locations": locations}
        if item.get("finding_id"):
            finding["finding_id"] = item["finding_id"]
        findings.append(finding)
    return findings, projection


def _csv_table_audit(*, data, filename, file_id, table, query, allow_publication, plot_fields=()):
    """Review one authorized CSV through the existing generic data-audit route.

    ``audit_review_adapter.run_review`` is the same adapter ``audit_uploaded_data``
    uses: it runs ``citrus_audit`` on the immutable table bytes, persists one
    integrity review, and reuses that review instead of rescanning. Publication
    stays under host authority: the annotated workbook is only requested when
    the turn's delivery contract asked for it. Frozen dataset refs are whatever
    the adapter already produced; they are never re-derived here.
    """
    from src.core.tracing import get_user_id
    from src.research.audit_review_adapter import run_review
    from src.research.integrity_review import get_review

    checks = {"plot_fields": list(plot_fields)} if plot_fields else {}
    validated = {"checks": checks, "formats": ["xlsx"], "header_rows": [1], "plan": None,
                 "file_ids": [], "file_indexes": [0], "custom_findings": [], "regions": []}
    audit_query = query if allow_publication else (
        (query or "审查这张表") + "\n（本轮只要文字结论，不要生成文件）")
    _, artifact = run_review([{"name": filename, "data": data, "file_id": file_id}], [0],
                             validated, sheet_name="", query=audit_query)
    outcome = artifact.get("_tool_outcome") or {}
    review_id = (artifact.get("integrity_review") or {}).get("review_id") or ""
    saved = get_review(int(get_user_id() or 0), review_id) if review_id else None
    report = (saved or {}).get("report") or {}
    if report.get("schema") != "standalone-audit-review/1":
        raise ValueError("csv_table_review_not_saved")
    findings, projection = _table_findings(report, table["source_sha256"], table["filename"])
    evidence = _evidence_fields(artifact)
    receipts = [dict(item) for item in artifact.get("publication_receipts") or () if isinstance(item, dict)]
    result_refs = [{"kind": "integrity_review", "review_id": review_id}]
    for ref in artifact.get("result_refs") or ():
        if isinstance(ref, dict) and ref not in result_refs:
            result_refs.append(ref)
    status = str(outcome.get("status") or "complete")
    if status not in {"complete", "partial", "failed", "unknown"}:
        status = "partial"
    reason = str(outcome.get("reason_code") or ("external_csv_table_review_complete"
                                                if status == "complete" else "external_csv_table_review_partial"))
    view = {"status": status, "limitations": list(artifact.get("coverage_gaps") or [])[:10],
            "execution": {"skill": "data-audit", "version": "generic/citrus_audit", "python": ""},
            "findings": findings,
            "coverage": [{"check": "csv_table_review", "status": "checked",
                          "scope": "CSV 单表：%d 条记录 × %d 列（逻辑记录与字段，无工作表）"
                                   % (table["record_count"], table["column_count"]),
                          "n_rows": table["record_count"], "n": table["column_count"]}],
            "evidence": []}
    public = {"skill_id": "data-audit", "input_contract": table, "review_id": review_id,
              "input": {"file_id": file_id, "sha256": table["source_sha256"], "filename": table["filename"]},
              "route": "generic_data_audit",
              "projection_status": projection.get("status"), "result_refs": result_refs,
              "publication_receipts": receipts, **evidence, **_summary(view)}
    if isinstance(artifact.get("plot_datasets"), list):
        # Frozen by the adapter; passed through unchanged (no second dataset).
        public["plot_datasets"] = artifact["plot_datasets"]
    return _response(public, status, reason, receipts=receipts)


@tool(args_schema=RunArgs, response_format="content_and_artifact")
def run_external_skill(skill_id: SkillID, inputs=None, options=None):
    """Run the pinned external literature, XLSX/XLSM/CSV data-audit, or research-integrity Skill. Use exact authorized file_id and sha256 per input role. Data audit accepts one workbook (workbook + confirmed sheet/A1 regions) or one CSV table (csv_table, no sheet). Literature accepts bibliography and/or typed references. No raw observations or URLs are accepted."""
    from .fusion_worker import schedule_fusion
    from .fusion_worker_contract import (CSV_TABLE_ROLE, WORKBOOK_EXTENSIONS, WORKBOOK_ROLE, csv_data_audit_route,
        confirmed_workbook_regions, csv_table_contract, data_audit_input_kind,
        excel_workbook_contract)

    try:
        identity, call_id, release = _bound(skill_id)
        refs = {role: ref.model_dump() if isinstance(ref, InputRef) else InputRef.model_validate(ref).model_dump()
                for role, ref in (inputs or {}).items()}
        if skill_id == "data-audit":
            from contextlib import closing
            from citrus_audit.readers import open_xlsx_readonly
            from citrus_audit._zip_safety import validate_macro_workbook

            roles = set(refs)
            if roles not in ({WORKBOOK_ROLE}, {CSV_TABLE_ROLE}):
                return _response({"error": "数据审查需要且只接受一个源文件：工作簿（workbook）或单表 CSV（csv_table）。"},
                                 "not_run", "external_data_audit_input_ambiguous")
            role = next(iter(roles))
            # Optional model-chosen columns for the frozen dataset projection;
            # the pinned analyzer requests do not carry this host-side hint.
            try:
                plot_fields = _plot_fields(options)
            except ValueError:
                return _response({"error": "plot_fields 必须是 1–64 个非空列名。"}, "not_run",
                                 "external_data_audit_plot_fields_invalid")
            options = {key: value for key, value in (options or {}).items() if key != "plot_fields"}
            data, filename = _workbook_source(identity, refs[role])
            kind = data_audit_input_kind(filename)
            if kind is None:
                return _response({"error": "此 Skill 只支持 .xlsx、.xlsm 工作簿或单表 CSV。"}, "not_run",
                                 "external_workbook_format_unsupported")
            if role != {"excel_workbook": WORKBOOK_ROLE, "csv_table": CSV_TABLE_ROLE}[kind]:
                return _response({"error": "输入角色与文件类型不一致：工作簿用 workbook，CSV 用 csv_table。"},
                                 "not_run", "external_data_audit_input_role_mismatch")
            if kind == "csv_table":
                # A CSV is a single table with logical records, not a workbook:
                # no sheet name is invented and no regions are confirmed.
                table = csv_table_contract(data, filename)
                if csv_data_audit_route("data-audit", release) == "generic":
                    # The pinned analyzer cannot read a table. Reuse the real
                    # generic data-audit route on the same authorized bytes and
                    # project its saved report into the shared Finding Schema.
                    contract_, query, _, _ = _delivery.get()
                    try:
                        return _csv_table_audit(data=data, filename=filename,
                                                file_id=refs[role]["file_id"], table=table, query=query,
                                                allow_publication=contract_.intent == "annotated_xlsx",
                                                plot_fields=plot_fields)
                    except (KeyError, LookupError, PermissionError, ValueError, OSError) as exc:
                        return _response({"error": "CSV 表审查未能通过通用数据审查路径完成：%s" % exc,
                                          "csv_table": table, "route": "generic_data_audit"},
                                         "not_run", "external_csv_table_audit_unavailable")
                if (options or {}).get("regions"):
                    return _response({"error": "CSV 是单表输入，不接受工作表区域；请按列名和记录范围审查。"},
                                     "not_run", "external_csv_table_regions_not_allowed")
                options = {**(options or {}), "csv_table": {"header_rows": table["header_rows"],
                                                            "column_names": table["column_names"],
                                                            "source_sha256": table["source_sha256"]}}
                receipt = schedule_fusion(identity, operation_id=call_id, skill_release=release,
                                          skill_id=skill_id, inputs=refs, options=options)
                return _queued(receipt, contract=table)
            if Path(filename).suffix.lower() not in WORKBOOK_EXTENSIONS:
                return _response({"error": "此 Skill 只支持 .xlsx 或 .xlsm 工作簿。"}, "not_run", "external_workbook_format_unsupported")
            if Path(filename).suffix.lower() == ".xlsm":
                try:
                    validate_macro_workbook(data)
                except ValueError:
                    return _response({"error": "无法安全解析此 XLSM。仅支持标准 OOXML 工作簿和普通 VBA 项目，不支持签名、宏工作表、ActiveX、嵌入对象或加密。"},
                                     "not_run", "external_workbook_format_unsupported")
            regions = (options or {}).get("regions")
            _, query, _, confirmation = _delivery.get()
            try:
                normalized = confirmed_workbook_regions(regions, confirmation or query)
            except (TypeError, ValueError):
                return _response({"error": "请确认真实工作表和有界区域，如 'Sheet 1'!A1:D20。默认区域首行为表头；若不同，请一并确认表头行。"},
                                 "not_run", "external_workbook_regions_unconfirmed")
            options = {**options, "regions": [{**original, "range": region["bounds"]}
                                             for original, region in zip(regions, normalized)]}
            with closing(open_xlsx_readonly(data, filename, data_only=True)) as workbook:
                if any(region["sheet"] not in workbook.sheetnames for region in options["regions"]):
                    return _response({"error": "确认的工作表不在源文件中。"}, "not_run", "external_workbook_sheet_missing")
                contract = excel_workbook_contract(workbook.sheetnames, normalized)
            receipt = schedule_fusion(identity, operation_id=call_id, skill_release=release,
                                      skill_id=skill_id, inputs=refs, options=options or {})
            return _queued(receipt, contract=contract)
        receipt = schedule_fusion(identity, operation_id=call_id, skill_release=release,
                                  skill_id=skill_id, inputs=refs, options=options or {})
        return _queued(receipt)
    except (KeyError, LookupError, PermissionError, ValueError, OSError):
        return _response({"error": "输入、权限或固定的 Skill 发行不可用。"}, "not_run", "external_skill_not_admitted")


@tool(args_schema=LiteratureArgs, response_format="content_and_artifact")
def continue_external_literature(execution_id: str):
    """Run one bounded registry batch on an exact literature extraction and return its Worker execution ID. Collect that report. Repeat only when the report explicitly permits continuation on the original extraction ID."""
    from citrus_refcheck.lookup import AuditStopped
    from src.core.account_admission import AccountDeletionPending, AccountUnavailable
    from src.core.account_leases import acquire_account_lease
    from src.research.fusion_observations import RegistryCoverage, advance_observations, batch_observations, observation_payload
    from .code_tools import _deadline
    from .fusion_worker import consume_fusion, find_fusion, schedule_fusion

    skill_id = "literature-verification"
    lease = None
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
        version = report.get("execution", {}).get("version")
        epoch = value.get("_account_epoch")
        if version == "0.1.2":
            if type(epoch) is not int:
                raise PermissionError("bibliographic_account_epoch_missing")
            lease = acquire_account_lease(identity.owner_user_id, expected_epoch=epoch)
        store = RegistryCoverage(identity)
        revision, child_id, state = store.get(execution_id, digest)
        if child_id:
            child = consume_fusion(identity, execution_id=child_id, skill_release=release, skill_id=skill_id)
            if child["status"] in {"queued", "claimed", "preparing", "running", "collecting", "cancel_requested"}:
                return _queued(child)
            if child["status"] != "succeeded":
                return _consumed(child)
            if "reference_batch" in state or state["stop_reason"] not in {"record_window", "lookup_window", "time_budget"}:
                return _deliver(identity, child, child_id, skill_id)
        if not state or child_id:
            deadline = time.monotonic() + max(0, _deadline(identity) - time.time())
            if version == "0.1.2":
                updated = batch_observations(report, identity=identity, account_epoch=epoch,
                                             deadline=deadline, original_report_sha256=digest)
            else:
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
    except (KeyError, LookupError, PermissionError, ValueError, OSError, AuditStopped, AccountDeletionPending, AccountUnavailable):
        return _response({"error": "登记补查不可用。可读取已有结果并标注覆盖缺口。"}, "not_run", "external_registry_unavailable")
    finally:
        if lease is not None:
            lease.release()


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
                             and (reason == "observations_not_started" or (
                                 not report.get("reference_batch") and reason in {"record_window", "lookup_window", "time_budget"}))),
            "counts": {state: sum(value == state for value in indexed.values()) for state in sorted(set(indexed.values()))}}, indexed


def _deliver(identity, value, execution_id, skill_id, *, findings_offset=0, historical=False):
    if value["status"] != "succeeded":
        return _consumed(value)
    report = value["report"]
    contract, query, previous_query, _ = _delivery.get()
    curated = report
    if skill_id == "data-audit":
        bound = value.get("inputs") or {}
        source = bound.get("workbook") or bound.get("csv_table") or {}
        digest = source.get("sha256")
        file_id = source.get("file_id")
        if digest and file_id:
            curated = {**report, "findings": [{**finding, "locations": [
                {**location, "source_file_id": file_id} if location.get("source_file_hash") == digest else location
                for location in finding.get("locations", [])]} for finding in report.get("findings", [])]}
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

        report_inputs = value["inputs"]
        ref = (report_inputs.get("workbook") or report_inputs.get("csv_table")
               if skill_id == "data-audit" else report_inputs.get("table"))
        if ref is None:
            raise ValueError("fusion_source_input_missing")
        with account_lease(identity.owner_user_id):
            raw, name = (historical_fusion_source(identity, value, ref) if historical else _workbook_source(identity, ref))
            artifact = publish_fusion_workbook(curated, source_file_id=ref["file_id"],
                source_sha256=ref["sha256"], source_name=name, source_bytes=raw)
        public["workbook"] = {k: artifact[k] for k in ("filename", "url", "cells", "source_format", "notice") if k in artifact}
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
    contract, query, previous_query, _ = _delivery.get()
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
