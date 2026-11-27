"""Synthetic contract, findings and scope tests. No production filesystem."""
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from . import worker
from .benford import BENFORD_ID


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def request(self, items, enable=False):
        sources = []
        for i, (fmt, data, columns) in enumerate(items, 1):
            (self.root / f"source-{i:02d}.{fmt}").write_bytes(data)
            sources.append({"id": f"fixture-{i}", "version": 1, "sha256": hashlib.sha256(data).hexdigest(),
                            "format": fmt, "size_bytes": len(data), "benford_columns": columns})
        payload = {"schema": "citrus.numeric-integrity-request/1", "sources": sources, "enable_benford": enable}
        (self.root / "request.json").write_text(json.dumps(payload), encoding="utf-8")
        return sources

    def test_real_worker_relation_and_independent_benford(self):
        rows = [f"{1.11111 + i / 31211:.7f},{1.11111 + i / 31211:.7f},1.{i:03d}e{i % 4}\n" for i in range(160)]
        data = ("measurement,replicate,measurement2\n" + "".join(rows)).encode()
        sources = self.request([("csv", data, [])])
        off = worker.run(self.root)
        self.assertEqual(off["image_review"], "not_in_scope")
        self.assertEqual(off["optional_detectors"][BENFORD_ID]["status"], "not_requested")
        matches = [finding for finding in off["findings"] if finding["detector_id"] == "paperconan.identical_column"]
        self.assertTrue(matches, off)
        self.assertTrue(any(loc["source"]["sha256"] == sources[0]["sha256"] and loc["scope"] == "csv_range"
                            for item in matches for loc in item["locations"]))
        self.request([("csv", data, [3])], enable=True)
        on = worker.run(self.root)
        self.assertEqual(off["paperconan"], on["paperconan"])
        self.assertEqual(off["findings"], [row for row in on["findings"] if row["detector_id"].startswith("paperconan.")])
        self.assertEqual([row["detector_id"] for row in on["findings"] if row["detector_id"] == BENFORD_ID], [BENFORD_ID])
        evidence = on["optional_detectors"][BENFORD_ID]["columns"][0]
        self.assertEqual(evidence["tested"], 160)
        self.assertEqual(evidence["location"]["start_row"], 2)
        self.assertNotIn("measurement", json.dumps(on))

    def test_workbook_location_and_formula_gap(self):
        from openpyxl import Workbook

        workbook = Workbook()
        a = workbook.active
        a.title = "Sample"
        b = workbook.create_sheet("Replicate")
        for sheet in (a, b):
            sheet.append(["measurement", "copy"])
            for i in range(1, 25):
                x = (i * 17487 % 72901) / 13671
                sheet.append([x, x])
        b["C25"] = "=B25*2"
        path = self.root / "fixture.xlsx"
        workbook.save(path)
        self.request([("xlsx", path.read_bytes(), [])])
        result = worker.run(self.root)
        self.assertEqual(result["status"], "partial", result)
        self.assertTrue(any("formula" in issue["reason"] for issue in result["paperconan"]["coverage"]["limitations"]))
        self.assertTrue(any(loc.get("sheet") == "Sample" and loc.get("start_column") == 1
                            for row in result["findings"] for loc in row["locations"]))

    def test_invalid_hash_and_out_of_scope_fail_before_scan(self):
        data = b"a,b\n1,2\n"
        self.request([("csv", data, [])])
        (self.root / "source-01.csv").write_bytes(data.replace(b"2", b"3"))
        with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
            worker.run(self.root)
        self.request([("png", data, [])])
        with self.assertRaisesRegex(ValueError, "invalid_source_identity"):
            worker.run(self.root)

    def test_narrow_range_is_not_applicable_not_zero_findings(self):
        self.request([("csv", b"x\n" + b"10.13\n12.13\n15.13\n" * 45, [1])], enable=True)
        result = worker.run(self.root)
        column, = result["optional_detectors"][BENFORD_ID]["columns"]
        self.assertEqual(column["reason"], "insufficient_order_span")
        self.assertEqual(column["state"], "not_applicable")
        self.assertNotIn(BENFORD_ID, [item["detector_id"] for item in result["findings"]])
