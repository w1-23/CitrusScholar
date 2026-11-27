"""Execute data-quality checks over a frozen table with source coordinates."""
from __future__ import annotations

from datetime import date, datetime
from fractions import Fraction
import re

from citrus_audit.integrity import _number

FINDING_LIMIT = 200


def _quartile(sorted_values, quarter):
    lower, rem = divmod((len(sorted_values) - 1) * quarter, 4)
    result = sorted_values[lower]
    return result if not rem else result + (sorted_values[lower + 1] - result) * Fraction(rem, 4)


def evaluate(table, checks):
    import pandas as pd
    import pandera.pandas as pa
    from pandera.errors import SchemaError

    cols = dict(zip(table.columns, table.column_numbers))
    columns = {key: [row[n] for row in table.values] for n, key in enumerate(table.columns)}
    findings, receipts = [], []
    omitted = 0

    def record(check, fields, reasons=()):
        nonlocal omitted
        tested = violations = 0
        for field, applicable, predicate in fields:
            if not applicable:
                continue
            # Pandera validates exactly the not-null values intended for this
            # check; the index is the original row or logical record number.
            series = pd.Series(applicable, dtype=object)
            schema = pa.SeriesSchema(nullable=True, checks=pa.Check(
                lambda values: values.map(predicate), ignore_na=False, n_failure_cases=FINDING_LIMIT))
            try:
                schema.validate(series)
                failed = []
            except SchemaError as exc:
                cases = exc.failure_cases
                if not isinstance(cases, pd.DataFrame) or 'index' not in cases:
                    raise RuntimeError('dataset_rule_not_evaluated') from exc
                failed = [row for row, value in applicable.items() if not predicate(value)]
                if not failed or set(cases['index']) - set(failed):
                    raise RuntimeError('dataset_rule_receipt_mismatch') from exc
            tested += len(applicable)
            violations += len(failed)
            for row in failed[: max(0, FINDING_LIMIT - len(findings))]:
                findings.append({'check': check, 'row': int(row), 'column': field,
                                 'column_index': cols[field] if field else None})
        omitted += violations
        receipts.append({'check': check, 'status': ('partial' if tested else 'not_run') if reasons else 'checked',
                         'tested': tested, 'findings': violations, 'limitations': list(reasons)})

    def rows(values):
        return dict(zip(table.row_numbers, values))

    if 'missing' in checks.checks:
        fields = checks.required_fields or table.columns
        record('missing', [(col, rows(columns[col]), lambda value: _number(value)[1] != 'missing') for col in fields])
    if 'duplicate' in checks.checks:
        keys = checks.unique_fields or table.columns
        key_frame = pd.DataFrame({col: columns[col] for col in keys}, index=table.row_numbers, dtype=object)
        duplicate = key_frame.duplicated(keep=False)
        record('duplicate', [(None, duplicate.to_dict(), lambda value: not value)])
    if 'type' in checks.checks or 'category' in checks.checks:
        def observed(col):
            return {row: value for row, value in rows(columns[col]).items() if _number(value)[1] != 'missing'}

        def matches_type(value, expected):
            if expected == 'number':
                return _number(value)[0] is not None
            if expected == 'boolean':
                return type(value) is bool or isinstance(value, str) and value.casefold() in {'true', 'false'}
            if expected == 'date':
                if isinstance(value, (date, datetime)):
                    return True
                if isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
                    try:
                        date.fromisoformat(value)
                        return True
                    except ValueError:
                        pass
                return False
            return isinstance(value, str)

        if 'type' in checks.checks:
            record('type', [(col, observed(col), lambda value, typ=typ: matches_type(value, typ))
                            for col, typ in checks.types.items()],
                   [] if checks.types else [{'code': 'expected_types_required'}])
        if 'category' in checks.checks:
            record('category', [(col, observed(col), lambda value, allowed=frozenset(items):
                                   isinstance(value, str) and value in allowed)
                                for col, items in checks.categories.items()],
                   [] if checks.categories else [{'code': 'allowed_categories_required'}])
    if 'range' in checks.checks:
        fields, reasons = [], []
        for col, bounds in checks.ranges.items():
            lo, hi = Fraction(bounds.minimum), Fraction(bounds.maximum)
            values = {row: value for row, value in rows(columns[col]).items() if _number(value)[1] != 'missing'}
            if len(values) < len(table.row_numbers):
                reasons.append({'column': col, 'code': 'missing_values_not_range_tested'})
            fields.append((col, values, lambda value, low=lo, high=hi:
                (number := _number(value)[0]) is not None and low <= number <= high))
        record('range', fields, reasons or ([] if fields else [{'code': 'range_bounds_required'}]))
    if 'outlier' in checks.checks:
        fields, reasons = [], []
        for col in checks.outlier_fields or table.columns:
            parsed = {row: value for row, raw in rows(columns[col]).items() if (value := _number(raw)[0]) is not None}
            if not parsed and not checks.outlier_fields:
                reasons.append({'column': col, 'code': 'numeric_scope_not_applicable'})
                continue
            if len(parsed) < 8 or len(parsed) * 5 < len(table.row_numbers) * 4:
                reasons.append({'column': col, 'code': 'requires_8_values_and_80_percent_numeric'})
                continue
            ordered = sorted(parsed.values())
            q1, q3 = _quartile(ordered, 1), _quartile(ordered, 3)
            spread = (q3 - q1) * Fraction(3, 2)
            if spread == 0:
                reasons.append({'column': col, 'code': 'zero_iqr'})
                continue
            if len(parsed) < len(table.row_numbers):
                reasons.append({'column': col, 'code': 'non_numeric_values_not_outlier_tested'})
            fields.append((col, parsed, lambda n, low=q1 - spread, high=q3 + spread: low <= n <= high))
        record('outlier', fields, reasons)
    return {'checks': receipts, 'findings': findings, 'findings_total': omitted,
            'findings_omitted': omitted - len(findings), 'validation_engine': 'pandera'}
