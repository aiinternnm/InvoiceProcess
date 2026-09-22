"""End-to-end pipeline tests (real main.py) with the Drive + Qwen layers faked.

This exercises the true flow: Drive list -> size gate -> download -> SHA-256 ->
PRE-QWEN content filter -> extraction -> validation -> fingerprint dedupe ->
Excel append -> ledger -> audit, for a batch of files across several runs.
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
from src.excel_writer import ExcelWriter, ExcelWriterError  # noqa: E402
from src.extractor import ExtractorError  # noqa: E402
from tests.helpers import build_text_pdf, make_cfg, make_workbook  # noqa: E402

logging.getLogger("invoice_pipeline").setLevel(logging.WARNING)

VALID_NO = "INV-2026-1001"
VALID_GST = "27AAACS5842A1ZD"


def _invoice(number: str = VALID_NO, gst: str = VALID_GST,
             date: str = "2026-09-15", total: float = 11800.0, taxable: float = 10000.0,
             confidence: float = 0.98, amounts: bool = True) -> dict:
    raw = {
        "invoice_number": number,
        "invoice_date": date,
        "vendor_name": "Sample Traders Pvt Ltd",
        "vendor_gstin": gst,
        "buyer_name": "Example Retail LLP",
        "taxable_value": taxable if amounts else None,
        "cgst_amount": 900.0 if amounts else None,
        "sgst_amount": 900.0 if amounts else None,
        "total_amount": total if amounts else None,
        "confidence": confidence,
        "uncertain_fields": [],
        "line_items": [{
            "sl_no": 1, "description": "Line 1", "qty": 1, "unit": "Pcs",
            "unit_rate": 10000.0, "taxable_value": taxable if amounts else None,
            "cgst_amount": 900.0 if amounts else None,
            "sgst_amount": 900.0 if amounts else None,
            "line_total": total if amounts else None,
        }] if amounts else [],
    }
    return raw


class FakeExtractor:
    """Mimics Extractor; invoices are keyed by Drive file id."""

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
                if callable(value):
                    return value(file_path)
                return {"data": value, "meta": {
                    "latency_ms": 2, "finish_reason": "stop", "status": "ok",
                    "input_tokens": 10, "output_tokens": 8}}
        raise ExtractorError(f"no behavior registered for {base}")


class FakeDrive:
    def __init__(self, files, content):
        self.files = files
        self.content = content  # id -> source path

    def list_folder(self, folder_id, recursive=True, allowed_extensions=None):
        return list(self.files)

    def download(self, file_meta, dest_dir, max_size_mb=None, verify_md5=False):
        src = self.content[file_meta["id"]]
        name = f"{file_meta['id']}_{os.path.basename(src)}"
        dest = os.path.join(dest_dir, name)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest


class PipelineEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wb = make_workbook(os.path.join(self.tmp.name, "template.xlsx"))
        self.cfg_path = make_cfg(self.tmp.name, self.wb)
        self._old_argv = sys.argv
        self._extractor_patch = mock.patch.object(pipeline, "Extractor", FakeExtractor)
        FakeExtractor.behaviors = {}
        FakeExtractor.calls = []

    def _teardown_patches(self):
        sys.argv = self._old_argv
        if hasattr(self, "_extractor_patch"):
            self._extractor_patch.stop()

    def _run(self, files, content, *extra_args, drive=None, cfg_overrides=None):
        if cfg_overrides:
            self.cfg_path = make_cfg(self.tmp.name, self.wb, **cfg_overrides)
        drv = drive or FakeDrive(files, content)
        with mock.patch.object(pipeline, "DriveClient", lambda cfg: drv):
            self._extractor_patch.start()
            try:
                args = ["main.py", "--config", self.cfg_path] + list(extra_args)
                sys.argv = args
                return pipeline.main()
            finally:
                self._teardown_patches()

    # ---- helpers ----------------------------------------------------------
    def _pdf_file(self, name, text):
        p = os.path.join(self.tmp.name, name)
        build_text_pdf(text, p)
        return p

    def _meta(self, fid, name, size=40 * 1024, mime="application/pdf") -> dict:
        return {"id": fid, "name": name, "mimeType": mime, "size": str(size),
                "md5Checksum": None,
                "owners": [{"displayName": "Tester", "emailAddress": "t@x.com"}],
                "lastModifyingUser": {"displayName": "Tester", "emailAddress": "t@x.com"}}

    def _invoice_pdf_text(self, number=VALID_NO):
        return (f"INVOICE {number} Date 15/09/2026 GSTIN {VALID_GST} "
                f"taxable value 10000.00 CGST 900.00 SGST 900.00 grand total 11800.00 "
                f"amount payable Eleven Thousand Eight Hundred Only")

    def _read_details(self):
        from openpyxl import load_workbook
        wb = load_workbook(self.wb)
        ws = wb["details"]
        rows = []
        for r in range(2, ws.max_row + 1):
            rows.append({ws.cell(row=1, column=c).value: ws.cell(row=r, column=c).value
                         for c in range(1, ws.max_column + 1)})
        return [r for r in rows if any(v is not None for v in r.values())]

    def _read_processed(self):
        from openpyxl import load_workbook
        wb = load_workbook(self.wb)
        ws = wb["_DocParser_ProcessedIDs"]
        return [ws.cell(row=r, column=1).value for r in range(2, ws.max_row + 1)]

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

    # ---- scenarios --------------------------------------------------------
    def test_1_valid_invoice_appends_everything(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["InvoiceNo"], VALID_NO)
        self.assertEqual(rows[0]["TaxableValue"], 10000.0)
        self.assertEqual(rows[0]["TotalValue"], 11800.0)
        self.assertEqual(rows[0]["PartyGST"], VALID_GST)
        self.assertEqual(rows[0]["VendorGST"], VALID_GST)
        self.assertIn("fA", self._read_processed())
        # ledger + audit
        self.assertTrue(self._ledger_records())
        recs = self._audit_records()
        self.assertEqual(recs[0]["extraction_status"], "ok")
        self.assertEqual(recs[0]["filter_status"], "PROCESSED")

    def test_2_batch_of_multiple_invoices(self):
        content, files = {}, []
        for i, fid in enumerate(["fA", "fB", "fC"], start=1):
            content[fid] = self._pdf_file(f"{fid}.pdf", self._invoice_pdf_text(f"INV-2{i}"))
            files.append(self._meta(fid, f"{fid}.pdf"))
            FakeExtractor.behaviors[fid] = _invoice(number=f"INV-2{i}")
        code = self._run(files, content)
        self.assertEqual(code, 0)
        rows = self._read_details()
        self.assertEqual([r["InvoiceNo"] for r in rows], ["INV-21", "INV-22", "INV-23"])
        # every invoice has its own line item row
        from openpyxl import load_workbook
        ws = load_workbook(self.wb)["Details_LineItems"]
        self.assertEqual(ws.max_row, 4)

    def test_3_bad_file_fails_but_batch_continues(self):
        content = {
            "fA": self._pdf_file("a.pdf", self._invoice_pdf_text()),
            "fB": self._pdf_file("b.pdf", self._invoice_pdf_text("INV-B")),
        }
        files = [self._meta("fA", "a.pdf"), self._meta("fB", "b.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice(), "fB": lambda p: (_ for _ in ()).throw(
            ExtractorError("model returned non-JSON"))}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 1)
        recs = self._audit_records()
        statuses = sorted(r["extraction_status"] for r in recs)
        self.assertEqual(statuses, ["failed", "ok"])

    def test_4_idempotency_run1_run2_run3(self):
        content, files = {}, []
        for i, fid in enumerate(["fA", "fB", "fC"], start=1):
            content[fid] = self._pdf_file(f"{fid}.pdf", self._invoice_pdf_text(f"INV-3{i}"))
            files.append(self._meta(fid, f"{fid}.pdf"))
            FakeExtractor.behaviors[fid] = _invoice(number=f"INV-3{i}")

        # RUN 1: three invoices -> three rows
        self.assertEqual(self._run(files, content), 0)
        self.assertEqual(len(self._read_details()), 3)

        # RUN 2: same folder -> zero new rows
        self.assertEqual(self._run(files, content), 0)
        self.assertEqual(len(self._read_details()), 3)

        # RUN 3: same three + two brand new
        for fid in ("fD", "fE"):
            content[fid] = self._pdf_file(f"{fid}.pdf", self._invoice_pdf_text(f"INV-3{fid[-1]}"))
            files.append(self._meta(fid, f"{fid}.pdf"))
            FakeExtractor.behaviors[fid] = _invoice(number=f"INV-3{fid[-1]}")
        self.assertEqual(self._run(files, content), 0)
        self.assertEqual(len(self._read_details()), 5)

    def test_5_exact_25kb_file_still_processed(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf", size=25 * 1024 + 1)]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        self.assertEqual(len(self._read_details()), 1)

    def test_6_small_file_skipped_before_qwen(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf", size=10 * 1024)]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        self.assertEqual(len(self._read_details()), 0)
        self.assertEqual(FakeExtractor.calls, [])
        recs = self._audit_records()
        self.assertEqual(recs[0]["filter_status"], "SKIPPED_SMALL_FILE")
        self.assertEqual(recs[0]["qwen_status"], "not_run")

    def test_7_logo_file_skipped_at_content_stage(self):
        logo = os.path.join(self.tmp.name, "logo.png")
        img = Image.new("RGB", (1200, 1600), (250, 250, 250))
        ImageDraw.Draw(img).ellipse([1030, 60, 1180, 210], fill=(0, 150, 150))
        img.save(logo)
        content = {"fA": logo}
        files = [self._meta("fA", "logo.png", size=300 * 1024, mime="image/png")]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        self.assertEqual(len(self._read_details()), 0)
        self.assertEqual(FakeExtractor.calls, [])
        recs = self._audit_records()
        self.assertEqual(recs[0]["filter_status"], "SKIPPED_LOGO_ONLY")
        self.assertEqual(recs[0]["extraction_status"], "skipped")

    def test_8_duplicate_invoice_fingerprint_skipped(self):
        content = {
            "fA": self._pdf_file("a.pdf", self._invoice_pdf_text()),
            "fB": self._pdf_file("b.pdf", self._invoice_pdf_text("INV-DISTINCT-B")),
        }
        files = [self._meta("fA", "a.pdf"), self._meta("fB", "b.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice(), "fB": _invoice()}  # identical invoice
        code = self._run(files, content)
        self.assertEqual(code, 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 1)  # only first appended
        recs = self._audit_records()
        dup = [r for r in recs if r["duplicate_status"] == "dup_invoice"]
        self.assertEqual(len(dup), 1)
        self.assertEqual([r["extraction_status"] for r in recs], ["ok", "skipped"])

    def test_9_review_and_reject_recorded_but_not_appended(self):
        content = {
            "fA": self._pdf_file("a.pdf", self._invoice_pdf_text("INV-REV")),
            "fB": self._pdf_file("b.pdf", self._invoice_pdf_text("INV-REJ")),
        }
        files = [self._meta("fA", "a.pdf"), self._meta("fB", "b.pdf")]
        # fA -> review (invoice number but no amounts), fB -> reject (nothing)
        FakeExtractor.behaviors = {
            "fA": _invoice(number="INV-REV", amounts=False, total=None, taxable=None),
            "fB": {},
        }
        code = self._run(files, content)
        self.assertEqual(code, 0)
        self.assertEqual(len(self._read_details()), 0)  # nothing appended
        recs = self._audit_records()
        statuses = sorted(r["extraction_status"] for r in recs)
        self.assertEqual(statuses, ["rejected", "review"])
        # recorded so they are never re-queried
        ledger = self._ledger_records()
        statuses = sorted(r["status"] for r in ledger)
        self.assertEqual(statuses, ["skipped_reject", "skipped_review"])

    def test_10_model_offline_exit_2(self):
        class OfflineExtractor(FakeExtractor):
            def ping(self):
                raise ConnectionError("refused")

        with mock.patch.object(pipeline, "Extractor", OfflineExtractor):
            sys.argv = ["main.py", "--config", self.cfg_path]
            code = pipeline.main()
        self.assertEqual(code, 2)
        sys.argv = self._old_argv

    def test_11_missing_service_account_exit_3(self):
        class BrokenDrive:
            def list_folder(self, *a, **k):
                raise FileNotFoundError("Service account JSON not found at 'credentials/service_account.json'")

        drv = BrokenDrive()
        with mock.patch.object(pipeline, "Extractor", FakeExtractor), \
                mock.patch.object(pipeline, "DriveClient", lambda cfg: drv):
            sys.argv = ["main.py", "--config", self.cfg_path]
            code = pipeline.main()
        self.assertEqual(code, 3)
        sys.argv = self._old_argv

    def test_12_workbook_locked_is_graceful_exit_4(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}

        def boom(self):
            raise ExcelWriterError("Could not save workbook (file in use by Excel)")

        with mock.patch.object(ExcelWriter, "save", boom):
            code = self._run(files, content)
        self.assertEqual(code, 4)
        # the workbook on disk was never touched -> still pristine
        self.assertEqual(len(self._read_details()), 0)

    def test_13_dry_run_writes_nothing(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content, "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._read_details()), 0)
        self.assertEqual(self._ledger_records(), [])          # no ledger writes
        self.assertTrue(self._audit_records())                 # audit still captured

    def test_14_already_processed_file_id_skipped_via_workbook_reseed(self):
        # simulate an invoice already sitting in the workbook (from a previous era)
        from openpyxl import load_workbook
        wb = load_workbook(self.wb)
        ws = wb["details"]
        ws["A2"] = "fA"
        ws["D2"] = "LEGACY-1"
        ws["E2"] = "2026-09-15"
        ws["F2"] = "Legacy Vendor"
        ws["H2"] = VALID_GST
        ws["I2"] = 500.0
        wb.save(self.wb)

        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content)
        self.assertEqual(code, 0)
        recs = self._audit_records()
        self.assertEqual(recs[0]["duplicate_status"], "dup_file_id")
        self.assertEqual(len(self._read_details()), 1)  # legacy row untouched

    def test_15_mcp_enabled_pipeline_end_to_end(self):
        # full pipeline behind the MCP Excel gateway (in-process transport)
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}
        code = self._run(files, content, cfg_overrides={
            "mcp": {"enabled": True, "transport": "inproc"}})
        self.assertEqual(code, 0)
        rows = self._read_details()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["InvoiceNo"], VALID_NO)
        self.assertIn("fA", self._read_processed())
        recs = self._audit_records()
        self.assertEqual(recs[0]["extraction_status"], "ok")
        self.assertEqual(recs[0]["filter_status"], "PROCESSED")

    def test_16_mcp_enabled_locked_workbook_exit_4(self):
        content = {"fA": self._pdf_file("a.pdf", self._invoice_pdf_text())}
        files = [self._meta("fA", "a.pdf")]
        FakeExtractor.behaviors = {"fA": _invoice()}

        def boom(self):
            raise ExcelWriterError("Could not save workbook (file in use by Excel)")

        with mock.patch.object(ExcelWriter, "save", boom):
            code = self._run(files, content, cfg_overrides={
                "mcp": {"enabled": True, "transport": "inproc"}})
        self.assertEqual(code, 4)
        self.assertEqual(len(self._read_details()), 0)


if __name__ == "__main__":
    unittest.main()