"""Optional first-significant-digit screening, adapted from HoneyMeta Geng (MIT)."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import math
import re

from .contract import csv_records

BENFORD_ID = "numeric.benford_first_digit"
MIN_N = 110  # Each chi-square expected count is >= 5, including digit 9.
TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?")


def assess(source, data):
    """Return primitive per-column receipts. The host confirms scientific suitability."""
    from scipy.stats import chisquare

    columns = {column: (Counter(), [], []) for column in source.benford_columns}
    for number, row in enumerate(csv_records(data), 1):
        if number == 1:
            continue
        for column, (counts, magnitudes, rows) in columns.items():
            raw = row[column - 1].strip() if len(row) >= column else ""
            if len(raw) > 64 or not TOKEN.fullmatch(raw):
                continue
            try:
                value = Decimal(raw).copy_abs()
            except InvalidOperation:
                continue
            if value:
                counts[value.as_tuple().digits[0]] += 1
                magnitudes.append(value.adjusted() + math.log10(float(value.scaleb(-value.adjusted()))))
                rows.append(number)
    res = []
    for column, (counts, magnitudes, rows) in columns.items():
        n = len(rows)
        span = max(magnitudes) - min(magnitudes) if magnitudes else 0.0
        item = {"source": source.reference, "column": column, "tested": n,
                "order_span": round(span, 6), "state": "not_applicable"}
        if n < MIN_N or span < 2:
            item["reason"] = "insufficient_count" if n < MIN_N else "insufficient_order_span"
        else:
            observed = [counts[d] for d in range(1, 10)]
            expected = [n * math.log10(1 + 1 / d) for d in range(1, 10)]
            stat, p = chisquare(observed, expected)
            item.update(state="executed", statistics={"chi2": float(stat), "p": float(p), "observed": observed},
                        location={"source": source.reference, "scope": "csv_range", "start_row": rows[0],
                                  "end_row": rows[-1], "start_column": column, "end_column": column})
        res.append(item)
    return res


def adjust(items):
    """Conservative Bonferroni adjustment across eligible columns in this scan."""
    executed = [item for item in items if item["state"] == "executed"]
    for item in executed:
        item["statistics"]["adjusted_p"] = min(1.0, item["statistics"]["p"] * len(executed))
    return [{"detector_id": BENFORD_ID, "locations": [item["location"]], "statistics": item["statistics"]}
            for item in executed if item["statistics"]["adjusted_p"] < 0.01]
