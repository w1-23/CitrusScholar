"""Private workbook projections using the pinned analyzer's cell semantics."""
from contextlib import closing
import hashlib
import json

from openpyxl.utils.cell import range_boundaries
from citrus_audit.envelope import TableEnvelope
from citrus_audit.readers import open_xlsx_readonly
from src.figures.dataset_codec import MAX_CELLS, MAX_COLUMNS

MAX_PROJECTION_BYTES = 1024 * 1024


def freeze_confirmed_workbook(data, filename, *, source_sha256, regions, fields):
    """Select exact columns from previously confirmed regions, not a preview.

    No persistence occurs here. A caller must bind these bytes to the submitted
    Worker input and require its accepted receipt before exposing any dataset.
    """
    if (hashlib.sha256(data).hexdigest() != source_sha256 or not fields
            or len(fields) > MAX_COLUMNS or len(set(fields)) != len(fields)
            or not 1 <= len(regions) <= 16):
        raise ValueError("fusion_projection_scope_invalid")
    projections, cells, text_bytes = [], 0, 0
    with (closing(open_xlsx_readonly(data, filename, data_only=False)) as headers,
          closing(open_xlsx_readonly(data, filename, data_only=True)) as cached):
        for region in regions:
            left, top, right, bottom = range_boundaries(region["bounds"])
            header, = region["header_rows"]
            if not top <= header < bottom or (bottom - top + 1) * (right - left + 1) > 50_000:
                raise ValueError("fusion_projection_region_invalid")
            sheet = region["sheet"]
            source = headers[sheet]
            sheet_values = cached[sheet]
            source.reset_dimensions()
            sheet_values.reset_dimensions()
            raw = next(source.iter_rows(min_row=header, max_row=header, min_col=left, max_col=right,
                                        values_only=True), ())
            # Same header semantics as the fixed Fusion data-audit adapter.
            columns = tuple(str(cell or "").strip() for cell in raw)
            if len(columns) != right - left + 1 or not all(columns) or len(set(columns)) != len(columns):
                raise ValueError("fusion_projection_header_invalid")
            if not set(fields) <= set(columns):
                continue
            positions = tuple(left + columns.index(name) for name in fields)
            first, last = min(positions), max(positions)
            rows = tuple(range(header + 1, bottom + 1))
            cells += len(rows) * len(fields)
            if cells > MAX_CELLS:
                raise ValueError("fusion_projection_cell_limit")
            offsets = tuple(i - first for i in positions)
            selected = []
            for row in sheet_values.iter_rows(min_row=header + 1, max_row=bottom,
                                                 min_col=first, max_col=last, values_only=True):
                values = tuple(row[i] for i in offsets)
                text_bytes += sum(len(value.encode("utf-8")) for value in values if isinstance(value, str))
                if text_bytes > MAX_PROJECTION_BYTES:
                    raise ValueError("fusion_projection_byte_limit")
                selected.append(values)
            selected.extend([(None,) * len(fields)] * (len(rows) - len(selected)))
            table = TableEnvelope(tuple(fields), tuple(selected), rows, positions, source_sha256,
                                  source_name=filename, sheet=sheet, header_row=header,
                                  region_id=region["region_id"], region_bounds=region["bounds"])
            projections.append(table.to_dict())
    if not projections:
        raise ValueError("fusion_projection_fields_unavailable")
    payload = json.dumps(projections, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_PROJECTION_BYTES:
        raise ValueError("fusion_projection_byte_limit")
    return payload



def publish_verified_workbook_projection(identity, value, execution_id):
    """Convert only a verified Worker projection into private dataset references."""
    from src.agent.evidence_cache import publish_audit_dataset
    from src.agent.fusion_worker_contract import workbook_regions
    from src.core.account_leases import account_lease

    receipt = value["receipt"]
    report = value["report"]
    if (value["status"] != "succeeded" or report["status"] != "complete"
            or receipt.get("execution_id") != execution_id or receipt.get("process_stopped") is not True
            or receipt.get("network_policy") != "offline"):
        raise PermissionError("fusion_projection_report_unverified")
    source = value["inputs"]["workbook"]
    regions = {region["region_id"]: region for region in workbook_regions(value["options"]["regions"])}
    checked = {item.get("region_id") for item in report["coverage"]
               if item.get("check") == "eda_profile" and item.get("status") == "checked"}
    raw = value["_plot_projection"]
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PROJECTION_BYTES:
        raise ValueError("fusion_projection_byte_limit")
    payload = json.loads(raw)
    if not isinstance(payload, list) or not 1 <= len(payload) <= len(regions):
        raise ValueError("fusion_projection_shape_invalid")
    seen = set()
    for entry in payload:
        table = TableEnvelope.from_dict(entry)
        region = regions[table.region_id]
        if (table.region_id in seen or table.region_id not in checked or table.source_sha256 != source["sha256"]
                or table.sheet != region["sheet"] or table.region_bounds != region["bounds"]
                or table.header_rows != tuple(region["header_rows"])):
            raise ValueError("fusion_projection_region_mismatch")
        seen.add(table.region_id)
    with account_lease(identity.owner_user_id, expected_epoch=value["_account_epoch"]):
        return [publish_audit_dataset(owner=identity.owner_user_id, session=identity.session_id,
            source_file_id=source["file_id"], source_sha256=source["sha256"], execution_id=execution_id,
            table=entry) for entry in payload]
