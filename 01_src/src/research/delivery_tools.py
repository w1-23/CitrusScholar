"""One model-facing delivery boundary; renderers and publication stay in tools.py.

Legacy names remain callable internally. This module does not register tools or
rewrite historical messages; callers may adapt newly executed calls explicitly.
"""
from __future__ import annotations

from contextlib import closing
import inspect
import json
import re
import sqlite3
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.tool_errors import ToolArgumentError

Kind = Literal["report", "table", "bibliography", "research_notes", "corpus_gap", "data_audit"]
Purpose = Literal["general", "paper_analysis", "experiment_design"]
Cell = str | int | float | bool | None


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Reference(StrictInput):
    citation: Cell = ""
    title: Cell = ""
    authors: str | list[str] | None = ""
    journal: Cell = ""
    year: Cell = ""
    volume: Cell = ""
    issue: Cell = ""
    pages: Cell = ""
    claim: Cell = ""
    evidence: Cell = ""
    source: Cell = ""
    doi: Cell = ""
    url: Cell = ""


class CorpusGap(StrictInput):
    topic: Cell = ""
    doi_or_title: Cell = ""
    failure: Cell = ""
    local_result: Cell = ""
    source_url: Cell = ""


class AnalysisReference(StrictInput):
    kind: Literal["analysis_result"] = "analysis_result"
    id: str
    version: int = 1
    sha256: str


class DatasetReference(StrictInput):
    kind: Literal["dataset_snapshot"] = "dataset_snapshot"
    id: str
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


# This dispatch contract enumerates the formats the existing renderers actually
# implement (not file-conversion aspirations). SVG is a report companion, never
# a replacement for HTML. Real-renderer tests exercise every listed format.
_FORMATS = {
    "report": ("html", "svg"),
    "table": ("csv", "tsv", "xlsx"),
    "bibliography": ("xlsx", "ris", "bibtex", "json"),
    "research_notes": ("html", "svg"),
    "corpus_gap": ("csv",),
    "data_audit": ("xlsx", "html", "json"),
}
_DEFAULT_FORMAT = {kind: formats[0] for kind, formats in _FORMATS.items()}


def _audit_format_requested(query: str, fmt: str) -> bool:
    """Only non-default data-audit formats require an explicit user request."""
    text = str(query or "")
    if fmt == "json":
        return bool(re.search(r"(?i)\bjson\b|\.json\b", text))
    if fmt == "html":
        return bool(re.search(r"(?i)\bhtml\b|网页版?|报告页", text))
    return True


LEGACY_DELIVERY_NAMES = (
    "export_paper_analysis", "export_experiment_design", "export_research_notes",
    "export_table", "export_literature_evidence", "export_corpus_gap_report",
)


def supported_delivery_formats(kind: str) -> tuple[str, ...]:
    """Return actual renderer formats; svg always accompanies an HTML report."""
    if kind not in _FORMATS:
        raise ToolArgumentError(f"不支持的成果类型：{kind}")
    return _FORMATS[kind]


