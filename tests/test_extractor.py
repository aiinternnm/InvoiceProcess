"""Extractor tests against an in-process OpenAI-compatible "LM Studio" server.

The real OpenAI SDK (installed: 3.x) drives the real Extractor code; the stub
server emulates LM Studio's /v1/chat/completions so the protocol, message
building, JSON parsing and retry logic are all exercised without a model.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from src.extractor import Extractor, ExtractorError  # noqa: E402
from src.utils import parse_number  # noqa: E402
from tests.helpers import FakeLMStudioServer, build_text_pdf  # noqa: E402

VALID_INVOICE = {
    "invoice_number": "INV-STUB-2026-001",
    "invoice_date": "2026-09-01",
    "vendor_name": "Stub Traders Pvt Ltd",
    "vendor_gstin": "27AAACS5842A1ZD",
    "taxable_value": 10000.0,
    "cgst_amount": 900.0,
    "sgst_amount": 900.0,
    "total_amount": 11800.0,
    "confidence": 0.98,
    "uncertain_fields": [],
    "line_items": [],
}


def _cfg(base_url: str, **over) -> dict:
    cfg = {
        "base_url": base_url,
        "api_key": "lm-studio",
        "model": "qwen-test",
        "max_tokens": 4096,
        "temperature": 0,
        "timeout_seconds": 15,
        "vision_enabled": True,
        "min_pdf_text_chars": 40,
        "max_pdf_pages_as_images": 3,
        "retries": 0,
        "backoff_seconds": 0.01,
    }
    cfg.update(over)
    return cfg


class ExtractorLiveProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _img(self, name="inv.png"):
        p = os.path.join(self.tmp.name, name)
        Image.new("RGB", (800, 1100), (255, 255, 255)).save(p)
        return p

    def _pdf(self, text, name="inv.pdf"):
        return build_text_pdf(text, os.path.join(self.tmp.name, name))

    def test_ping_returns_info(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            info = ex.ping()
            self.assertTrue(info["ok"])
            self.assertEqual(info["model"], "qwen-test")
            self.assertIn("INV-STUB-1", info["reply"] or "")

    def test_text_pdf_extracts(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            fp = self._pdf("INVOICE INV-2026-1001 GSTIN 27AAACS5842A1ZD taxable "
                           "value 10000 CGST 900 SGST 900 grand total 11800 date 01/09/2026")
            res = ex.extract(fp, "application/pdf")
            self.assertEqual(res["data"]["invoice_number"], "INV-STUB-1")
            self.assertEqual(res["meta"]["status"], "ok")
            self.assertIn("PDF TEXT CONTENT:", json.dumps(srv.requests[0]))

    def test_image_sent_as_base64_data_uri(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            img = self._img()
            res = ex.extract(img, "image/png")
            self.assertEqual(res["data"]["invoice_number"], "INV-STUB-1")
            body = srv.requests[0]
            content = body["messages"][0]["content"]
            urls = [p["image_url"]["url"] for p in content
                    if isinstance(p, dict) and p.get("type") == "image_url"]
            self.assertEqual(len(urls), 1)
            self.assertTrue(urls[0].startswith("data:image/png;base64,"))
            self.assertIn("invoice", json.dumps(content).lower())

    def test_mime_fallback_for_unknown_mime(self):
        # Drive sometimes reports application/octet-stream for .tif
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            img = self._img("scan.tif")
            res = ex.extract(img, "application/octet-stream")
            self.assertEqual(res["data"]["invoice_number"], "INV-STUB-1")
            urls = [p["image_url"]["url"] for p in srv.requests[0]["messages"][0]["content"]
                    if isinstance(p, dict) and p.get("type") == "image_url"]
            self.assertTrue(urls[0].startswith("data:image/tiff;base64,"))

    def test_vision_disabled_rejected_for_image(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url, vision_enabled=False))
            with self.assertRaises(ExtractorError):
                ex.extract(self._img(), "image/png")

    def test_unsupported_file_rejected(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            p = os.path.join(self.tmp.name, "data.doc")
            with open(p, "wb") as fh:
                fh.write(b"\xd0\xcf\x11\xe0")
            with self.assertRaises(ExtractorError):
                ex.extract(p, "application/msword")

    def test_fenced_json_parsed(self):
        payload = "```json\n" + json.dumps(VALID_INVOICE) + "\n```"
        with FakeLMStudioServer(payload=payload) as srv:
            ex = Extractor(_cfg(srv.base_url))
            data, meta = ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(data["invoice_number"], "INV-STUB-2026-001")
            self.assertEqual(meta["finish_reason"], "stop")

    def test_trailing_text_after_json_parsed(self):
        payload = json.dumps(VALID_INVOICE) + "\n\n(definitely notes after)"
        with FakeLMStudioServer(payload=payload) as srv:
            ex = Extractor(_cfg(srv.base_url))
            data, _ = ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(data["invoice_number"], "INV-STUB-2026-001")

    def test_non_json_raises_extractor_error(self):
        with FakeLMStudioServer(mode="bad_json") as srv:
            ex = Extractor(_cfg(srv.base_url))
            with self.assertRaises(ExtractorError):
                ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")

    def test_response_format_fallback(self):
        # LM Studio may reject response_format; extractor must retry without it.
        with FakeLMStudioServer(mode="no_response_format") as srv:
            ex = Extractor(_cfg(srv.base_url))
            data, _ = ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(data["invoice_number"], "INV-STUB-1")
            self.assertEqual(len(srv.requests), 2)
            self.assertIn("response_format", srv.requests[0])
            self.assertNotIn("response_format", srv.requests[1])

    def test_retry_then_success(self):
        with FakeLMStudioServer(mode="fail_then_ok", fail_until=2) as srv:
            ex = Extractor(_cfg(srv.base_url, retries=2))
            data, _ = ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(data["invoice_number"], "INV-STUB-1")
            self.assertEqual(len(srv.requests), 3)

    def test_retries_exhausted_raises(self):
        with FakeLMStudioServer(mode="fail_then_ok", fail_until=99) as srv:
            ex = Extractor(_cfg(srv.base_url, retries=1))
            with self.assertRaises(ExtractorError):
                ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(len(srv.requests), 2)

    def test_tokens_recorded_from_sdk_v3_usage(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            _, meta = ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertEqual(meta["input_tokens"], 12)
            self.assertEqual(meta["output_tokens"], 7)


class PdfTextHeuristicTest(unittest.TestCase):
    """The extractor routes text PDFs to the text path and scanned ones to vision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_short_non_meaningful_pdf_routes_to_vision(self):
        from PIL import Image as PILImage
        p = os.path.join(self.tmp.name, "scanned.pdf")
        PILImage.new("RGB", (600, 800), (255, 255, 255)).save(p, "PDF")
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url, min_pdf_text_chars=40))
            res = ex.extract(p, "application/pdf")
            self.assertEqual(res["data"]["invoice_number"], "INV-STUB-1")
            content = srv.requests[0]["messages"][0]["content"]
            self.assertTrue(any(isinstance(x, dict) and x.get("type") == "image_url"
                                for x in content), "expected a rendered page image")


if __name__ == "__main__":
    unittest.main()