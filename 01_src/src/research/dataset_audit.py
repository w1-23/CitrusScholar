"""Prepare a trusted DatasetRef and audit it without re-parsing the original file."""
from __future__ import annotations

from csv import Error as CsvError
from datetime import date, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from zipfile import BadZipFile

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import dataset_audit_reader as reader
from .analysis_results import _load as load_snapshot_metadata, persist_prepared_dataset

_SCHEMA = 'dataset-audit/1'
_PARSER_HASH = sha256((_SCHEMA + '/parser/1').encode()).hexdigest()
FileId = Annotated[str, Field(pattern=r'^[a-f0-9]{32}$')]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Column = Annotated[str, Field(min_length=1, max_length=160)]
Check = Literal['missing', 'duplicate', 'type', 'range', 'category', 'outlier']


class _Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


class DatasetRef(_Contract):
    kind: Literal['dataset_snapshot']
    id: FileId
    version: Annotated[int, Field(strict=True, ge=1)]
    sha256: Digest


class WorkbookScope(_Contract):
    """Only the Host may create this from the authoritative user selection."""
    sheet: Annotated[str, Field(min_length=1, max_length=31)]
    bounds: Annotated[str, Field(min_length=3, max_length=32)] | None = None
    header_rows: tuple[Annotated[int, Field(strict=True, ge=1)], ...] = Field(default=(), max_length=8)

    @model_validator(mode='after')
    def validate_headers(self):
        if self.bounds and not self.header_rows:
            raise ValueError('a bounded worksheet requires authorized header_rows')
        if tuple(sorted(set(self.header_rows))) != self.header_rows:
            raise ValueError('header_rows must be strictly increasing')
        return self


class NumericRange(_Contract):
    minimum: Decimal = Field(max_digits=40, decimal_places=20)
    maximum: Decimal = Field(max_digits=40, decimal_places=20)

    @model_validator(mode='after')
    def ordered(self):
        if self.minimum > self.maximum:
            raise ValueError('minimum must not exceed maximum')
        return self