class DeliveryInput(StrictInput):
    kind: Kind
    title: str = ""
    output_format: str = Field(default="", description="留空用类型默认格式；report/research_notes 的 svg 为 HTML 附件。")
    purpose: Purpose = "general"
    content: str = Field(default="", description="report 正文 Markdown；不补造研究内容。")
    svg_code: str = ""
    append: bool = False
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Cell] | dict[str, Cell]] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    source: Literal["", "last_verification"] = ""
    audit_id: str = Field(default="", description="明确的 ReferenceAudit ID；按此 ID 导出全部条目、覆盖与证据，不取最近一次结果。")
    review_id: str = Field(default="", description="audit_uploaded_data 返回的真实 integrity_review.review_id；data_audit 按此记录导出，不重跑扫描。")
    analysis_result_ref: str | AnalysisReference = ""
    dataset_ref: DatasetReference | None = None
    result_section: Literal["outputs", "descriptive", "grouped", "test", "tests", "coverage", "exclusions", "design"] = "outputs"
    project_name: str = ""
    limit: int = 100
    gaps: list[CorpusGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_kind_fields(self):
        fmt = self.output_format.strip().lower() or _DEFAULT_FORMAT[self.kind]
        if fmt not in supported_delivery_formats(self.kind):
            raise ValueError(f"{self.kind} 不支持 {fmt}；支持 {supported_delivery_formats(self.kind)}")
        self.output_format = fmt
        allowed = {
            "report": {"purpose", "content", "svg_code", "append"},
            "table": {"columns", "rows", "analysis_result_ref", "dataset_ref", "result_section", "append"},
            "bibliography": {"references", "source", "audit_id", "append"},
            "research_notes": {"project_name", "limit", "svg_code"},
            "corpus_gap": {"gaps", "append"},
            "data_audit": {"review_id"},
        }[self.kind] | {"kind", "title", "output_format"}
        for name, field in type(self).model_fields.items():
            if name not in allowed and getattr(self, name) != field.get_default(call_default_factory=True):
                raise ValueError(f"{self.kind} 不接受参数 {name}")
        if self.kind == "data_audit" and not re.fullmatch(r"[a-f0-9]{32}", self.review_id):
            raise ValueError("data_audit 需要真实 integrity_review.review_id，不接受工具调用 ID 或手抄扫描数据")
        if self.kind == "research_notes" and self.title:
            raise ValueError("课题记录以 project_name 命名，不接受 title")
        if self.kind == "table" and self.analysis_result_ref and (self.columns or self.rows or self.append):
            raise ValueError("分析结果引用不能同时覆盖行列或追加")
        if self.kind == "table" and self.dataset_ref and (
                self.analysis_result_ref or self.columns or self.rows or self.append
                or self.result_section != "outputs"):
            raise ValueError("数据集引用不能同时覆盖行列、混用分析结果、追加或选择分析字段")
        if self.kind == "table" and not (self.analysis_result_ref or self.dataset_ref or self.rows):
            raise ValueError("需要表格数据行、数据集引用或分析结果引用，不发布空表")
        if fmt == "svg" and not self.svg_code and "<svg" not in self.content.lower():
            raise ValueError("SVG 导出需要 svg_code 或正文中的完整 SVG")
        if self.kind == "bibliography":
            if self.source:
                raise ValueError("最近一次核验不能代表完整审查；请提供明确 audit_id 或 references")
            if self.audit_id and (self.references or self.append):
                raise ValueError("audit_id 全量导出不能混用 references 或 append")
            if self.audit_id and not re.fullmatch(r"[a-f0-9]{32}", self.audit_id):
                raise ValueError("需要有效的 ReferenceAudit audit_id")
            if fmt == "json" and not self.audit_id:
                raise ValueError("JSON 证据导出需要明确 audit_id")
        if self.kind == "corpus_gap" and (not self.gaps or len(self.gaps) > 5000):
            raise ValueError("需要 1–5000 条语料缺口，不发布空报告或截断批次")
        return self


def legacy_delivery_call(name: str, args: dict) -> tuple[str, dict]:
    """Adapt a pending legacy call without mutating it; unrelated calls pass through.

    Signature binding catches unknown arguments instead of silently losing them.
    JSON strings are decoded only here; the canonical schema uses typed arrays.
    """
    if name not in LEGACY_DELIVERY_NAMES:
        return name, dict(args)
    from src.research import tools
    bound = inspect.signature(getattr(tools, name).func).bind(**args)
    bound.apply_defaults()
    old = dict(bound.arguments)
    mapped: dict = {}
    if name in {"export_paper_analysis", "export_experiment_design"}:
        purpose = "paper_analysis" if name == "export_paper_analysis" else "experiment_design"
        content_key = "analysis_markdown" if purpose == "paper_analysis" else "design_markdown"
        mapped.update(kind="report", purpose=purpose, content=old.pop(content_key))
    elif name == "export_table":
        mapped.update(kind="table", columns=old.pop("columns_json"), rows=old.pop("rows_json"))
    elif name == "export_literature_evidence":
        mapped.update(kind="bibliography", references=old.pop("evidence_rows_json"))
        if old["source"].strip().lower() in {"verification", "verify", "last_verification"}:
            old["source"] = "last_verification"
    elif name == "export_research_notes":
        mapped["kind"] = "research_notes"
    else:
        mapped.update(kind="corpus_gap", gaps=old.pop("gaps_json"))
    mapped.update(old)
    for key in ("columns", "rows", "references", "gaps"):
        if key in mapped and isinstance(mapped[key], str):
            mapped[key] = json.loads(mapped[key]) if mapped[key].strip() else []
    # Old result loaders accept a JSON-encoded reference as well as its id.
    return "deliver_research_artifact", DeliveryInput.model_validate(mapped).model_dump()


def _data_audit_payload(review: dict) -> dict:
    """Keep legacy table issues and expose the shared review projection."""
    from citrus_audit.workbook import _kind
    from src.core.data_audit_xlsx import _FINDING_SEVERITIES, _ANNOTATION_SEVERITIES
    from .audit_projection import location_cells, project_findings, table_location, table_targets

    report = review['report']
    issues = []
    for target in table_targets(report):
        for signal in target['signals']:
            location = table_location(target, signal)
            cells = list(location_cells(location).values())
            label, fallback, _ = _kind(signal['check'])
            severity = signal.get('severity') or signal.get('level') or ''
            issues.append({**signal, 'file': target['source_name'], 'file_index': target['file_index'],
                'source_file_id': target['source_file_id'], 'source_sha256': target['source_sha256'],
                'sheet': target['sheet'], 'region_id': target['region_id'], 'region_bounds': target['region_bounds'],
                'cell': cells[0] if len(cells) == 1 else '', 'cells': cells,
                'category': signal['check'], 'category_label': label, 'severity': severity,
                'severity_label': _FINDING_SEVERITIES.get(severity, _ANNOTATION_SEVERITIES.get(severity, fallback)),
                'location_scope': 'cells' if cells else 'column' if signal.get('column_index') else 'table'})
    return {'schema_version': 'data-audit-export/1', 'review_id': review['review_id'],
            'session_id': review['session_id'], 'created_at': review['created_at'], **report,
            'issues': issues, 'finding_projection': report.get('finding_projection') or project_findings(report),
            'interpretation': '错误表示违反显式数据规则。探索性发现属于人工复核线索。原始文件未修改。'}


def _require_normal_audit_xlsx(content: bytes) -> None:
    """Check the actual OOXML package before using the normal XLSX contract."""
    from io import BytesIO
    from xml.etree import ElementTree
    from zipfile import ZipFile

    types_ns = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    normal = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    with ZipFile(BytesIO(content)) as archive:
        manifest = archive.getinfo("[Content_Types].xml")
        if manifest.file_size > 2 * 1024 * 1024:
            raise ValueError("XLSX 包的内容类型清单过大")
        root = ElementTree.fromstring(archive.read(manifest))
        workbook_types = [entry.get("ContentType") for entry in root.findall(types_ns + "Override")
                          if entry.get("PartName") == "/xl/workbook.xml"]
        macro_parts = ("vbaproject", "macroenabled", "macrosheet")
        names = archive.namelist()
        if (root.tag != types_ns + "Types" or workbook_types != [normal]
                or "xl/workbook.xml" not in names or len(set(names)) != len(names)
                or any(marker in entry.get("ContentType", "").lower()
                       for entry in root for marker in macro_parts)
                or any(marker in name.lower() for name in names for marker in macro_parts)):
            raise ValueError("审查工作簿不是普通 XLSX，未发布文件")


def publish_audit_workbooks(report, *, load_source, title='', review_id=''):
    """Publish one annotated copy per reviewed source using the recorded findings."""
    from collections import defaultdict
    from hashlib import sha256
    from io import BytesIO
    from pathlib import Path
    from zipfile import ZipFile
    from citrus_audit.workbook import export_reviewed_workbook
    from src.core.cancel import ToolCancelled, raise_if_cancelled
    from src.core.ooxml_determinism import normalize_ooxml_bytes
    from . import tools
    from .audit_projection import iter_findings, location_cells, source_key, table_targets

    if report.get("schema") == "standalone-audit-review/1":
        from citrus_audit.api import AuditSnapshot, export_workbook
        from citrus_audit.reporting import dumps

        receipts, errors = {}, []
        for saved in report["snapshots"]:
            raise_if_cancelled("data_audit_workbook")
            try:
                name, digest = saved["source_name"], saved["source_sha256"]
                if Path(name).suffix.lower() == ".xlsm":
                    raise ValueError("暂不发布 XLSM 审查工作簿，可使用文字审查或 HTML/JSON")
                snapshot = AuditSnapshot(load_source(saved), name, dumps(saved["report"]), digest)
                if snapshot.report_sha256 != saved["report_sha256"]:
                    raise ValueError("审查快照摘要不符")
                content, stats = export_workbook(snapshot, presentation="annotated")
                _require_normal_audit_xlsx(content)
                content = normalize_ooxml_bytes(content)
                filename = tools._short_stem(name) + "_审查标注.xlsx"
                publication_key = sha256(dumps([saved["source_file_id"], digest, snapshot.report_sha256,
                                               "xlsx", "annotated/1"]).encode()).hexdigest()
                url = tools._save_artifact(filename, content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    source_file_id=saved["source_file_id"], source_sha256=digest,
                    logical_task_id="audit_workbook:" + publication_key)
                if not url:
                    raise ValueError("审查交付未获得发布回执")
                receipts[(saved["file_index"], digest)] = {"url": url, "filename": filename,
                    "sha256": sha256(content).hexdigest(), "size_bytes": len(content),
                    "source_file_id": saved["source_file_id"], "source_sha256": digest,
                    "file_index": saved["file_index"], "cells": stats.get("marked_locations", 0)}
            except ToolCancelled:
                raise
            except Exception as exc:
                errors.append(f"{saved['source_name']}: {type(exc).__name__}: {exc}")
        return receipts, errors

    groups, targets_by_location = defaultdict(list), defaultdict(list)
    errors = []
    targets = list(table_targets(report))
    for result in report['table_results']:
        if result.get('ok') and not result.get('table_metadata'):
            errors.append(f"{result['file']}: 审查记录缺少工作表渲染元数据")
    for target in targets:
        key = source_key(target)
        if key is None or (target['region_id'] and (not target['column_numbers'] or not target['row_numbers'])):
            errors.append(f"{target['source_name']}: 无法唯一绑定已审查的来源坐标")
            continue
        target['findings'] = []
        groups[key].append(target)
        targets_by_location[key, target['sheet'], target['region_id']].append(target)
    for finding in iter_findings(report, targets):
        per_target = {}
        for location in finding['locations']:
            key = (source_key(location), location.get('sheet', ''), location.get('region_id') or '')
            matches = targets_by_location.get(key, ())
            if len(matches) == 1:
                target, cells = per_target.setdefault(key, (matches[0], {}))
                cells.update(location_cells(location))
        for target, cells in per_target.values():
            target['findings'].append({'check': finding['rule'], 'severity': finding['severity'],
                'detail': finding['detail'], 'region_id': target['region_id'],
                'finding_id': finding.get('finding_id', ''), 'layer': finding['kind'],
                'coverage_only': finding['kind'] == 'coverage_diagnostic',
                'evidence': {'cells': [{'row': row, 'column_index': column} for row, column in cells]}})
    receipts = {}
    for key, group in groups.items():
        raise_if_cancelled('data_audit_workbook')
        source = group[0]
        file_name, digest = source['source_name'], source['source_sha256']
        try:
            data = load_source(source)
            if sha256(data).hexdigest() != digest:
                raise ValueError('审查原件哈希已变化')
            suffix = Path(file_name).suffix.lower()
            original = suffix in {'.xlsx', '.xlsm'}
            if suffix == '.xlsm':
                with ZipFile(BytesIO(data)) as archive:
                    if any('vbaproject' in name.lower() or '/macrosheets/' in name.lower() for name in archive.namelist()):
                        raise ValueError('含宏工作簿不能发布为 XLSX，可交付 HTML/JSON')
            if not original:
                frames = [tools._load_frame_from_bytes(data, file_name, sheet_name=target['sheet'],
                    header_row=target['header_row'], header_rows=target['header_rows']) for target in group]
                data, names = tools._frames_to_workbook_bytes(frames)
                group = [{**target, 'sheet_name': name} for target, name in zip(group, names)]
            content, stats = export_reviewed_workbook(data, targets=group, source_sha256=digest, original=original)
            content = normalize_ooxml_bytes(content)
            stem = tools._short_stem(file_name)
            index = source['file_index']
            label = tools._safe_name(title) + '_' if title else ''
            filename = f'{label}{index}_{stem}_数据审查.xlsx'
            url = tools._save_artifact(filename, content, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                source_file_id=source['source_file_id'], source_sha256=digest,
                logical_task_id='data_audit:' + review_id if review_id else '')
            if not url:
                raise ValueError('审查交付未获得发布回执')
            receipts[key] = {'url': url, 'filename': filename, 'sha256': sha256(content).hexdigest(),
                             'size_bytes': len(content), 'source_file_id': source['source_file_id'],
                             'source_sha256': digest, 'file_index': index, 'cells': stats['marked_locations']}
        except ToolCancelled:
            raise
        except Exception as exc:
            errors.append(f'{file_name} [{source["file_index"]}]: {type(exc).__name__}: {exc}')
    return receipts, errors


def _publish_data_audit(review_id: str, output_format: str, title: str = '') -> str:
    """Render an existing account/session review; never invoke an audit rule."""
    from html import escape
    from pathlib import Path
    from src.core.tracing import get_user_id, get_session_id
    from src.research.integrity_review import get_review
    from src.research import tools
    from src.session import files
    owner, session_id = int(get_user_id() or 0), str(get_session_id() or '')
    if owner <= 0 or not session_id:
        raise PermissionError('需要已认证账号和会话')
    review = get_review(owner, review_id)
    if not review or review['session_id'] != session_id:
        raise LookupError('审查结果不在本账号本会话范围内，请使用实际返回的 review_id')
    if output_format not in supported_delivery_formats('data_audit'):
        raise ToolArgumentError('数据审查仅支持 xlsx/html/json')
    report = review['report']
    if not report.get('table_results'):
        raise ValueError('该复核记录没有表格扫描结果')
    stem = tools._safe_name(title or '数据审查')
    standalone = report.get("schema") == "standalone-audit-review/1"
    payload = report if standalone else _data_audit_payload(review)
    prepared = []
    if output_format in {'json', 'html'}:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
        if output_format == 'json':
            content, mime = encoded.encode('utf-8'), 'application/json'
        else:
            from .audit_projection import location_cells
            rows = ''.join('<tr>' + ''.join('<td>' + escape(str(value)) + '</td>' for value in (
                loc.get('source_name', ''), loc.get('sheet', ''), loc.get('region_id', ''),
                ', '.join(loc.get('ranges') or location_cells(loc).values()), item['rule'], item['severity'], item['detail'])) + '</tr>'
                for item in payload.get('finding_projection', {}).get('findings', []) for loc in item['locations'])
            content = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>' +
                escape(title or '数据审查') + '</title></head><body><h1>' + escape(title or '数据审查') +
                '</h1><p>' + escape(payload.get('interpretation') or '保存的审查范围、发现及覆盖见下方快照。') + '</p><p>复核记录：' + escape(review_id) +
                '</p><table><thead><tr><th>文件</th><th>工作表</th><th>区域</th><th>单元格</th><th>类别</th>' +
                '<th>严重程度</th><th>说明</th></tr></thead><tbody>' + rows +
                '</tbody></table><details><summary>完整结果、覆盖与规则执行凭证</summary><pre>' +
                escape(encoded) + '</pre></details></body></html>').encode('utf-8')
            mime = 'text/html'
        prepared.append((stem + '.' + output_format, content, mime, {}))
    else:
        def load_source(target):
            source = files.get_account_file(target['source_file_id'], owner, touch=False)
            if not source or source.get('sha256') != target['source_sha256']:
                raise ValueError('审查原件不可用或已变化')
            limit = 30 * 1024 * 1024
            with Path(source['path']).open('rb') as handle:
                data = handle.read(limit + 1)
            if len(data) > limit or len(data) != source.get('size_bytes', len(data)):
                raise ValueError('审查原件超过读取上限或大小不符')
            return data

        receipts, errors = publish_audit_workbooks(report, load_source=load_source, title=title, review_id=review_id)
        links = [receipt['url'] for receipt in receipts.values()]
        if not links and not errors:
            raise ValueError('审查记录没有可交付的数据表')
        error_text = '[ERR_DATA_AUDIT_EXPORT] ' + '；'.join(errors) + '\n' if errors else ''
        return error_text + '同一次扫描的标注工作簿（含原始坐标）：\n' + '\n'.join(links)
    links, errors = [], []
    for filename, content, mime, source_args in prepared:
        try:
            url = tools._save_artifact(filename, content, mime,
                logical_task_id='data_audit:' + review_id, **source_args)
            if not url:
                raise ValueError('审查交付未获得发布回执')
            links.append(url)
        except Exception as exc:
            from src.core.cancel import ToolCancelled
            if isinstance(exc, ToolCancelled):
                raise
            errors.append(f'{filename}: {type(exc).__name__}: {exc}')
    error_text = '[ERR_DATA_AUDIT_EXPORT] ' + '; '.join(errors) + '\n' if errors else ''
    if not prepared:
        raise ValueError('审查记录没有可交付的数据表')
    return error_text + '同一次扫描的审查结果（含实际覆盖与未执行项）：\n' + '\n'.join(links)


