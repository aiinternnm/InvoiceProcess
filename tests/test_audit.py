"""Tests for src/audit.py (JSONL per-run + consolidated CSV)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.audit import AuditLogger, _AUDIT_FIELDS  # noqa: E402


class AuditLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit_dir = os.path.join(self.tmp.name, "audit")
        self.csv_path = os.path.join(self.audit_dir, "audit_all_runs.csv")

    def _logger(self):
        return AuditLogger(self.audit_dir, self.csv_path)

    def test_records_contain_all_audit_fields(self):
        logger = self._logger()
        logger.log({"file_id": "f1", "file_name": "inv.pdf", "filter_status": "PROCESSED"})
        logger.close()
        recs = []
        for fn in os.listdir(self.audit_dir):
            if fn.startswith("run_") and fn.endswith(".jsonl"):
                with open(os.path.join(self.audit_dir, fn), encoding="utf-8") as fh:
                    for line in fh:
                        recs.append(json.loads(line))
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        for field in _AUDIT_FIELDS:
            self.assertIn(field, rec)
        self.assertEqual(rec["file_id"], "f1")
        self.assertEqual(rec["filter_status"], "PROCESSED")
        self.assertTrue(rec["run_id"])
        self.assertTrue(rec["processed_at"])

    def test_consolidated_csv_written_with_header(self):
        logger = self._logger()
        logger.log({"file_id": "f1"})
        logger.log({"file_id": "f2"})
        logger.close()
        self.assertTrue(os.path.exists(self.csv_path))
        with open(self.csv_path, encoding="utf-8") as fh:
            rows = [line.rstrip("\n").split(",") for line in fh if line.strip()]
        self.assertEqual(rows[0], _AUDIT_FIELDS)
        self.assertEqual(len(rows), 3)  # header + 2 records
        self.assertIn("f1", rows[1])

    def test_filter_fields_serialised_for_skipped_files(self):
        logger = self._logger()
        logger.log({"file_id": "f1", "filter_status": "SKIPPED_SMALL_FILE",
                    "skip_reason": "below threshold", "filter_score": 0.0})
        logger.close()
        fn = [x for x in os.listdir(self.audit_dir) if x.startswith("run_")][0]
        with open(os.path.join(self.audit_dir, fn), encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["filter_status"], "SKIPPED_SMALL_FILE")
        self.assertEqual(rec["filter_score"], 0.0)

    def test_non_ascii_content_survives(self):
        logger = self._logger()
        logger.log({"file_id": "f1", "file_name": "राशन_चालान.pdf", "skip_reason": "शून्य"})
        logger.close()
        fn = [x for x in os.listdir(self.audit_dir) if x.startswith("run_")][0]
        with open(os.path.join(self.audit_dir, fn), encoding="utf-8") as fh:
            rec = json.loads(fh.readline())
        self.assertEqual(rec["file_name"], "राशन_चालान.pdf")


if __name__ == "__main__":
    unittest.main()