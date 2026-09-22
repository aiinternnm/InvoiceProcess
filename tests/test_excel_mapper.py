"""Tests for src/excel_mapper.py: config driven mapping onto real headers."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.excel_mapper import Mapper, build_context  # noqa: E402
from src.validator import validate_extraction  # noqa: E402

HEADERS = ["FileID", "FileName", "ProcessedAt", "InvoiceNo", "InvoiceDate",
           "PartyName", "PartyGST", "VendorGST", "TotalValue", "_Confidence"]

COLUMN_MAP = {
    "file_id": "FileID",
    "file_name": "FileName",
    "processed_at": "ProcessedAt",
    "invoice_number": "InvoiceNo",
    "invoice_date": "InvoiceDate",
    "vendor_name": "PartyName",
    "vendor_gstin": ["PartyGST", "VendorGST"],
    "total_amount": "TotalValue",
    "confidence": "NoSuchColumn",   # must be ignored
}


def _validator_result(**raw_over):
    raw = {
        "invoice_number": "INV-1", "invoice_date": "15/09/2026",
        "vendor_name": "Vendor Ltd", "vendor_gstin": "27AAACS5842A1ZD",
        "total_amount": 11800.0, "confidence": 0.95, "uncertain_fields": [],
        "line_items": [],
    }
    raw.update(raw_over)
    return validate_extraction(raw, {"extraction": {}, "duplicates": {"amount_rounding": 2}})


class MapperTest(unittest.TestCase):
    def test_build_row_maps_only_existing_headers(self):
        m = Mapper(HEADERS, COLUMN_MAP)
        v = _validator_result()
        row = m.build_row(v.data, build_context(
            "FILE1", "inv.pdf", dt.datetime(2026, 9, 21, 10, 0), v,
            {"content_hash": "abc123", "parse_status": "success",
             "owner_name": "O", "owner_email": "o@x.com"}))
        self.assertEqual(row["FileID"], "FILE1")
        self.assertEqual(row["FileName"], "inv.pdf")
        self.assertEqual(row["InvoiceNo"], "INV-1")
        self.assertEqual(row["TotalValue"], 11800.0)
        self.assertNotIn("NoSuchColumn", row)

    def test_dual_guest_both_columns_filled(self):
        m = Mapper(HEADERS, COLUMN_MAP)
        v = _validator_result()
        row = m.build_row(v.data, build_context("f", "n", dt.datetime.now(), v, {"parse_status": "success"}))
        self.assertEqual(row["PartyGST"], "27AAACS5842A1ZD")
        self.assertEqual(row["VendorGST"], "27AAACS5842A1ZD")

    def test_none_values_leave_columns_blank(self):
        m = Mapper(HEADERS, COLUMN_MAP)
        v = _validator_result(total_amount=None)
        row = m.build_row(v.data, build_context("f", "n", dt.datetime.now(), v, {"parse_status": "success"}))
        self.assertNotIn("TotalValue", row)

    def test_internal_audit_columns_autofilled(self):
        m = Mapper(HEADERS, COLUMN_MAP)  # only _Confidence exists in HEADERS
        v = _validator_result()
        row = m.build_row(v.data, build_context("f", "n", dt.datetime.now(), v,
                                                {"content_hash": "H", "parse_status": "success",
                                                 "input_tokens": 5, "output_tokens": 3}))
        self.assertEqual(row["_Confidence"], 0.95)

    def test_line_item_rows(self):
        m = Mapper([], COLUMN_MAP)
        items = [{"sl_no": 1, "description": "Item", "qty": 2, "unit": "Pcs",
                  "unit_rate": 5000.0, "taxable_value": 10000.0,
                  "cgst_amount": 900.0, "sgst_amount": 900.0, "line_total": 11800.0}]
        headers = ["_InvoiceNo", "_PartyName", "_InvoiceDate", "_FileID",
                   "Description", "Qty", "Unit", "UnitRate", "TaxableValue",
                   "CGSTAmt", "SGSTAmt", "LineTotal", "SlNo"]
        rows = m.build_line_item_rows(items, headers, {
            "invoice_number": "INV-1", "vendor_name": "Vendor Ltd",
            "invoice_date": "2026-09-15", "file_id": "FILE1"})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0], "INV-1")
        self.assertEqual(row[1], "Vendor Ltd")
        self.assertIsInstance(row[2], dt.date)  # converted, not a raw string
        self.assertEqual(row[2], dt.date(2026, 9, 15))
        self.assertEqual(row[3], "FILE1")
        self.assertEqual(row[4], "Item")
        self.assertEqual(row[11], 11800.0)


if __name__ == "__main__":
    unittest.main()