def _dispatch(request: DeliveryInput) -> str:
    from src.research import tools
    common = {"title": request.title, "append": request.append}
    if request.kind == 'data_audit':
        return _publish_data_audit(request.review_id, request.output_format, request.title)
    if request.kind == "report":
        if request.purpose == "general":
            return tools._export_markdown(request.title, request.content, "研究报告", request.svg_code, append=request.append)
        name, key = (("export_paper_analysis", "analysis_markdown") if request.purpose == "paper_analysis"
                     else ("export_experiment_design", "design_markdown"))
        return getattr(tools, name).func(**common, **{key: request.content}, svg_code=request.svg_code)
    if request.kind == "table":
        if request.dataset_ref:
            from src.core.tracing import get_user_id
            from src.research.analysis_delivery import dataset_table
            projection = dataset_table(int(get_user_id() or 0), request.dataset_ref.model_dump(),
                                       output_format=request.output_format)
            return tools.export_table.func(**common, columns_json=projection["columns"],
                rows_json=projection["rows"], output_format=request.output_format)
        ref = request.analysis_result_ref
        return tools.export_table.func(**common, columns_json=request.columns, rows_json=request.rows,
            output_format=request.output_format, analysis_result_ref=ref.model_dump() if isinstance(ref, BaseModel) else ref,
            result_section=request.result_section)
    if request.kind == "bibliography":
        if request.audit_id:
            from src.core.tracing import get_user_id, get_session_id, get_client_request_id
            from src.research.reference_audit import export_audit
            from src.research.artifacts import publish_artifact
            owner, session_id = int(get_user_id() or 0), str(get_session_id() or "")
            content, mime, filename = export_audit(owner, session_id, request.audit_id,
                                                  format=request.output_format)
            receipt = publish_artifact(owner=owner, session_id=session_id, filename=filename,
                content=content, mime=mime, producer_tool="export_literature_evidence",
                request_id=str(get_client_request_id() or ""))
            return "文献审查全量导出（含未核验条目与覆盖说明）：" + receipt["artifact_url"]
        return tools.export_literature_evidence.func(**common,
            evidence_rows_json=[row.model_dump() for row in request.references],
            output_format=request.output_format, source=request.source)
    if request.kind == "research_notes":
        return tools.export_research_notes.func(project_name=request.project_name, limit=request.limit, svg_code=request.svg_code)
    return tools.export_corpus_gap_report.func(**common,
        gaps_json=json.dumps([row.model_dump() for row in request.gaps], ensure_ascii=False))


