"""8-file end-to-end acceptance scenario (the user's canonical test set).

Runs the REAL main.main() three times, exactly as the acceptance script
specifies, against a fake Drive + stubbed extractor (stand-ins for the still-
offline LM Studio and missing service-account key):

  RUN1  > file 1 invoice1.pdf            INV-ACE-1001   -> NEW  | file 2 invoice2.jpg   INV-ACE-1002 -> NEW
        > file 3 logo.png                SKIPPED_LOGO_ONLY        | file 4 banner.jpg SKIPPED_NON_INVOICE  | file 5 tiny.png -> SKIPPED_SMALL_FILE
  RUN2  > the same five files again      -> 0 new rows (idempotent, zero re-extraction)
  RUN3  > all eight files                -> invoice3.pdf (INV-ACE-1003) -> NEW  | invoice1_duplicate.pdf -> dup (same invoice re-uploaded) | blank.jpg -> SKIPPED_EMPTY

Total Excel rows after the three runs: EXACTLY 3. Both transports are verified:
the in-process direct backend and the real MCP gateway (in-proc transport).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

import main as pipeline  # noqa: E402
from src.extractor import ExtractorError  # noqa: E402
from tests.helpers import build_text_pdf, make_cfg, make_workbook  # noqa: E402

logging.getLogger("invoice_pipeline").setLevel(logging.WARNING)

INV_A = "INV-ACE-1001"
INV_B = "INV-ACE-1002"
INV_C = "INV-ACE-1003"
GST = "27AAACS5842A1ZD"


def _invoice(number: str, total: float = 11800.0, taxable: float = 10000.0) -> dict:
    return {
        "invoice_number": number, "invoice_date": "2026-09-15",
        "vendor_name": "Sample Traders Pvt Ltd", "vendor_gstin": GST,
        "buyer_name": "Example Retail LLP",
        "taxable_value": taxable, "cgst_amount": 900.0, "sgst_amount": 900.0,
        "total_amount": total, "confidence": 0.98, "uncertain_fields": [],
        "line_items": [{
            "sl_no": 1, "description": "Line 1", "qty": 1, "unit": "Pcs",
            "unit_rate": taxable, "taxable_value": taxable,
            "cgst_amount": 900.0, "sgst_amount": 900.0, "line_total": total}],
    }


class FakeExtractor:
    behaviors = {}
    calls = []

    def __init__(self, cfg):
        self.cfg = cfg

    def ping(self):
        return {"ok": True, "base_url": self.cfg["base_url"], "model": self.cfg["model"],
                "latency_ms": 1, "reply": "ok"}

    def extract(self, file_path, mime_type):
        FakeExtractor.calls.append(os.path.basename(file_path))
        base = os.path.basename(file_path)
        for key, value in self.behaviors.items():
            if base.startswith(key):
                return {"data": value, "meta": {
                    "latency_ms": 2, "finish_reason": "stop", "status": "ok",
                    "input_tokens": 10, "output_tokens": 8}}
        raise ExtractorError(f"no behavior registered for {base}")


class FakeDrive:
    def __init__(self, files, content):
        self.files = files
        self.content = content

    def list_folder(self, folder_id, recursive=True, allowed_extensions=None):
        return list(self.files)

    def download(self, file_meta, dest_dir, max_size_mb=None, verify_md5=False):
        src = self.content[file_meta["id"]]
        name = f"{file_meta['id']}_{os.path.basename(src)}"
        dest = os.path.join(dest_dir, name)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest


class AcceptanceScenario(unittest.TestCase):
    """Both transports run the exact same three runs and assertions."""

    MCP_TRANSPORT = None  # subclasses set to None (direct) or "inproc" (MCP)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wb = make_workbook(os.path.join(self.tmp.name, "template.xlsx"))
        self._old_argv = sys.argv
        self._extractor_patch = mock.patch.object(pipeline, "Extractor", FakeExtractor)
        FakeExtractor.behaviors = {}
        FakeExtractor.calls = []

    def _teardown_patches(self):
        sys.argv = self._old_argv
        if hasattr(self, "_extractor_patch"):
            self._extractor_patch.stop()

    # ---- file makers ------------------------------------------------------
    def _invoice_pdf_text(self, number):
        return (f"INVOICE {number} Date 15/09/2026 GSTIN {GST} "
                f"taxable value 10000.00 CGST 900.00 SGST 900.00 "
                f"grand total 11800.00 amount payable Eleven Thousand Eight Hundred Only")

    def _texty_invoice_image(self, path):
        img = Image.new("RGB", (1200, 1600), (255, 255, 255))
        d = ImageDraw.Draw(img)
        for y in range(80, 1520, 34):
            x = 90
            while x < 1120:
                d.rectangle([x, y, x + 26, y + 16], fill=(30, 30, 30))
                x += 46
        img.save(path)

    def _logo_image(self, path):
        img = Image.new("RGB", (1200, 1600), (250, 250, 250))
        d = ImageDraw.Draw(img)
        d.ellipse([20, 20, 220, 220], fill=(0, 140, 140))
        img.save(path)

    def _banner_image(self, path):
        img = Image.new("RGB", (1280, 720), (60, 120, 180))
        d = ImageDraw.Draw(img)
        for y in range(0, 720, 90):
            d.rectangle([0, y, 1280, y + 45], fill=(180, 60, 120) if (y // 90) % 2 else (120, 180, 60))
        img.save(path)

    def _blank_image(self, path):
        Image.new("RGB", (800, 1100), (255, 255, 255)).save(path)

    def _meta(self, fid, name, size, mime):
        return {"id": fid, "name": name, "mimeType": mime, "size": str(size),
                "md5Checksum": None,
                "owners": [{"displayName": "Tester", "emailAddress": "t@x.com"}],
                "lastModifyingUser": {"displayName": "Tester", "emailAddress": "t@x.com"}}

    # ---- runs -------------------------------------------------------------
    def _run(self, files, content, run_label):
        drv = FakeDrive(files, content)
        overrides = {}
        if self.MCP_TRANSPORT:
            overrides["mcp"] = {"enabled": True, "transport": self.MCP_TRANSPORT}
        self.cfg_path = make_cfg(self.tmp.name, self.wb, **overrides)
        FakeExtractor.calls = []
        with mock.patch.object(pipeline, "DriveClient", lambda cfg: drv):
            self._extractor_patch.start()
            try:
                sys.argv = ["main.py", "--config", self.cfg_path]
                code = pipeline.main()
            finally:
                self._teardown_patches()
        print(f"[{run_label}] exit={code}  extracted={sorted(FakeExtractor.calls)}")
        return code

    def _read_details(self):
        from openpyxl import load_workbook
        wb = load_workbook(self.wb)
        ws = wb["details"]
        rows = []
        for r in range(2, ws.max_row + 1):
            rows.append({ws.cell(row=1, column=c).value: ws.cell(row=r, column=c).value
                         for c in range(1, ws.max_column + 1)})
        return [r for r in rows if any(v is not None for v in r.values())]

    def _audit_records(self):
        recs = []
        audit_dir = os.path.join(self.tmp.name, "audit")
        for fn in os.listdir(audit_dir):
            if fn.startswith("run_") and fn.endswith(".jsonl"):
                with open(os.path.join(audit_dir, fn), encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            recs.append(json.loads(line))
        return recs

    def _ledger_records(self):
        p = os.path.join(self.tmp.name, "ledger.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    # ---- the canonical scenario ------------------------------------------
    def _build_world(self):
        files, content = [], {}
        for fid, name, mime in [
                ("f1", "invoice1.pdf", "application/pdf"),
                ("f2", "invoice2.jpg", "image/jpeg"),
                ("f3", "logo.png", "image/png"),
                ("f4", "banner.jpg", "image/jpeg"),
                ("f5", "tiny.png", "image/png"),
                ("f6", "blank.jpg", "image/jpeg"),
                ("f7", "invoice1_duplicate.pdf", "application/pdf"),
                ("f8", "invoice3.pdf", "application/pdf")]:
            size = {"f1": 120, "f2": 300, "f3": 260, "f4": 420,
                    "f5": 20, "f6": 310, "f7": 120, "f8": 110}[fid] * 1024
            files.append(self._meta(fid, name, size, mime))

        content["f1"] = self._invoice_pdf_file("invoice1.pdf", INV_A)
        self._texty_invoice_image(os.path.join(self.tmp.name, "invoice2.jpg"))
        content["f2"] = os.path.join(self.tmp.name, "invoice2.jpg")
        self._logo_image(os.path.join(self.tmp.name, "logo.png"))
        content["f3"] = os.path.join(self.tmp.name, "logo.png")
        self._banner_image(os.path.join(self.tmp.name, "banner.jpg"))
        content["f4"] = os.path.join(self.tmp.name, "banner.jpg")
        Image.new("RGB", (1, 1), (0, 0, 0)).save(os.path.join(self.tmp.name, "tiny.png"))
        content["f5"] = os.path.join(self.tmp.name, "tiny.png")
        self._blank_image(os.path.join(self.tmp.name, "blank.jpg"))
        content["f6"] = os.path.join(self.tmp.name, "blank.jpg")
        # f7 is byte-for-byte the SAME file as f1 (re-uploaded/renamed invoice)
        content["f7"] = content["f1"]
        content["f8"] = self._invoice_pdf_file("invoice3.pdf", INV_C)

        FakeExtractor.behaviors = {"f1_invoice1.pdf": _invoice(INV_A),
                                   "f2_invoice2.jpg": _invoice(INV_B, total=5900.0, taxable=5000.0),
                                   "f8_invoice3.pdf": _invoice(INV_C)}
        return files, content

    def _invoice_pdf_file(self, name, number):
        p = os.path.join(self.tmp.name, name)
        return build_text_pdf(self._invoice_pdf_text(number), p)

    def _print_final_table(self):
        recs = {}
        for r in self._audit_records():
            recs.setdefault(r.get("file_id"), []).append(r)
        print("  file | id | filter_status | extraction_status | duplicate_status | excel_row")
        print("  -----|----|---------------|-------------------|-----------------|----------")
        for fid in ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"):
            row = recs.get(fid, [{}])[-1]
            print(f"  {row.get('file_name', fid):5} | {fid} | "
                  f"{row.get('filter_status', '-'):13} | "
                  f"{row.get('extraction_status', '-'):17} | "
                  f"{row.get('duplicate_status', '-'):15} | "
                  f"{row.get('excel_row', '-')}")

    def test_canonical_three_runs(self):
        files, content = self._build_world()

        # RUN 1 -> five files; two invoices, three filtered skips
        self.assertEqual(self._run(files[:5], content, "RUN1"), 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["InvoiceNo"] for r in rows}, {INV_A, INV_B})
        self.assertEqual(sorted(FakeExtractor.calls),
                         ["f1_invoice1.pdf", "f2_invoice2.jpg"])
        statuses = {r["filter_status"] for r in self._audit_records()}
        self.assertEqual(statuses, {"PROCESSED", "SKIPPED_LOGO_ONLY",
                                    "SKIPPED_NON_INVOICE", "SKIPPED_SMALL_FILE"})

        # RUN 2 -> same five files: idempotent, nothing re-extracted
        self.assertEqual(self._run(files[:5], content, "RUN2"), 0)
        self.assertEqual(len(self._read_details()), 2)
        self.assertEqual(FakeExtractor.calls, [])

        # RUN 3 -> all eight: one new invoice, one duplicate, one blank-skip
        self.assertEqual(self._run(files, content, "RUN3"), 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(r["InvoiceNo"] for r in rows),
                         [INV_A, INV_B, INV_C])
        self.assertEqual(FakeExtractor.calls, ["f8_invoice3.pdf"])
        recs = {r["file_id"]: r for r in self._audit_records()}
        self.assertEqual(recs["f7"]["duplicate_status"], "dup_hash")
        self.assertEqual(recs["f7"]["extraction_status"], "skipped")
        self.assertEqual(recs["f6"]["filter_status"], "SKIPPED_EMPTY")
        self.assertEqual(recs["f8"]["duplicate_status"], "new")

        # ledger: exactly 3 processed across all runs
        ledger = self._ledger_records()
        self.assertEqual(len(ledger), 8)
        self.assertEqual(len([l for l in ledger if l.get("status") == "processed"]), 3)

        print(f"\nAcceptance summary (transport={self.MCP_TRANSPORT or 'direct'}): "
              f"total Excel rows after RUN1+RUN2+RUN3 = {len(rows)}")
        self._print_final_table()


class AcceptanceScenarioDirect(AcceptanceScenario):
    MCP_TRANSPORT = None


class AcceptanceScenarioMcp(AcceptanceScenario):
    MCP_TRANSPORT = "inproc"


if __name__ == "__main__":
    unittest.main()