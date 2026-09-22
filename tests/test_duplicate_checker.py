"""Duplicate-detection tests: the 5 idempotency scenarios + seeds from workbook."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.duplicate_checker import DuplicateChecker  # noqa: E402
from src.excel_writer import ExcelWriter  # noqa: E402
from tests.helpers import make_workbook  # noqa: E402


def _checker(tmp: str) -> DuplicateChecker:
    cfg = {"duplicates": {"ledger_file": os.path.join(tmp, "ledger.jsonl"),
                          "check_invoice_fingerprint": True,
                          "amount_rounding": 2}}
    return DuplicateChecker(cfg)


def _seed(checker, file_id, hash_, inv_no, gst, date, total, status="processed"):
    checker.record({
        "file_id": file_id, "content_hash": hash_ or None,
        "invoice_number": inv_no, "vendor_gstin": gst, "invoice_date": date,
        "total_amount": total,
        "fingerprint": "|".join([str(inv_no or "").upper(), str(gst or "").upper(),
                                 str(date or ""),
                                 f"{float(total):.2f}" if total is not None else ""]),
        "excel_row": None, "status": status,
    })


class DuplicateCheckerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checker = _checker(self.tmp.name)

    # ---- the 5 documented scenarios ---------------------------------------
    def test_case_a_same_drive_file_twice(self):
        _seed(self.checker, "AAA", "h1", "INV-1", "G1", "2026-09-01", 100)
        self.assertTrue(self.checker.is_file_known("AAA", ""))

    def test_case_b_rename_reupload_same_bytes(self):
        _seed(self.checker, "AAA", "h1", "INV-1", "G1", "2026-09-01", 100)
        self.assertTrue(self.checker.is_file_known("NEWID", "h1"))  # same hash

    def test_case_c_rescan_bytes_differ(self):
        _seed(self.checker, "AAA", "h1", "INV-1", "G1", "2026-09-01", 100)
        # same invoice, different bytes -> invoice fingerprint matches
        fp = "INV-1|G1|2026-09-01|100.00"
        rec = self.checker.invoice_match(fp)
        self.assertIsNotNone(rec)

    def test_case_d_two_different_invoices_both_new(self):
        _seed(self.checker, "AAA", "h1", "INV-1", "G1", "2026-09-01", 100)
        self.assertFalse(self.checker.is_file_known("BBB", ""))
        self.assertIsNone(self.checker.invoice_match("INV-2|G2|2026-09-02|200.00"))

    def test_case_e_same_file_in_other_folder(self):
        _seed(self.checker, "AAA", "h1", "INV-1", "G1", "2026-09-01", 100)
        self.assertTrue(self.checker.is_file_known("AAA", "h1"))

    # ---- ledger persistence ----------------------------------------------
    def test_ledger_survives_new_instance(self):
        _seed(self.checker, "AAA", "hash1", "INV-1", "G1", "2026-09-01", 100)
        fresh = _checker(self.tmp.name)
        fresh.load_ledger()
        self.assertTrue(fresh.is_file_known("AAA", ""))
        self.assertTrue(fresh.is_file_known("", "hash1"))

    def test_missing_ledger_is_silent(self):
        c = _checker(os.path.join(self.tmp.name, "nope", "ledger.jsonl"))
        c.load_ledger()
        self.assertFalse(c.is_file_known("x", ""))

    # ---- workbook reseed (source of truth) --------------------------------
    def test_seed_from_workbook(self):
        wb_path = os.path.join(self.tmp.name, "real.xlsx")
        make_workbook(wb_path)
        w = ExcelWriter(wb_path, "details", "Details_LineItems", "_DocParser_ProcessedIDs",
                        os.path.join(self.tmp.name, "backups"))
        w.append_header_mapped_row({"FileID": "OLD1", "InvoiceNo": "INV-OLD",
                                    "InvoiceDate": "2026-09-01", "TotalValue": 500.0,
                                    "PartyGST": "27AAACS5842A1ZD"})
        w.save()
        c = _checker(self.tmp.name)
        c.seed_from_workbook(w.read_template_for_seed())
        self.assertTrue(c.is_file_known("OLD1", ""))
        fp = "INV-OLD|27AAACS5842A1ZD|2026-09-01|500.00"
        self.assertIsNotNone(c.invoice_match(fp))

    def test_skipped_records_block_reprocessing_by_file(self):
        # a record recorded as "skipped" (e.g. reject) must still block reprocessing
        # of the same file via file_id/hash, but is NOT a successful invoice match.
        _seed(self.checker, "R1", "hR1", "INV-R", "G1", "2026-09-01", 100, status="skipped_reject")
        self.assertTrue(self.checker.is_file_known("R1", ""))
        self.assertTrue(self.checker.is_file_known("OTHER", "hR1"))  # same bytes re-upload
        self.assertIsNone(self.checker.invoice_match("INV-R|G1|2026-09-01|100.00"))


if __name__ == "__main__":
    unittest.main()