def _receipts(text: str) -> tuple[list[dict], bool]:
    """Verify disk, owner, request AND committed library entry, using existing checks."""
    from src.core.tracing import get_client_request_id, get_user_id
    from src.research import tools
    from src.session import files
    links = list(dict.fromkeys(re.findall(r"/api/v2/session/[A-Za-z0-9_-]+/artifacts/[a-f0-9]{32}", text)))
    if not links:
        return [], False
    verified = tools.validate_artifact_links(links, request_id=str(get_client_request_id() or ""))
    receipts = []
    with closing(sqlite3.connect(str(tools._db_path()))) as conn:
        conn.row_factory = sqlite3.Row
        for item in verified:
            if not item.get("valid"):
                # The legacy matcher compares bibliography MIME strings exactly,
                # while its renderer emits a charset parameter. Reuse that same
                # byte-format matcher with the media type (parameters removed).
                # All ownership/request/hash checks must have passed first.
                if not (item.get("reason_code") == "format_mismatch" and
                        all(item.get(key) for key in ("exists", "hash_matches", "owner_matches", "request_matches"))):
                    continue
                resolved = tools.resolve_artifact(str(tools.get_session_id() or ""), item["artifact_id"])
                if not resolved or not tools._artifact_format_matches(
                        resolved["path"], item["filename"], item["mime"].split(";", 1)[0].strip()):
                    continue
            row = conn.execute("SELECT file_id, artifact_sha256, size_bytes FROM publication_outbox "
                               "WHERE artifact_id=? AND owner_user_id=? AND state='committed'",
                               (item["artifact_id"], int(get_user_id() or 0))).fetchone()
            if not row or not row["file_id"] or int(row["size_bytes"]) <= 0:
                continue
            entry = files.get_account_file(row["file_id"], int(get_user_id() or 0))
            if not entry or entry.get("sha256") != row["artifact_sha256"]:
                continue
            receipts.append({"artifact_id": item["artifact_id"], "file_id": row["file_id"],
                             "filename": item["filename"], "mime": item["mime"], "url": item["link"],
                             "sha256": row["artifact_sha256"], "size_bytes": row["size_bytes"]})
    return receipts, len(receipts) == len(links)


