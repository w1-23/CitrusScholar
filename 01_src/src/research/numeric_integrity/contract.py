"""Strict input contract for the internal offline numeric worker."""
import csv
from dataclasses import dataclass
import hashlib
import io
import json
import re
from zipfile import BadZipFile, ZipFile

REQUEST_SCHEMA = "citrus.numeric-integrity-request/1"
REPORT_SCHEMA = "citrus.numeric-integrity-report/1"
MAX_REQUEST = 64 * 1024
MAX_FILE = 8 * 1024**2
MAX_TOTAL = 24 * 1024**2
MAX_CELLS = 250_000
MAX_ROWS = 20_000
MAX_COLUMNS = 200
MAX_CSV_FIELD = 16_384
SHA = re.compile(r"[a-f0-9]{64}")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


@dataclass(frozen=True)
class Source:
    id: str
    version: int
    sha256: str
    format: str
    size_bytes: int
    staged_name: str
    benford_columns: tuple[int, ...]

    @property
    def reference(self):
        return {"id": self.id, "version": self.version, "sha256": self.sha256}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_request_key")
        result[key] = value
    return result


def decode_request(data):
    if not (type(data) is bytes and 0 < len(data) <= MAX_REQUEST):
        raise ValueError("invalid_request_size")
    body = json.loads(data, object_pairs_hook=_unique_pairs)
    if (type(body) is not dict or body.keys() != {"schema", "sources", "enable_benford"}
            or body["schema"] != REQUEST_SCHEMA or type(body["enable_benford"]) is not bool):
        raise ValueError("invalid_request_contract")
    rows = body["sources"]
    if type(rows) is not list or not 1 <= len(rows) <= 4:
        raise ValueError("invalid_source_count")
    sources, identities = [], set()
    for index, row in enumerate(rows, 1):
        if type(row) is not dict or row.keys() != {"id", "version", "sha256", "format", "size_bytes", "benford_columns"}:
            raise ValueError("invalid_source_contract")
        ref, version, digest = row["id"], row["version"], row["sha256"]
        fmt, size, columns = row["format"], row["size_bytes"], row["benford_columns"]
        if (type(ref) is not str or not IDENTIFIER.fullmatch(ref) or type(version) is not int or version < 1
                or type(digest) is not str or not SHA.fullmatch(digest) or fmt not in ("csv", "xlsx")
                or type(size) is not int or not 1 <= size <= MAX_FILE or (ref, version) in identities):
            raise ValueError("invalid_source_identity")
        if (type(columns) is not list or len(columns) > 8 or
                any(type(c) is not int or not 1 <= c <= MAX_COLUMNS for c in columns) or
                len(set(columns)) != len(columns) or
                columns and (not body["enable_benford"] or fmt != "csv")):
            raise ValueError("invalid_benford_scope")
        identities.add((ref, version))
        sources.append(Source(ref, version, digest, fmt, size, f"source-{index:02d}.{fmt}", tuple(sorted(columns))))
    if sum(source.size_bytes for source in sources) > MAX_TOTAL:
        raise ValueError("total_source_limit")
    return sources, body["enable_benford"]


def csv_records(data):
    old_limit = csv.field_size_limit(MAX_CSV_FIELD)
    try:
        with io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="") as text:
            rows = csv.reader(text, strict=True)
            yield from rows
    finally:
        csv.field_size_limit(old_limit)


def verified_source(data, source):
    if len(data) != source.size_bytes or hashlib.sha256(data).hexdigest() != source.sha256:
        raise ValueError("source_hash_mismatch")
    if source.format == "csv":
        try:
            records = csv_records(data)
            header = next(records, [])
            if not 1 <= len(header) <= MAX_COLUMNS or (source.benford_columns and max(source.benford_columns) > len(header)):
                raise ValueError("csv_column_scope_invalid")
            cells = len(header)
            for row_id, record in enumerate(records, 2):
                cells += len(record)
                if row_id > MAX_ROWS or len(record) > len(header) or cells > MAX_CELLS:
                    raise ValueError("csv_scan_limit")
        except (UnicodeError, csv.Error) as exc:
            raise ValueError("invalid_csv") from exc
    else:
        try:
            with ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if (len(names) > 256 or len(names) != len(set(names))
                        or sum(entry.file_size for entry in archive.infolist()) > 48 * 1024**2
                        or any(".." in name.split("/") or name.startswith("/") or "\\" in name for name in names)
                        or "xl/workbook.xml" not in names or "[Content_Types].xml" not in names
                        or any(entry.flag_bits & 1 for entry in archive.infolist())):
                    raise ValueError("workbook_archive_limit_or_format")
        except BadZipFile as exc:
            raise ValueError("invalid_workbook") from exc
    return data
