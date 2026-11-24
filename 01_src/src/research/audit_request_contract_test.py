"""Contract regressions for the hosted audit entry and delivery format."""
from __future__ import annotations

import pytest

from src.core.tool_errors import ToolArgumentError
from src.research import audit_review_adapter, delivery_tools, tools

REGIONS = [
    {"sheet": "Sheet1", "bounds": "A1:B4", "header_rows": [1], "region_id": "block1"},
    {"sheet": "Sheet1", "bounds": "H1:I4", "header_rows": [1], "region_id": "block2"},
    {"sheet": "Sheet1", "bounds": "A15:B18", "header_rows": [15], "region_id": "block3"},
    {"sheet": "Sheet1", "bounds": "H15:I18", "header_rows": [15], "region_id": "block4"},
    {"sheet": "Sheet1", "bounds": "A34:B37", "header_rows": [34], "region_id": "block5"},
    {"sheet": "Sheet1", "bounds": "H34:I37", "header_rows": [34], "region_id": "block6"},
]


def test_audit_accepts_package_profiles_and_json_format_string():
    for profile in ("balanced", "duplicates", "relations", "numeric_forensics",
                    "custom", "on_demand"):
        validated = tools.validate_audit_request(
            checks_json={"profile": profile, "requested_checks": ["duplicate_row"]},
            publish_formats='["xlsx"]')
        assert validated["formats"] == ["xlsx"]


def test_single_selected_file_fills_missing_region_file_index():
    validated = tools.validate_audit_request(
        checks_json={"regions": REGIONS, "requested_checks": ["duplicate_row"]},
        file_indexes_json=[0])
    assert [row["file_index"] for row in validated["regions"]] == [0] * 6


def test_multiple_selected_files_still_require_region_file_index():
    with pytest.raises(ToolArgumentError):
        tools.validate_audit_request(
            checks_json={"regions": REGIONS},
            file_indexes_json=[0, 1])


def test_default_file_index_cannot_bypass_overlap_validation():
    overlapping = [
        {"sheet": "Sheet1", "bounds": "A1:B4", "header_rows": [1]},
        {"sheet": "Sheet1", "bounds": "A3:B6", "header_rows": [3], "file_index": 0},
    ]
    with pytest.raises(ToolArgumentError):
        tools.validate_audit_request(
            checks_json={"regions": overlapping},
            file_indexes_json=[0])


def test_explicit_duplicate_group_resolves_to_duplicate_row():
    assert audit_review_adapter._selected_checks(
        {"requested_checks": ["duplicates"]}, "") == ["duplicate_row"]
    assert audit_review_adapter._selected_checks(
        {"requested_checks": ["duplicate_rows"]}, "") == ["duplicate_row"]
    assert audit_review_adapter._selected_checks(
        {"requested_checks": ["duplicate_row", "duplicate_row"]}, "") == ["duplicate_row"]


def test_only_duplicate_query_does_not_add_iqr_or_group_checks():
    selected = audit_review_adapter._selected_checks(
        {}, "请审查附件 Sheet1 的全部六块数据，仅检查完全重复记录。")
    assert selected == ["duplicate_row"]
    assert "iqr_outlier" not in selected


def test_data_audit_non_xlsx_formats_require_explicit_user_request():
    xlsx_only = "请审查附件 Sheet1，直接交付一份带原坐标和跳转的标注 XLSX。"
    assert not delivery_tools._audit_format_requested(xlsx_only, "json")
    assert not delivery_tools._audit_format_requested(xlsx_only, "html")
    assert delivery_tools._audit_format_requested("请同时导出 JSON 证据。", "json")
    assert delivery_tools._audit_format_requested("请生成 HTML 审查报告。", "html")