class AuditChecks(_Contract):
    checks: tuple[Check, ...] = Field(default=('missing', 'duplicate'), min_length=1, max_length=6)
    required_fields: tuple[Column, ...] = Field(default=(), max_length=256)
    unique_fields: tuple[Column, ...] = Field(default=(), max_length=256)
    types: dict[Column, Literal['number', 'string', 'boolean', 'date']] = Field(default_factory=dict, max_length=256)
    categories: dict[Column, tuple[Annotated[str, Field(max_length=256)], ...]] = Field(default_factory=dict, max_length=256)
    ranges: dict[Column, NumericRange] = Field(default_factory=dict, max_length=256)
    outlier_fields: tuple[Column, ...] = Field(default=(), max_length=256)

    @model_validator(mode='before')
    @classmethod
    def unpack_json_arrays(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        for field in ('checks', 'required_fields', 'unique_fields', 'outlier_fields'):
            if isinstance(data.get(field), list):
                data[field] = tuple(data[field])
        if isinstance(data.get('categories'), dict):
            data['categories'] = {column: tuple(items) if isinstance(items, list) else items
                                  for column, items in data['categories'].items()}
        return data

    @model_validator(mode='after')
    def validate_fields(self):
        for name, items in (('missing', self.required_fields), ('duplicate', self.unique_fields),
                            ('range', self.ranges), ('outlier', self.outlier_fields),
                            ('type', self.types), ('category', self.categories)):
            if items and (name not in self.checks or len(set(items)) != len(items)):
                raise ValueError('check settings must match one requested check')
        if len(set(self.checks)) != len(self.checks):
            raise ValueError('duplicate check')
        if any(not 1 <= len(items) <= 100 or len(set(items)) != len(items) for items in self.categories.values()):
            raise ValueError('invalid category list')
        return self


def _outcome(status, reason_code, **details):
    return {'status': status, 'reason_code': reason_code, **details}


def _session_owned(owner, session_id):
    from src.session.manager import session_manager
    return (type(owner) is int and owner > 0 and isinstance(session_id, str) and bool(session_id)
            and session_manager.get_session_owner(session_id) == owner)


def _source(owner, file_id):
    from src.session.files import get_account_file
    return get_account_file(file_id, owner, touch=False)


def _file_stamp(entry):
    stat = entry['path'].stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _scope_matches(scope, receipt):
    reviewed = receipt.get('reviewed', {})
    if reviewed.get('sheet') != scope.sheet:
        return False
    if scope.header_rows and tuple(reviewed.get('header_rows', ())) != scope.header_rows:
        return False
    if scope.bounds is None:
        return receipt.get('selection') == 'explicit_sheet_auto_region'
    return (receipt.get('selection') == 'explicit_range'
            and reviewed.get('range') == reader.canonical_bounds(scope.bounds))


def _logical_type(values):
    kinds = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            kinds.add('boolean')
        elif isinstance(value, int):
            kinds.add('integer')
        elif isinstance(value, (float, Decimal)):
            kinds.add('number')
        elif isinstance(value, (date, time, timedelta)):
            kinds.add('temporal')
        elif isinstance(value, str):
            kinds.add('string')
        else:
            kinds.add('unknown')
    if kinds == {'integer', 'number'}:
        return 'number'
    return next(iter(kinds)) if len(kinds) == 1 else 'mixed' if kinds else 'empty'


def prepare_dataset(*, owner, session_id, execution_id, file_id, source_sha256, source_name, source_bytes,
                    scope: WorkbookScope | None = None):
    """Bind verified attachment bytes to a frozen single-table input; never publish the original."""
    if not _session_owned(owner, session_id):
        return _outcome('not_run', 'dataset_source_unavailable')
    if scope is not None and not isinstance(scope, WorkbookScope):
        return _outcome('not_run', 'dataset_scope_not_authorized')
    source = _source(owner, file_id)
    if (not source or not isinstance(source_bytes, bytes) or not source_bytes
            or source.get('filename') != source_name or len(source_bytes) > reader.MAX_BYTES):
        return _outcome('not_run', 'dataset_source_unavailable')
    if source.get('sha256') != source_sha256 or sha256(source_bytes).hexdigest() != source_sha256:
        return _outcome('not_run', 'dataset_source_changed')
    format_ = Path(source_name).suffix.lower().removeprefix('.')
    try:
        source_stamp = _file_stamp(source)
        if format_ == 'csv':
            if scope is not None:
                return _outcome('not_run', 'dataset_csv_does_not_accept_workbook_scope')
            table, receipt = reader.csv_table(source_bytes, source_name, source_sha256)
        elif format_ in {'xlsx', 'xlsm'}:
            table, receipt = reader.workbook_table(source_bytes, source_name, source_sha256, scope)
        else:
            return _outcome('not_run', 'dataset_format_unsupported')
    except reader.AmbiguousTable as exc:
        return _outcome('needs_input', exc.code, clarification=exc.details)
    except (ValueError, OSError, CsvError, BadZipFile, KeyError, IndexError, SyntaxError):
        return _outcome('not_run', 'dataset_parse_rejected')
    from src.figures.dataset_codec import encode_frame
    region_ref = sha256(repr((_SCHEMA, source_sha256, format_, receipt['reviewed'])).encode()).hexdigest()
    try:
        if _file_stamp(source) != source_stamp:
            return _outcome('not_run', 'dataset_source_changed')
        payload = encode_frame(table.to_frame(), table_envelope=table)
        snapshot = persist_prepared_dataset(owner, typed_payload=payload, session_id=session_id,
            parents=[{'kind': 'input_snapshot', 'file_id': file_id, 'sha256': source_sha256}],
            execution_id=execution_id, script_sha256=_PARSER_HASH,
            source={'method': _SCHEMA, 'file_id': file_id, 'sha256': source_sha256,
                    'format': format_, 'region_ref': region_ref, 'scope': receipt, 'file_stamp': source_stamp})
    except (PermissionError, ValueError, OSError, LookupError):
        return _outcome('not_run', 'dataset_snapshot_unavailable')
    return _outcome('complete', 'dataset_prepared', dataset_ref=snapshot.ref, region_ref=region_ref,
                    format=format_, source_ref={'file_id': file_id, 'sha256': source_sha256},
                    columns=[{'name': col, 'position': pos, 'logical_type': _logical_type(row[index] for row in table.values)}
                             for index, (col, pos) in enumerate(zip(table.columns, table.column_numbers))],
                    row_count=len(table.values), scope=receipt)


def audit_dataset(*, owner, session_id, dataset_ref: DatasetRef | dict, checks: AuditChecks | dict | None = None,
                  required_scope: WorkbookScope | None = None, required_execution_id: str | None = None):
    """Run data-quality checks on the exact prepared values; no source parse or publication."""
    if not _session_owned(owner, session_id):
        return _outcome('not_run', 'dataset_reference_unavailable')
    try:
        ref = DatasetRef.model_validate(dataset_ref).model_dump()
        # Authorize against only the authenticated snapshot header before any
        # reads or retention touches of the private typed payload.
        snapshot = load_snapshot_metadata(owner, ref, 'dataset_snapshot')
        meta = snapshot.to_dict()
        source = meta['source']
        if (meta.get('session_id') != session_id or meta.get('asset_type') != 'prepared_dataset'
                or meta.get('script_sha256') != _PARSER_HASH or source.get('method') != _SCHEMA):
            return _outcome('not_run', 'dataset_reference_unavailable')
        if (required_execution_id is not None and meta.get('execution_id') != required_execution_id
                or required_scope is not None and (not isinstance(required_scope, WorkbookScope)
                                                    or not _scope_matches(required_scope, source['scope']))):
            return _outcome('not_run', 'dataset_reference_scope_mismatch')
        original = _source(owner, source['file_id'])
        if (not original or original.get('sha256') != source['sha256']
                or _file_stamp(original) != source.get('file_stamp')):
            return _outcome('not_run', 'dataset_source_changed')
        from citrus_audit.envelope import TableEnvelope
        table = TableEnvelope.from_dict(snapshot.to_frame().attrs['table_envelope'])
        if table.source_sha256 != source['sha256'] or table.coverage.get('truncated'):
            return _outcome('not_run', 'dataset_reference_unavailable')
    except (KeyError, TypeError, ValueError, OSError, LookupError, PermissionError):
        return _outcome('not_run', 'dataset_reference_unavailable')
    try:
        selected = AuditChecks.model_validate(checks if checks is not None else {})
    except ValidationError:
        return _outcome('not_run', 'dataset_checks_invalid')
    fields = (set(selected.required_fields) | set(selected.unique_fields) | set(selected.ranges)
              | set(selected.outlier_fields) | set(selected.types) | set(selected.categories))
    if not fields <= set(table.columns):
        return _outcome('not_run', 'dataset_check_field_unknown')
    from .dataset_audit_rules import evaluate
    try:
        results = evaluate(table, selected)
    except ImportError:
        return _outcome('not_run', 'dataset_validation_engine_unavailable')
    except (ValueError, TypeError, RuntimeError):
        return _outcome('failed', 'dataset_validation_failed')
    complete = all(row['status'] == 'checked' for row in results['checks']) and not results['findings_omitted']
    return _outcome('complete' if complete else 'partial', 'dataset_audit_completed' if complete else 'dataset_audit_partial',
                    dataset_ref=ref, region_ref=source['region_ref'], scope=source['scope'], **results)
