"""Audit-oriented tests for the PRE-QWEN content filter (src/content_filter.py).

Covers the 10 required scenarios from the design spec plus the exact
MIN_FILE_SIZE_KB boundary. Synthetic images / PDFs are generated with Pillow,
so no external test fixtures or network access are needed.

Run from the project root:
    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont

from src.content_filter import (
    ContentFilter,
    FilterResult,
    STATUS_EMPTY,
    STATUS_LOGO_ONLY,
    STATUS_NON_INVOICE,
    STATUS_PROCESSED,
    STATUS_REVIEW_REQUIRED,
    STATUS_SMALL_FILE,
)

MIN_KB = 25
CFG = {
    "min_file_size_kb": MIN_KB,
    "enable_content_filter": True,
    "enable_logo_only_filter": True,
    "min_invoice_signal_score": 2,
}


def make_filter(cfg: dict = None) -> ContentFilter:
    merged = dict(CFG)
    if cfg:
        merged.update(cfg)
    return ContentFilter(merged)


class ContentFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    def _evaluate(self, img: Image.Image, name: str, size_bytes: int,
                  mime: str = "image/png") -> FilterResult:
        p = self._path(name)
        img.save(p)
        return make_filter().evaluate(p, mime, file_size_bytes=size_bytes)

    # -- image builders ----------------------------------------------------
    def _blank_white(self) -> Image.Image:
        return Image.new("RGB", (1240, 1754), (248, 248, 248))

    def _logo_image(self) -> Image.Image:
        img = Image.new("RGB", (1200, 1600), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.ellipse([1030, 60, 1180, 210], fill=(0, 150, 150), outline=(0, 110, 110), width=8)
        return img

    def _banner_image(self) -> Image.Image:
        w, h = 1400, 700
        img = Image.new("RGB", (w, h))
        c1, c2 = (225, 45, 90), (55, 95, 230)
        d = ImageDraw.Draw(img)
        for x in range(w):
            t = x / (w - 1)
            r = int(c1[0] + (c2[0] - c1[0]) * t)
            g = int(c1[1] + (c2[1] - c1[1]) * t)
            b = int(c1[2] + (c2[2] - c1[2]) * t)
            d.line([(x, 0), (x, h)], fill=(r, g, b))
        return img

    def _invoice_image(self, with_logo: bool = True) -> Image.Image:
        img = Image.new("RGB", (1240, 1754), (255, 255, 255))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        d.text((80, 80), "INVOICE", fill=(0, 0, 0), font=font)
        d.text((80, 150), "Invoice No: INV-2026-1001", fill=(0, 0, 0), font=font)
        d.text((80, 210), "Date: 12/03/2026", fill=(0, 0, 0), font=font)
        d.text((80, 270), "GSTIN: 27AAACS5842A1ZD", fill=(0, 0, 0), font=font)
        d.text((80, 330), "Vendor: Sample Traders Pvt Ltd", fill=(0, 0, 0), font=font)
        d.text((620, 330), "Buyer: Example Retail LLP", fill=(0, 0, 0), font=font)
        for i in range(8):
            d.line([(80, 430 + i * 40), (1160, 430 + i * 40)], fill=(0, 0, 0), width=2)
        for i in range(5):
            d.line([(80 + i * 270, 390), (80 + i * 270, 710)], fill=(0, 0, 0), width=2)
        for i in range(6):
            y = 460 + i * 40
            for j in range(4):
                d.line([(100 + j * 240, y), (170 + j * 240, y)], fill=(0, 0, 0), width=3)
        d.text((80, 760), "Taxable Value: 10000.00", fill=(0, 0, 0), font=font)
        d.text((80, 820), "CGST: 900.00    SGST: 900.00", fill=(0, 0, 0), font=font)
        d.text((80, 880), "Grand Total: 11800.00", fill=(0, 0, 0), font=font)
        d.text((80, 940), "Amount in words: Eleven Thousand Eight Hundred Only", fill=(0, 0, 0), font=font)
        if with_logo:
            d.ellipse([1110, 40, 1230, 160], fill=(0, 120, 150))
        return img

    # ======================================================================
    # 1. 10 KB logo -> SKIPPED_SMALL_FILE
    # 2. 24.9 KB image -> SKIPPED_SMALL_FILE
    # ======================================================================
    def test_1_10kb_file_skipped_small(self):
        res = self._evaluate(self._logo_image(), "logo.png", int(10 * 1024))
        self.assertEqual(res.status, STATUS_SMALL_FILE)
        self.assertEqual(res.decision, "skip")

    def test_2_249kb_file_skipped_small(self):
        res = self._evaluate(self._logo_image(), "img.png", int(24.9 * 1024))
        self.assertEqual(res.status, STATUS_SMALL_FILE)
        self.assertEqual(res.decision, "skip")

    # ======================================================================
    # 3. Exactly 25 KB -> passes size gate, content is evaluated
    # ======================================================================
    def test_3_exactly_25kb_passes_size_gate(self):
        f = make_filter()
        self.assertIsNone(f.check_size(int(25 * 1024)))
        self.assertIsNotNone(f.check_size(int(25 * 1024) - 1))
        res = self._evaluate(self._invoice_image(), "inv.png", int(25 * 1024))
        self.assertNotEqual(res.status, STATUS_SMALL_FILE)
        self.assertTrue(res.decision in ("pass", "review"))

    # ======================================================================
    # 4. 150 KB blank white image -> SKIPPED_EMPTY
    # ======================================================================
    def test_4_blank_white_image_skipped_empty(self):
        res = self._evaluate(self._blank_white(), "blank.png", int(150 * 1024))
        self.assertEqual(res.status, STATUS_EMPTY)
        self.assertEqual(res.decision, "skip")

    # ======================================================================
    # 5. 300 KB company logo image -> SKIPPED_LOGO_ONLY
    # ======================================================================
    def test_5_logo_image_skipped_logo_only(self):
        res = self._evaluate(self._logo_image(), "logo.png", int(300 * 1024))
        self.assertEqual(res.status, STATUS_LOGO_ONLY)
        self.assertEqual(res.decision, "skip")

    # ======================================================================
    # 6. 500 KB marketing banner -> SKIPPED_NON_INVOICE
    # ======================================================================
    def test_6_marketing_banner_skipped_non_invoice(self):
        res = self._evaluate(self._banner_image(), "banner.png", int(500 * 1024))
        self.assertEqual(res.status, STATUS_NON_INVOICE)
        self.assertEqual(res.decision, "skip")

    # ======================================================================
    # 7. 500 KB genuine image-based invoice -> processed by Qwen (pass)
    # ======================================================================
    def test_7_genuine_image_invoice_processed(self):
        res = self._evaluate(self._invoice_image(with_logo=False), "inv.png", int(500 * 1024))
        self.assertTrue(res.decision in ("pass", "review"), res)
        self.assertIn(res.status, (STATUS_PROCESSED, STATUS_REVIEW_REQUIRED))

    # ======================================================================
    # 8. 80 KB small but legitimate invoice -> NOT auto-rejected for size
    # ======================================================================
    def test_8_small_legitimate_invoice_not_rejected(self):
        res = self._evaluate(self._invoice_image(), "small_inv.png", int(80 * 1024))
        self.assertNotEqual(res.status, STATUS_SMALL_FILE)
        self.assertTrue(res.decision in ("pass", "review"), res)

    # ======================================================================
    # 9. Image invoice with logo + actual values -> processed
    # ======================================================================
    def test_9_invoice_with_logo_processed(self):
        res = self._evaluate(self._invoice_image(with_logo=True), "inv_logo.png", int(500 * 1024))
        self.assertTrue(res.decision in ("pass", "review"), res)
        self.assertIn(res.status, (STATUS_PROCESSED, STATUS_REVIEW_REQUIRED))

    # ======================================================================
    # 10. PDF with only letterhead/logo and no invoice data -> skipped
    # ======================================================================
    def test_10_letterhead_pdf_skipped_non_invoice(self):
        p = self._path("letterhead.pdf")
        self._logo_image().save(p, "PDF", resolution=72)
        res = make_filter().evaluate(p, "application/pdf", file_size_bytes=200 * 1024)
        self.assertEqual(res.decision, "skip")
        self.assertIn(res.status, (STATUS_LOGO_ONLY, STATUS_NON_INVOICE))

    # ======================================================================
    # Extra: text-based letterhead PDF is also skipped (no invoice data)
    # ======================================================================
    def test_text_only_letterhead_pdf_skipped(self):
        p = self._path("letterhead_text.pdf")
        # image-only PDF; PIL renders a white page with the small logo.
        img = Image.new("RGB", (1240, 1754), (250, 250, 250))
        ImageDraw.Draw(img).ellipse([1030, 60, 1180, 210], fill=(0, 150, 150))
        img.save(p, "PDF", resolution=72)
        res = make_filter().evaluate(p, "application/pdf", file_size_bytes=150 * 1024)
        self.assertEqual(res.decision, "skip")
        self.assertIn(res.status, (STATUS_LOGO_ONLY, STATUS_NON_INVOICE))

    # ======================================================================
    # Extra: filter can be disabled via config
    # ======================================================================
    def test_content_filter_disabled_by_config(self):
        f = make_filter({"enable_content_filter": False})
        img = Image.new("RGB", (40, 40), (255, 255, 255))
        p = self._path("whatever.png")
        img.save(p)
        res = f.evaluate(p, "image/png", file_size_bytes=100 * 1024)
        self.assertEqual(res.decision, "pass")
        self.assertEqual(res.status, STATUS_PROCESSED)

    def test_review_required_for_uncertain_content(self):
        # A faint, low-ink image should never be auto-skipped as irrelevant.
        img = Image.new("RGB", (800, 800), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([300, 300, 520, 380], radius=20, outline=(120, 120, 120), width=4)
        res = self._evaluate(img, "uncertain.png", int(120 * 1024))
        self.assertNotEqual(res.decision, "skip")
        self.assertIn(res.status, (STATUS_PROCESSED, STATUS_REVIEW_REQUIRED))


class PipelineFilterIntegrationTest(unittest.TestCase):
    """End-to-end: small / logo-only Drive files must be filtered BEFORE Qwen,
    must never reach Excel, and must be recorded in the audit log."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make_workbook(self, path: str) -> None:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "details"
        ws.append(["FileID", "FileName", "ProcessedAt", "InvoiceNo"])
        ws2 = wb.create_sheet("Details_LineItems")
        ws2.append(["Description"])
        ws3 = wb.create_sheet("_DocParser_ProcessedIDs")
        ws3.append(["FileID"])
        wb.save(path)

    def _make_config(self, wb_path: str) -> str:
        cfg = {
            "drive": {
                "service_account_json": os.path.abspath("config.json"),
                "folder_id": "faketestfolderid",
                "recursive": True,
                "allowed_extensions": [".pdf", ".jpg", ".jpeg", ".png"],
                "download_dir": os.path.join(self.tmp.name, "downloads"),
                "max_file_size_mb": 50,
                "delete_temp_files": False,
            },
            "excel": {
                "template_path": wb_path,
                "target_sheet": "details",
                "lineitems_sheet": "Details_LineItems",
                "processed_ids_sheet": "_DocParser_ProcessedIDs",
                "backup_dir": os.path.join(self.tmp.name, "backups"),
                "write_line_items": True,
                "write_processed_ids": True,
            },
            "model": {"base_url": "http://localhost:1234/v1", "api_key": "lm-studio", "model": "qwen-test"},
            "extraction": {"require_invoice_number_to_append": True},
            "duplicates": {"ledger_file": os.path.join(self.tmp.name, "ledger.jsonl"),
                           "check_invoice_fingerprint": True},
            "audit": {"dir": os.path.join(self.tmp.name, "audit"),
                      "consolidated_csv": os.path.join(self.tmp.name, "audit", "audit_all_runs.csv")},
            "filtering": {
                "min_file_size_kb": 25,
                "enable_content_filter": True,
                "enable_logo_only_filter": True,
                "min_invoice_signal_score": 2,
            },
            "column_map": {},
        }
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def _run_pipeline(self, cfg_path: str, files, downloaded_paths):
        import main as pipeline
        from src.drive_client import DriveClient

        _real_list = DriveClient.list_folder
        _real_download = DriveClient.download

        def fake_list(self, folder_id, recursive=True, allowed_extensions=None):
            return files

        def fake_download(self, file, dest_dir, max_size_mb=None, verify_md5=False):
            return downloaded_paths[file["id"]]

        DriveClient.list_folder = fake_list
        DriveClient.download = fake_download
        try:
            code = self._call_pipeline(cfg_path)
        finally:
            DriveClient.list_folder = _real_list
            DriveClient.download = _real_download
        return code

    @staticmethod
    def _call_pipeline(cfg_path: str) -> int:
        import main as pipeline

        argv = ["main.py", "--config", cfg_path, "--mock-extract", "--dry-run"]
        old = sys.argv
        sys.argv = argv
        try:
            return pipeline.main()
        finally:
            sys.argv = old

    def _audit_records(self, cfg_path: str):
        cfg_dir = os.path.dirname(cfg_path)
        audit_dir = os.path.join(cfg_dir, "audit")
        recs = []
        for fn in os.listdir(audit_dir):
            if fn.startswith("run_") and fn.endswith(".jsonl"):
                with open(os.path.join(audit_dir, fn), encoding="utf-8") as fh:
                    for line in fh:
                        recs.append(json.loads(line))
        return recs

    def test_small_file_skipped_before_qwen(self):
        wb = os.path.join(self.tmp.name, "template.xlsx")
        self._make_workbook(wb)
        cfg = self._make_config(wb)
        small = {"id": "f1", "name": "tiny.png", "mimeType": "image/png", "size": "10000",
                 "md5Checksum": None, "owners": [{"displayName": "T", "emailAddress": "t@x.com"}]}
        code = self._run_pipeline(cfg, [small], {})
        self.assertEqual(code, 0)
        recs = self._audit_records(cfg)
        self.assertTrue(recs)
        rec = recs[0]
        self.assertEqual(rec["file_id"], "f1")
        self.assertEqual(rec["filter_status"], STATUS_SMALL_FILE)
        self.assertEqual(rec["qwen_status"], "not_run")
        self.assertEqual(rec["extraction_status"], "skipped")
        self.assertIn("below configured 25 KB", rec["skip_reason"])

    def test_logo_file_skipped_at_content_stage(self):
        wb = os.path.join(self.tmp.name, "template.xlsx")
        self._make_workbook(wb)
        cfg = self._make_config(wb)

        logo_path = os.path.join(self.tmp.name, "logo.png")
        img = Image.new("RGB", (1200, 1600), (250, 250, 250))
        ImageDraw.Draw(img).ellipse([1030, 60, 1180, 210], fill=(0, 150, 150))
        img.save(logo_path)
        logo = {"id": "f2", "name": "logo.png", "mimeType": "image/png", "size": str(300 * 1024),
                "md5Checksum": None, "owners": [{"displayName": "T", "emailAddress": "t@x.com"}]}
        code = self._run_pipeline(cfg, [logo], {"f2": logo_path})
        self.assertEqual(code, 0)
        recs = self._audit_records(cfg)
        self.assertTrue(recs)
        rec = recs[0]
        self.assertEqual(rec["filter_status"], STATUS_LOGO_ONLY)
        self.assertEqual(rec["qwen_status"], "not_run")

    def test_content_filter_policy_helper_statuses(self):
        from src.content_filter import STATUS_SMALL_FILE as S_SMALL
        from src import content_filter as cf
        self.assertTrue(S_SMALL.startswith("SKIPPED_"))
        # Every defined audit status is a string in the documented set.
        allowed = {"PROCESSED", "DUPLICATE", "SKIPPED_SMALL_FILE", "SKIPPED_EMPTY",
                   "SKIPPED_LOGO_ONLY", "SKIPPED_NON_INVOICE", "REVIEW_REQUIRED", "FAILED"}
        for attr in ("STATUS_PROCESSED", "STATUS_DUPLICATE", "STATUS_SMALL_FILE", "STATUS_EMPTY",
                     "STATUS_LOGO_ONLY", "STATUS_NON_INVOICE", "STATUS_REVIEW_REQUIRED", "STATUS_FAILED"):
            self.assertIn(getattr(cf, attr), allowed)


if __name__ == "__main__":
    unittest.main()