@tool(args_schema=DeliveryInput, response_format="content_and_artifact")
def deliver_research_artifact(**kwargs) -> tuple[str, dict]:
    """将已具备的研究内容发布到资料库，由界面下载导出。

    kind=report：content Markdown，purpose=general/paper_analysis/experiment_design；HTML，可附 svg_code。
    kind=table：columns+rows、analysis_result_ref+result_section 或 dataset_ref；CSV/TSV/XLSX。
    脚本 emit.table 已保存的 dataset_ref 可直接交付，不必重抄数据；引用不能混用行列或追加。
    kind=bibliography：明确 audit_id 导出全量审查（JSON/XLSX/RIS/BibTeX），或 references 导出显式书目（XLSX/RIS/BibTeX）；不读取最近一次核验。
    kind=data_audit：真实 integrity_review.review_id；默认 XLSX，仅在用户明确要求时 HTML/JSON；直接读取同一次扫描与覆盖，勿重抄发现或重跑扫描。
    JSON issues 含原始 sheet/cell/cells/category/severity；XLSX 原件需仍可访问且哈希一致。每次仅发布 output_format 请求的格式。
    kind=research_notes：project_name、limit、可选 svg_code；HTML。
    kind=corpus_gap：gaps；CSV。append 仅用于报告、表格、文献、语料缺口分批追加。
    不生成缺失科学数据；Word 文档编辑请使用 word_document。
    """
    data_audit_note = ""
    if str(kwargs.get("kind") or "") == "data_audit":
        requested_format = str(kwargs.get("output_format") or "").strip().lower()
        if requested_format in {"json", "html"}:
            from src.core.tracing import original_query
            query = original_query() or ""
            if query and not _audit_format_requested(query, requested_format):
                kwargs = {**kwargs, "output_format": "xlsx"}
                data_audit_note = "未要求 JSON/HTML；数据审查默认交付一份原表标注 XLSX。\n"
    request = DeliveryInput.model_validate(kwargs)
    from src.core.tracing import get_current_tool_name, set_current_tool_name
    from .delivery_reconciliation import before_publication, delivery_outcome
    renderer = {"table": "export_table", "bibliography": "export_literature_evidence",
                "research_notes": "export_research_notes", "corpus_gap": "export_corpus_gap_report",
                "data_audit": "audit_uploaded_data"}.get(request.kind)
    if request.kind == "report":
        renderer = {"general": "render_research_report", "paper_analysis": "export_paper_analysis",
                    "experiment_design": "export_experiment_design"}[request.purpose]
    previous_tool = get_current_tool_name()
    try:
        # Persist the concrete format producer, not the union of all possible
        # kinds of this model-facing entrypoint. Historical rows stay unchanged.
        set_current_tool_name(renderer)
        text = _dispatch(request)
    except (ValueError, LookupError, PermissionError, ToolArgumentError) as exc:
        if not before_publication():
            raise
        text = f"[ERR_DELIVERY_PREFLIGHT] {exc}"
    finally:
        set_current_tool_name(previous_tool)
    text = text if isinstance(text, str) else ""
    if data_audit_note:
        text = data_audit_note + text
    receipts, complete = _receipts(text)
    # Legacy renderers report validation/publication errors in-band, including
    # partial HTML success followed by a failed SVG companion publication.
    error = re.search(r"\[(DELIVERABLE_REJECTED(?::[a-z0-9_]+)?|ERR_[A-Z0-9_]+|SVG_FORMAT_ERROR)\]", text)
    expected_extension = ".bib" if request.output_format == "bibtex" else "." + request.output_format
    requested_present = any(r["filename"].lower().endswith(expected_extension) for r in receipts)
    success = bool(receipts) and complete and requested_present and not error
    status = "complete" if success else "partial" if receipts else "failed"
    reason = "" if success else error.group(1).split(":")[-1].lower() if error else "delivery_not_verified"
    outcome = delivery_outcome(status, reason, receipts)
    if outcome["status"] != "complete":
        status, reason = outcome["status"], outcome["reason_code"]
        detail = {"not_run": "发布前校验或导出失败，本次未发布成果",
                  "partial": "已登记部分成果，其余输出失败",
                  "unknown": "部分发布结果尚未确认，请核对原操作"}.get(status, "成果未全部确认入库")
        text = f"[DELIVERY_{status.upper()}] {detail}（{reason}）。\n" + text
    artifact = {"kind": request.kind, "purpose": request.purpose, "format": request.output_format,
                "receipts": receipts, "file_refs": [r["file_id"] for r in receipts],
                "_tool_outcome": outcome}
    if request.dataset_ref:
        artifact["dataset_ref"] = request.dataset_ref.model_dump()
    if request.review_id:
        artifact['review_id'] = request.review_id
        artifact['result_refs'] = [{'kind': 'integrity_review', 'review_id': request.review_id}]
    if request.audit_id:
        artifact["audit_id"] = request.audit_id
        artifact["result_refs"] = [{"kind": "reference_audit", "audit_id": request.audit_id}]
    return text, artifact
