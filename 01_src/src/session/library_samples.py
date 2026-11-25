"""Small, read-only samples for account-library table cards."""
from __future__ import annotations

from csv import Error as CsvError, reader
from datetime import date, datetime, time
from io import BytesIO, StringIO
from itertools import islice
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_BYTES = 256 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024
MAX_ROWS = 3
MAX_COLUMNS = 4


def _cell(value) -> str:
    if type(value) not in (str, int, float, bool, datetime, date, time):
        return ""
    return " ".join(str(value).split())[:64]


def table_sample(info: dict) -> dict | None:
    if info.get("kind") not in {None, "table"}:
        return None
    suffix = Path(info.get("filename") or "").suffix.casefold()
    if suffix not in {".csv", ".tsv", ".xlsx", ".xlsm"} or not 0 < int(info.get("size_bytes") or 0) <= MAX_BYTES:
        return None
    book = None
    try:
        with Path(info["path"]).open("rb") as file:
            raw = file.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES or len(raw) != info["size_bytes"]:
            return None
        if suffix in {".csv", ".tsv"}:
            text = raw.decode("utf-8-sig")
            if "\x00" in text:
                return None
            values = reader(StringIO(text), delimiter="\t" if suffix == ".tsv" else ",", strict=True)
        else:
            with ZipFile(BytesIO(raw)) as archive:
                members = archive.infolist()
                if len(members) > 128 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
                    return None
                if any(item.flag_bits & 1 for item in members):
                    return None
            from openpyxl import load_workbook
            book = load_workbook(BytesIO(raw), read_only=True, data_only=True, keep_links=False)
            if not book.worksheets:
                return None
            values = book.worksheets[0].iter_rows(max_row=MAX_ROWS, max_col=MAX_COLUMNS, values_only=True)
        rows = [[_cell(value) for value in row[:MAX_COLUMNS]] for row in islice(values, MAX_ROWS)]
        return {"rows": rows} if any(any(row) for row in rows) else None
    except (OSError, ValueError, LookupError, TypeError, CsvError, BadZipFile):
        return None
    except Exception:  # A malformed workbook is not eligible for a thumbnail.
        return None
    finally:
        if book is not None:
            book.close()
