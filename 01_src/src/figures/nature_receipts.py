"""Host-side acceptance of the installed Nature worker's actual QA receipt."""
from __future__ import annotations
import hashlib
import json
import re

from citrus_figures.nature_runtime import COMMIT, RELEASE_VENDOR_COMMIT


def validate_nature_receipt(manifest: dict, *, pdf: bytes | None = None, expected_commit=None) -> dict:
    receipt = manifest.get("nature_skill_receipt")
    commits = (COMMIT, RELEASE_VENDOR_COMMIT) if expected_commit is None else (expected_commit,)
    if (not isinstance(receipt, dict) or receipt.get("version") != 1
            or receipt.get("commit") not in commits):
        raise ValueError("工作器未返回当前 Nature 核心检查回执；请部署匹配版本的科研沙箱。")
    checks = receipt.get("checks")
    allowed = {
        "source_preflight": {"pass", "review_required"},
        "panel_alignment": {"pass", "warn", "not_applicable"},
        "pdf_text": {"pass", "warn"},
        "pdf_collisions": {"pass", "warn"},
    }
    policy = receipt.get("policy")
    ordinary = isinstance(policy, dict) and policy.get("journal_strict") is False
    statuses = {"pass", "warn", "review_required"}
    if ordinary:
        statuses.update({"fail", "not_auditable"})
        for states in allowed.values():
            states.update({"fail", "not_auditable"})
    if not isinstance(checks, dict) or any(
        not isinstance(checks.get(name), dict) or checks[name].get("status") not in states
        for name, states in allowed.items()
    ):
        raise ValueError("Nature 图件检查失败或无法审计；保留诊断，未发布图件。")
    if receipt.get("status") not in statuses:
        raise ValueError("Nature 图件检查总状态无效。")
    digest = receipt.get("pdf_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Nature 检查缺少最终 PDF 摘要。")
    if pdf is not None and hashlib.sha256(pdf).hexdigest() != digest:
        raise ValueError("发布 PDF 与 Nature 检查的版本不一致。")
    return receipt


def current_receipt_id(receipt: dict) -> str:
    value = {key: item for key, item in receipt.items() if key != "receipt_id"}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def accept_current_render_qa(manifest: dict, blobs: dict[str, bytes], mode: str) -> dict:
    """Accept one worker receipt and bind its figures to the exact exports."""
    from citrus_figures.verification import CHECKS
    from src.figures.nature_runtime import current_security_findings, expected_vendor_commit

    def reject():
        raise ValueError("当前图件 QA 回执缺失、未绑定实际导出或含阻断结果。")

    acknowledgement = manifest.get("render_receipt") or {}
    receipt = manifest.get("current_qa_receipt", acknowledgement.get("current_qa_receipt"))
    layout = manifest.get("current_qa_layout", acknowledgement.get("current_qa_layout"))
    if (not isinstance(receipt, dict) or receipt.get("version") != 2
            or receipt.get("mode") != mode or receipt.get("skill") != "nature-figure"
            or receipt.get("vendor_commit") != expected_vendor_commit()
            or receipt.get("receipt_id") != current_receipt_id(receipt)):
        reject()
    for key, value in (("current_qa_receipt", receipt), ("current_qa_layout", layout)):
        if key in acknowledgement and acknowledgement[key] != value:
            reject()
    checks = receipt.get("checks")
    artifacts = receipt.get("artifacts")
    if (not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks)
            or {c.get("check") for c in checks} != set(CHECKS)
            or not isinstance(artifacts, list) or not all(isinstance(a, dict) for a in artifacts)
            or not isinstance(receipt.get("overall"), dict) or receipt["overall"].get("blocking")
            or current_security_findings(receipt)):
        reject()
    measured = {row.get("format"): row for row in artifacts}
    if (not blobs or len(measured) != len(artifacts)
            or set(measured) not in (set(blobs), {*blobs, "pdf"})):
        reject()
    for fmt, blob in blobs.items():
        if measured[fmt] != {"format": fmt, "role": "figure", "size": len(blob),
                             "sha256": hashlib.sha256(blob).hexdigest()}:
            reject()
    pdf = measured.get("pdf")
    policy = receipt.get("policy") or {}
    panel = next(check for check in checks if check.get("check") == "panel_alignment")
    no_layout = (layout is None and policy.get("journal_strict") is False
                 and panel.get("executed") == "not_run" and panel.get("conclusion") == "undetermined"
                 and panel.get("reason_code") == "bound_layout_absent" and panel.get("artifact") is None)
    if pdf is None:
        if (not no_layout or manifest.get("nature_skill_receipt")
                or (mode == "initial" and acknowledgement.get("nature_audit") != "not_requested")):
            reject()
    else:
        digest = pdf.get("sha256")
        if (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(pdf.get("size")) is not int or pdf["size"] <= 0
                or ("pdf" not in blobs and pdf.get("role") != "internal_qa")
                or (not no_layout and (not isinstance(layout, dict) or layout.get("artifact_sha256") != digest
                    or layout.get("capture") != "pdf_draw_event" or layout.get("rerendered") is not False
                    or not isinstance(layout.get("layout"), dict)))):
            reject()
        if mode == "initial":
            legacy = validate_nature_receipt(manifest, pdf=blobs.get("pdf"),
                                             expected_commit=receipt["vendor_commit"])
            if legacy["pdf_sha256"] != digest:
                reject()
    integrity = [c.get("artifact") for c in checks if c.get("check") == "format_integrity"
                 and c.get("executed") == "executed" and c.get("conclusion") == "pass"]
    if len(integrity) != len(artifacts) or any(row not in integrity for row in artifacts):
        reject()
    for check in checks:
        name = check.get("check")
        artifact = check.get("artifact")
        if (check.get("executed") not in {"not_run", "attempted", "executed"}
                or check.get("conclusion") not in {"pass", "warn", "fail", "not_applicable", "undetermined"}
                or (name in {"pdf_text", "pdf_collisions"} and (artifact != pdf or
                    (pdf is None and (check.get("executed"), check.get("conclusion"), check.get("reason_code"))
                     != ("not_run", "undetermined", "format_absent"))))
                or (name == "panel_alignment" and not no_layout and artifact != pdf)
                or (name == "svg_schematic" and artifact != measured.get("svg"))
                or (name == "visual" and check.get("executed") != "not_run")):
            reject()
    manifest["current_qa_receipt"] = receipt
    manifest["current_qa_layout"] = layout
    acknowledgement.pop("current_qa_receipt", None)
    acknowledgement.pop("current_qa_layout", None)
    return receipt


def current_qa_summary(manifest: dict) -> dict:
    """Give the caller evidence identities without the raw diagnostics."""
    receipt = manifest.get("current_qa_receipt") or {}
    if not receipt:
        return {}
    return {"receipt_id": receipt["receipt_id"], "overall": receipt["overall"],
            "checks": [{key: check[key] for key in ("check", "executed", "conclusion",
                       "reason_code", "artifact")} for check in receipt["checks"]]}
