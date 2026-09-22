"""Unit tests for src/validator.py (decision logic, arithmetic, fingerprint)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.validator import validate_extraction  # noqa: E402

CFG = {
    "extraction": {"require_invoice_number_to_append": True,
                   "audit_low_confidence_threshold": 0.6},
    "duplicates": {"amount_rounding": 2},
}


def _ok_invoice(**overrides):
    raw = {
        "invoice_number": "INV-2026-001",
        "invoice_date": "15/09/2026",
        "vendor_name": "Vendor Ltd",
        "vendor_gstin": "27AAACS5842A1ZD",
        "taxable_value": 10000.0,
        "cgst_amount": 900.0,
        "sgst_amount": 900.0,
        "total_amount": 11800.0,
        "confidence": 0.98,
        "uncertain_fields": [],
        "line_items": [
            {"sl_no": 1, "description": "Item", "qty": 2, "unit": "Pcs",
             "taxable_value": 10000.0, "cgst_amount": 900.0, "sgst_amount": 900.0,
             "line_total": 5900.0},
        ],
    }
    raw.update(overrides)
    return raw


class ValidatorDecisionTest(unittest.TestCase):
    def test_complete_invoice_is_ok(self):
        r = validate_extraction(_ok_invoice(), CFG)
        self.assertEqual(r.decision, "ok")
        self.assertEqual(r.data["invoice_number"], "INV-2026-001")
        self.assertEqual(r.data["invoice_date"], "2026-09-15")
        self.assertEqual(r.data["total_amount"], 11800.0)
        self.assertTrue(r.is_applicable)

    def test_missing_invoice_number_with_amounts_is_review(self):
        r = validate_extraction(_ok_invoice(invoice_number=None), CFG)
        self.assertEqual(r.decision, "review")
        self.assertTrue(any("invoice_number" in x for x in r.reasons))

    def test_totally_empty_is_reject(self):
        r = validate_extraction({"invoice_number": None, "total_amount": None,
                                 "taxable_value": None}, CFG)
        self.assertEqual(r.decision, "reject")

    def test_no_amounts_is_review(self):
        r = validate_extraction(_ok_invoice(total_amount=None, taxable_value=None), CFG)
        self.assertEqual(r.decision, "review")
        self.assertTrue(any("No amounts" in x for x in r.reasons))

    def test_low_confidence_adds_flag_but_stays_ok(self):
        r = validate_extraction(_ok_invoice(confidence=0.4), CFG)
        self.assertEqual(r.decision, "ok")
        self.assertTrue(any("Low model confidence" in x for x in r.reasons))

    def test_never_fabricates_unknown_fields(self):
        r = validate_extraction(_ok_invoice(), CFG)
        self.assertIsNone(r.data.get("buyer_name"))
        self.assertIsNone(r.data.get("irn"))


class ValidatorArithmeticTest(unittest.TestCase):
    def test_arithmetic_matches(self):
        r = validate_extraction(_ok_invoice(), CFG)
        self.assertTrue(r.arithmetic["checked"])
        self.assertTrue(r.arithmetic["ok"])

    def test_arithmetic_mismatch_flags_review(self):
        raw = _ok_invoice(total_amount=13000.0)
        r = validate_extraction(raw, CFG)
        self.assertFalse(r.arithmetic["ok"])
        self.assertTrue(any("Tax arithmetic mismatch" in x for x in r.reasons))

    def test_rounding_tolerance(self):
        raw = _ok_invoice(total_amount=11800.01)
        r = validate_extraction(raw, CFG)
        self.assertTrue(r.arithmetic["ok"])


class ValidatorFingerprintTest(unittest.TestCase):
    def test_fingerprint_normalizes(self):
        r = validate_extraction(_ok_invoice(), CFG)
        fp = r.fingerprint["fingerprint"]
        self.assertEqual(fp, "INV-2026-001|27AAACS5842A1ZD|2026-09-15|11800.00")

    def test_distinct_keys_differ(self):
        a = validate_extraction(_ok_invoice(invoice_number="INV-1"), CFG)
        b = validate_extraction(_ok_invoice(invoice_number="INV-2"), CFG)
        self.assertNotEqual(a.fingerprint["fingerprint"], b.fingerprint["fingerprint"])

    def test_same_semantics_same_fingerprint(self):
        a = validate_extraction(_ok_invoice(invoice_number=" inv-1 "), CFG)
        b = validate_extraction(_ok_invoice(invoice_number="INV-1"), CFG)
        self.assertEqual(a.fingerprint["fingerprint"], b.fingerprint["fingerprint"])


class ValidatorLineItemsTest(unittest.TestCase):
    def test_line_totals_computed_when_missing(self):
        raw = _ok_invoice()
        raw["line_items"] = [{"sl_no": 1, "description": "Item", "qty": 1,
                              "taxable_value": 1000.0, "cgst_amount": 90.0,
                              "sgst_amount": 90.0}]
        r = validate_extraction(raw, CFG)
        self.assertEqual(r.data["line_items"][0]["line_total"], 1180.0)

    def test_line_items_ignores_non_dicts(self):
        raw = _ok_invoice()
        raw["line_items"] = [None, "x", {"description": "ok"}]
        r = validate_extraction(raw, CFG)
        self.assertEqual(len(r.data["line_items"]), 1)

    def test_gstin_cleaning(self):
        r = validate_extraction(_ok_invoice(vendor_gstin=" 27AAACS5842A1ZD "), CFG)
        self.assertEqual(r.data["vendor_gstin"], "27AAACS5842A1ZD")


if __name__ == "__main__":
    unittest.main()