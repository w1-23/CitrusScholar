"""The product-side deliverable contract, distinct from execution authorization.

Explicit output requests are checked against registered artifacts and scope.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import re
from typing import Literal

REPAIR_TOOLS = {
    "figure": ("run_research_figure_script",),
    "verification": ("verify_uploaded_references",),
    "audit": ("audit_uploaded_data",),
    "input": ("request_user_input",),
}
FIGURE_KINDS = frozenset({"figure", "figure_package"})

@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    kind: str
    subject: dict
    acceptance: str = "observed"
    required: bool = True

    def repair_tools(self, available=()):
        names = set(available)
        candidates = (("research_workspace",) if self.kind == "figure" and self.subject.get("phase") == "edit"
                      else REPAIR_TOOLS.get(self.kind, ()))
        return tuple(n for n in candidates if n in names)

@dataclass(frozen=True)
class TaskContract:
    query: str = ""
    obligations: tuple[Obligation, ...] = ()
    notes: tuple[str, ...] = ()
    source_scope: str = "auto"
    understanding_status: str = "structured"
    schema_version: int = 1

    @property
    def deliverables(self):
        return [dict(o.subject["requirement"]) for o in self.obligations
                if o.kind in {"figure", "deliverable"}]

    def as_dict(self):
        return asdict(self)


def _products(requirements):
    return [Obligation("deliverable:" + str(r.get("product_id") or i),
            "figure" if FIGURE_KINDS.intersection(r.get("kinds", [])) else "deliverable",
            {"requirement": dict(r), "label": str(r.get("label") or ""),
             "phase": r.get("phase", "initial"), "figure_id": r.get("figure_id", "")},
            "validated_artifact_and_initial_nature" if FIGURE_KINDS.intersection(r.get("kinds", []))
            else "validated_artifact") for i, r in enumerate(requirements)]


def contract_for_explicit_outputs(query: str) -> TaskContract:
    from src.graph.export_contract import deliverable_contract
    requirements = [dict(item, all_formats=True) for item in deliverable_contract(query)]
    return TaskContract(obligations=tuple(_products(requirements)))



@dataclass(frozen=True)
class FusionDelivery:
    """Server interpretation of the current user turn, never a tool argument."""

    intent: Literal["unspecified", "text_only", "annotated_xlsx", "alternate_file"] = "unspecified"
    format: str = ""


_AUDIT_RESULT = re.compile(
    r"审查|核验|数据(?:检查|完整性)|科研诚信|重复(?:数据|数值|数字)?(?:区段|块|项|段|异常)|异常(?:项|清单|列表)|问题清单|标注工作簿|"
    r"\b(?:audit|integrity|findings?|verification|duplicate blocks?|review results?)\b", re.I)
_SAVE = r"(?:保存(?:为)?|另存(?:为)?|导出(?:为)?|下载|生成|制作|交付|写入|做成|整理成|输出(?:为)?|给我|提供|要一份|\b(?:export|download|create|produce|save|deliver|generate)\b)"
_FILE_WORD = r"(?:文件|工作簿|表格|审查表|标注表|\b(?:xlsx|excel|workbook|spreadsheet|file)\b)"
_XLSX = r"(?:工作簿|Excel|\bxlsx\b|\b(?:workbook|spreadsheet)\b|标注(?:版|表格?|副本))"
_ALTERNATE_FORMAT = r"\b(csv|tsv|json|html|pdf|docx|word)\b"
_OTHER_FILE = r"(?:" + _ALTERNATE_FORMAT + r"|报告文件|文件版|可下载(?:的)?(?:报告|清单|文件))"
_DELIVERY_QUESTION = re.compile(r"如何|怎么|怎样|能否解释|\bhow\s+(?:do|can|to)\b|\bwhat\s+(?:is|does)\b", re.I)
_NO_FILE = re.compile(
    r"(?:只|仅)(?:要|给|用|输出|提供)?\s*(?:文字|文本|在(?:聊天|对话|回答|这里)(?:里|中)?)|"
    r"(?:只|仅)(?:列出|展示|罗列)(?:就行|即可|\s*在(?:聊天|对话|回答|这里)(?:里|中)?)|"
    r"(?:不用|不要|无需|不需要|不必|禁止|别)\s*(?:额外|再|自动)?\s*(?:生成|另存|保存|交付|导出|提供|输出|制作)?\s*" + _FILE_WORD +
    r"|不(?:再|自动)?(?:生成|另存|保存|交付|导出|提供|输出|制作)\s*" + _FILE_WORD +
    r"|\b(?:text[- ]only|in chat only|(?:no|without) (?:an? )?(?:files?|xlsx|excel|workbook|spreadsheet))\b"
    r"|\b(?:do not|don't)\s+(?:create|export|save|generate|deliver)\s+(?:an? )?(?:files?|xlsx|excel|workbook|spreadsheet)\b", re.I)


def fusion_delivery_for_turn(query: str, *, has_tables: bool = False) -> FusionDelivery:
    from src.graph.export_contract import _negated_output_action, deliverable_contract
    from src.graph.intent_text import intent_clauses, mask_literal_payload, negated_after

    content = mask_literal_payload(query)
    clauses = [clause for clause in intent_clauses(content) if not _DELIVERY_QUESTION.search(clause)]
    requested = "。".join(clauses)
    alt = re.compile(_SAVE + r"[^，。]{0,50}?" + _OTHER_FILE, re.I)
    xlsx = re.compile(_SAVE + r"[^，。]{0,40}?" + _XLSX + r"(?!\s*(?:审查|检查|里|中|中的|数据|格式是什么))", re.I)
    explicit_copy = re.compile(
        r"(?:在(?:原|复制的|给我一份)[^，。]{0,18}(?:工作簿|xlsx|excel)[^，。]{0,18}(?:标红|标注)|"
        r"(?:标红|标注)[^，。]{0,18}(?:另存|副本|可下载)|"
        r"(?:给我|需要|要)[^，。]{0,14}(?:标红|标注)[^，。]{0,8}(?:工作簿|xlsx|excel|副本))", re.I)
    def positive_matches(pattern):
        return (match for clause in clauses for match in pattern.finditer(clause)
                if not _negated_output_action(clause, match.start()) and not negated_after(clause, match.end()))

    formats = []
    for match in positive_matches(alt):
        fmt = re.search(_ALTERNATE_FORMAT, match.group(0), re.I)
        formats.append(("docx" if fmt.group(1).lower() == "word" else fmt.group(1).lower()) if fmt else "html")
    no_file = _NO_FILE.search(requested)
    if no_file and not (formats and re.search(_XLSX, no_file.group(0), re.I)):
        return FusionDelivery("text_only")
    # Multiple competing formats must not silently select one workbook.
    if formats:
        # The verb matcher sees only the first member of "export CSV and PDF".
        # Retain all stated formats; one Fusion projection cannot fulfill a mixed request.
        stated = {fmt for item in deliverable_contract(query)
                  if re.search(_ALTERNATE_FORMAT + "|" + _XLSX, item["label"], re.I)
                  for fmt in item["formats"]}
        return FusionDelivery("alternate_file", formats[0] if len(set(formats) | stated) == 1 else "")
    if ((has_tables or _AUDIT_RESULT.search(content))
            and (next(positive_matches(xlsx), None) or next(positive_matches(explicit_copy), None))):
        return FusionDelivery("annotated_xlsx", "xlsx")
    return FusionDelivery()


def contract_for_turn(query: str, *, has_tables: bool) -> TaskContract:
    from src.graph.intent_text import intent_clauses, intent_with_negation, mask_literal_payload

    explicit = contract_for_explicit_outputs(query)
    content = mask_literal_payload(query)
    drawing = re.compile(r"(?:绘制|绘图|画|可视化).{0,16}(?:数据|表格|附件|箱线|热图|图)|"
                         r"\bplot\s+(?:this|the|these|data)\b", re.I)
    short_drawing = re.compile(
        r"(?:好[的吧]?\s*)?(?:(?:请你?|帮我|麻烦你?|直接|继续|先|再)\s*)*"
        r"(?:画一下(?:\s*图)?|画(?:一张|个|一幅)?\s*图|作图|绘图|出图)(?:吧|即可)?")
    named_drawing = re.compile(
        r"\s*(?:(?:请你?|帮我|麻烦你?|直接|继续|先|再)\s*)*"
        r"(?:画|绘制|生成|制作)(?:一张|一个|一幅)?[^？！?!。；;]{0,60}"
        r"(?:箱线图|热图|热力图|折线图|散点图|柱状图|流程图|示意图)(?:吧|即可)?\s*")
    delivery = fusion_delivery_for_turn(query, has_tables=has_tables)
    text_only = delivery.intent == "text_only"
    requirements = explicit.deliverables
    if not text_only and not any(FIGURE_KINDS.intersection(item["kinds"]) for item in requirements):
        if has_tables and intent_with_negation(content, drawing):
            requirements.append({"product_id": "attachment_figure", "label": "附件数据图",
                                 "kinds": ["figure"], "formats": ["svg"]})
        elif any(short_drawing.fullmatch(clause.strip()) or named_drawing.fullmatch(clause)
                 for clause in intent_clauses(content)):
            requirements.append({"product_id": "requested_figure", "label": "图件",
                                 "kinds": ["figure"], "formats": ["svg"]})
    audit_turn = _AUDIT_RESULT.search(content) or (has_tables and re.search(r"检查|校验", content))
    if audit_turn and delivery.intent == "annotated_xlsx":
        literature = bool(re.search(r"文献|参考文献|\b(?:references?|bibliography|citations?)\b", content, re.I))
        workbook = {"product_id": "verification_workbook" if literature else "annotated_workbook",
                    "label": "文献核验结果工作簿" if literature else "数据审查标注工作簿",
                    "kinds": ["verification_sheet" if literature else "annotation_table"], "formats": ["xlsx"]}
        candidates = [i for i, item in enumerate(requirements) if item["formats"] == ["xlsx"]]
        if not candidates:
            requirements.append(workbook)
        for i in candidates:
            if set(requirements[i]["kinds"]) <= {"annotation_table", "verification_sheet", "table_file"}:
                requirements[i] = dict(requirements[i], kinds=workbook["kinds"])
    elif audit_turn and delivery.intent == "alternate_file" and delivery.format:
        # Keep the explicit artifact contract as parsed, including quantity and
        # other products. Correct only the generic parser's implied DOCX for a
        # formatless report-file request that Fusion expressly renders as HTML.
        if not requirements:
            requirements.append({"product_id": "external_review_report", "label": "审查结果文件",
                                 "kinds": [{"csv": "table_file", "tsv": "table_file", "json": "report_json",
                                            "html": "report_html", "docx": "report_docx"}.get(
                                                delivery.format, "generic_output")], "formats": [delivery.format]})
        elif delivery.format == "html" and not any("html" in item["formats"] for item in requirements):
            for i, item in enumerate(requirements):
                if (item["kinds"] == ["report_docx"] and item["formats"] == ["docx"]
                        and re.search(_OTHER_FILE, item["label"], re.I)
                        and not re.search(_ALTERNATE_FORMAT, item["label"], re.I)):
                    requirements[i] = dict(item, kinds=["report_html"], formats=["html"])
    return TaskContract(obligations=tuple(_products(requirements)))


def build_task_contract(*, query="", request_plan=None, request_constraints=None,
                        table_count=0, explicit_reference_check=False, has_paper_attachment=False):
    """Conservative migration fallback; never silently declare its scope certain."""
    obligations = _products(getattr(request_plan, "deliverables", ()))
    if explicit_reference_check:
        obligations.append(Obligation("verification:references", "verification",
                                      {"file_indexes": [], "scope": "source_records"}))
    if getattr(request_constraints, "data_audit_requested", False):
        # The fallback does not know whether the user narrowed the target. Do
        # not instruct the executor to audit additional files based on a count.
        obligations.append(Obligation("audit:tables", "audit", {"file_indexes": [], "scope": "unknown"}))
    return TaskContract(query, tuple(obligations),
        ("任务范围未取得结构化确认；仅保留已有交付提示，不能据此扩展检查范围。",),
        getattr(request_plan, "selected_scope", "auto"), "unavailable")
