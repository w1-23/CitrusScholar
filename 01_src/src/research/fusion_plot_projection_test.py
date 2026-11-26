from hashlib import sha256
from io import BytesIO
import json

from openpyxl import Workbook
import pytest

from citrus_audit.envelope import TableEnvelope
from src.research.fusion_plot_projection import freeze_confirmed_workbook


def test_fusion_projection_keeps_exact_rows_columns_and_source_digest(monkeypatch):
    book = Workbook()
    sheet = book.active
    sheet.title = "Trials"
    for coord, value in {"A3": "outside", "B3": "group", "C3": "private", "D3": "score",
                         "B4": "control", "C4": "excluded", "D4": 12.5,
                         "B5": "trial", "D5": "=6*4", "B7": "outside"}.items():
        sheet[coord] = value
    out = BytesIO()
    book.save(out)
    source = out.getvalue()
    digest = sha256(source).hexdigest()
    args = {"source_sha256": digest, "regions": [{"sheet": "Trials", "header_rows": [3],
             "bounds": "B3:D6", "region_id": "r1"}], "fields": ["score", "group"]}
    encoded = freeze_confirmed_workbook(source, "test.xlsx", **args)
    one, = json.loads(encoded)
    table = TableEnvelope.from_dict(one)
    assert table.columns == ("score", "group") and table.column_numbers == (4, 2)
    assert table.row_numbers == (4, 5, 6) and table.values == ((12.5, "control"), (None, "trial"), (None, None))
    assert table.sheet == "Trials" and table.source_sha256 == digest and table.region_id == "r1"
    assert b"excluded" not in encoded and b"outside" not in encoded
    with pytest.raises(ValueError, match="scope_invalid"):
        freeze_confirmed_workbook(source, "test.xlsx", **{**args, "source_sha256": "f" * 64})
    with pytest.raises(ValueError, match="fields_unavailable"):
        freeze_confirmed_workbook(source, "test.xlsx", **{**args, "fields": ["outside"]})
    monkeypatch.setattr("src.research.fusion_plot_projection.MAX_PROJECTION_BYTES", 4)
    with pytest.raises(ValueError, match="byte_limit"):
        freeze_confirmed_workbook(source, "test.xlsx", **args)
