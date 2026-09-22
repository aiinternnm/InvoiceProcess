"""Unit tests for src/utils.py (hashing, date/number parsing, ids)."""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils import (  # noqa: E402
    parse_date,
    parse_drive_id,
    parse_number,
    safe_boolish,
    sha256_file,
)


class ParseNumberTest(unittest.TestCase):
    def test_floats_and_ints(self):
        self.assertEqual(parse_number(1000), 1000.0)
        self.assertAlmostEqual(parse_number(99.999), 99.999)
        self.assertEqual(parse_number("0"), 0.0)

    def test_indian_locale_formats(self):
        self.assertEqual(parse_number("1,23,456.78"), 123456.78)
        self.assertEqual(parse_number("1,00,000"), 100000.0)
        self.assertEqual(parse_number("₹10,500"), 10500.0)
        self.assertEqual(parse_number("Rs 500"), 500.0)
        self.assertEqual(parse_number("INR 250.50"), 250.5)

    def test_negative_numbers(self):
        self.assertEqual(parse_number("-500"), -500.0)
        self.assertEqual(parse_number("Total: -1,234.50"), -1234.5)

    def test_invoice_number_never_misread_as_negative(self):
        # regression: "INV-100" used to parse as -100.0
        self.assertIsNone(parse_number("INV-100"))
        self.assertIsNone(parse_number("INV-2026-1001"))
        self.assertEqual(parse_number("100"), 100.0)

    def test_bad_values(self):
        self.assertIsNone(parse_number(None))
        self.assertIsNone(parse_number(""))
        self.assertIsNone(parse_number("abc"))
        self.assertIsNone(parse_number(True))  # booleans are not amounts
        self.assertIsNone(parse_number("n/a"))

    def test_embedded_number(self):
        self.assertEqual(parse_number("Sub Total 5,000.00 only"), 5000.0)

    def test_rounding(self):
        self.assertEqual(parse_number("0.123456"), 0.1235)


class ParseDateTest(unittest.TestCase):
    def test_iso(self):
        self.assertEqual(parse_date("2026-09-15"), "2026-09-15")
        self.assertEqual(parse_date("2026/09/15"), "2026-09-15")

    def test_dmy_formats(self):
        for s in ("15/09/2026", "15-09-2026", "15.09.2026", "15-Sep-2026", "15/9/26"):
            self.assertEqual(parse_date(s), "2026-09-15")

    def test_text_formats(self):
        self.assertEqual(parse_date("15 Sep 2026"), "2026-09-15")
        self.assertEqual(parse_date("September 15, 2026"), "2026-09-15")

    def test_inverted_day_month_heuristic(self):
        # 25/-something must be a day -> treated as day-first even if first token big
        self.assertEqual(parse_date("25/12/2026"), "2026-12-25")

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date("2026-13-40"))

    def test_date_object(self):
        self.assertEqual(parse_date(dt.date(2026, 3, 1)), "2026-03-01")

    def test_weekday_suffix_scrubbed(self):
        self.assertEqual(parse_date("12/03/2026 (Tue)"), "2026-03-12")


class ParseDriveIdTest(unittest.TestCase):
    def test_folder_link(self):
        self.assertEqual(
            parse_drive_id("https://drive.google.com/drive/folders/1jeH4_xIUGG89ozhwbq01NfH52iRPbpxi"),
            "1jeH4_xIUGG89ozhwbq01NfH52iRPbpxi")

    def test_file_link(self):
        self.assertEqual(
            parse_drive_id("https://drive.google.com/file/d/AbC123def_456/view?usp=sharing"),
            "AbC123def_456")

    def test_bare_id(self):
        self.assertEqual(parse_drive_id("1jeH4_xIUGG89ozhwbq01NfH52iRPbpxi"),
                         "1jeH4_xIUGG89ozhwbq01NfH52iRPbpxi")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            parse_drive_id("")
        with self.assertRaises(ValueError):
            parse_drive_id("not a link")


class SafeBoolishTest(unittest.TestCase):
    def test_yes_variants(self):
        for v in ("YES", "Y", "True", "RCM", "1"):
            self.assertEqual(safe_boolish(v), "YES")

    def test_no_variants(self):
        for v in ("NO", "N", "False", "0"):
            self.assertEqual(safe_boolish(v), "NO")

    def test_other(self):
        self.assertIsNone(safe_boolish(None))
        self.assertEqual(safe_boolish("Not Applicable"), "NOT APPLICABLE")


class HashTest(unittest.TestCase):
    def test_sha256_known(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.bin")
            with open(p, "wb") as fh:
                fh.write(b"hello world")
            self.assertEqual(sha256_file(p),
                             "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9")


if __name__ == "__main__":
    unittest.main()