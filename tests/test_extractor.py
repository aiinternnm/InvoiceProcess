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
from src.validator import validate_extraction  # noqa: E402
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

# Canonical-schema XML the extractor must parse into the same shape as JSON.
XML_INVOICE = """<invoice>
  <invoice_number>INV-XML-2026-042</invoice_number>
  <invoice_date>2026-09-02</invoice_date>
  <vendor_name>XML Traders</vendor_name>
  <vendor_gstin>27AAACS5842A1ZD</vendor_gstin>
  <taxable_value>10000</taxable_value>
  <cgst_amount>900</cgst_amount>
  <sgst_amount>900</sgst_amount>
  <total_amount>11800</total_amount>
  <confidence>0.97</confidence>
  <uncertain_fields/>
  <line_items>
    <line_item>
      <sl_no>1</sl_no>
      <description>Item One</description>
      <qty>2</qty>
      <unit>Pcs</unit>
      <taxable_value>5000</taxable_value>
      <cgst_amount>450</cgst_amount>
      <sgst_amount>450</sgst_amount>
      <line_total>5900</line_total>
    </line_item>
  </line_items>
</invoice>"""


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

    # ---- Qwen3.5 thinking-model behaviour (LM Studio) --------------------
    def test_response_format_fallback_with_reasoning_extracts_end_to_end(self):
        # Qwen3.5: response_format rejected AND the model thinks first, yet still
        # emits the final JSON in content. The fallback must succeed through the
        # full extract() path (message build -> call -> parse -> validate).
        with FakeLMStudioServer(mode="no_response_format", reasoning=True) as srv:
            ex = Extractor(_cfg(srv.base_url))
            fp = self._pdf("INVOICE INV-2026-1001 GSTIN 27AAACS5842A1ZD taxable "
                           "10000 CGST 900 SGST 900 grand total 11800")
            res = ex.extract(fp, "application/pdf")
            self.assertEqual(res["data"]["invoice_number"], "INV-STUB-1")
            self.assertEqual(res["meta"]["status"], "ok")
            self.assertEqual(len(srv.requests), 2)
            self.assertIn("response_format", srv.requests[0])
            self.assertNotIn("response_format", srv.requests[1])
            # reasoning_content flowed through and is surfaced as a diagnostic
            self.assertGreater(res["meta"]["reasoning_chars"], 0)

    def test_reasoning_only_content_gives_actionable_error(self):
        # Qwen consumed the whole completion budget on thinking; content empty.
        with FakeLMStudioServer(mode="no_response_format", reasoning_only=True,
                                finish_reason="length") as srv:
            ex = Extractor(_cfg(srv.base_url))
            with self.assertRaises(ExtractorError) as cm:
                ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            msg = str(cm.exception)
            self.assertIn("max_tokens", msg)
            self.assertRegex(msg, r"truncated|reasoning")

    def test_truncated_json_surfaces_finish_reason(self):
        truncated = '{"invoice_number": "INV-1", "total_amount": 1'  # cut mid-string
        with FakeLMStudioServer(payload=truncated, finish_reason="length") as srv:
            ex = Extractor(_cfg(srv.base_url))
            with self.assertRaises(ExtractorError) as cm:
                ex._chat_json([{"role": "user", "content": "hi"}], "x.pdf")
            self.assertIn("truncated", str(cm.exception))
            self.assertIn("max_tokens", str(cm.exception))

    # ---- JSON (primary) -> XML (fallback) ----------------------------------
    def _xml_pdf(self):
        return self._pdf("INVOICE INV-2026-1001 GSTIN 27AAACS5842A1ZD "
                         "taxable 10000 CGST 900 SGST 900 grand total 11800")

    def test_json_success_records_source_format(self):
        with FakeLMStudioServer() as srv:
            ex = Extractor(_cfg(srv.base_url))
            res = ex.extract(self._xml_pdf(), "application/pdf")
            self.assertEqual(res["meta"]["source_format"], "json")
            self.assertIs(res["meta"]["xml_fallback"], False)
            self.assertEqual(len(srv.requests), 1)

    def test_json_parse_failure_triggers_xml_fallback(self):
        # malformed JSON -> exactly ONE controlled XML request -> canonical object
        with FakeLMStudioServer(script=[
            lambda body: "this is definitely not JSON",
            lambda body: XML_INVOICE,
        ]) as srv:
            ex = Extractor(_cfg(srv.base_url))
            res = ex.extract(self._xml_pdf(), "application/pdf")
            self.assertEqual(res["data"]["invoice_number"], "INV-XML-2026-042")
            self.assertEqual(res["data"]["vendor_name"], "XML Traders")
            self.assertEqual(res["meta"]["source_format"], "xml")
            self.assertTrue(res["meta"]["xml_fallback"])
            self.assertIn("non-JSON", res["meta"]["json_error"])
            self.assertEqual(len(srv.requests), 2)
            last = srv.requests[1]["messages"][-1]["content"]
            self.assertIn("<invoice>", last)

    def test_schema_invalid_json_triggers_xml_fallback(self):
        # JSON parses but is reject-level (no anchor fields) -> XML re-request
        with FakeLMStudioServer(script=[
            {"invoice_number": None, "total_amount": None,
             "taxable_value": None, "line_items": []},
            lambda body: XML_INVOICE,
        ]) as srv:
            ex = Extractor(_cfg(srv.base_url))
            res = ex.extract(self._xml_pdf(), "application/pdf")
            self.assertEqual(res["meta"]["source_format"], "xml")
            self.assertEqual(res["data"]["invoice_number"], "INV-XML-2026-042")
            self.assertEqual(len(srv.requests), 2)
            # the XML-derived canonical object passes the SAME downstream validator
            vr = validate_extraction(
                Extractor._parse_xml(XML_INVOICE), {"duplicates": {}, "extraction": {}})
            self.assertEqual(vr.decision, "ok")
            self.assertEqual(vr.data["total_amount"], 11800.0)

    def test_xml_fallback_both_fail_raises(self):
        with FakeLMStudioServer(script=[
            "not json at all",
            lambda body: "this is not xml either <<<",
        ]) as srv:
            ex = Extractor(_cfg(srv.base_url))
            with self.assertRaises(ExtractorError) as cm:
                ex.extract(self._xml_pdf(), "application/pdf")
            self.assertIn("Both JSON and XML", str(cm.exception))

    def test_xxe_guard_blocks_doctype_in_xml_fallback(self):
        evil = ('<!DOCTYPE invoice [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                "<invoice>&xxe;</invoice>")
        with FakeLMStudioServer(script=["not json", lambda body: evil]) as srv:
            ex = Extractor(_cfg(srv.base_url))
            with self.assertRaises(ExtractorError) as cm:
                ex.extract(self._xml_pdf(), "application/pdf")
            self.assertIn("XXE", str(cm.exception))


class XmlParseUnitTest(unittest.TestCase):
    """Deterministic XML -> canonical-object mapping used by the fallback."""

    def test_parse_xml_full_invoice(self):
        data = Extractor._parse_xml(XML_INVOICE)
        self.assertEqual(data["invoice_number"], "INV-XML-2026-042")
        self.assertEqual(data["taxable_value"], "10000")
        self.assertEqual(len(data["line_items"]), 1)
        self.assertEqual(data["line_items"][0]["description"], "Item One")
        self.assertEqual(data["line_items"][0]["sl_no"], "1")
        self.assertEqual(data["uncertain_fields"], [])

    def test_parse_xml_fenced(self):
        data = Extractor._parse_xml("```xml\n" + XML_INVOICE + "\n```")
        self.assertEqual(data["invoice_number"], "INV-XML-2026-042")

    def test_parse_xml_namespace_tolerant(self):
        ns = XML_INVOICE.replace("<invoice>", '<invoice xmlns="urn:x">')
        data = Extractor._parse_xml(ns)
        self.assertEqual(data["invoice_number"], "INV-XML-2026-042")

    def test_parse_xml_uncertain_fields(self):
        data = Extractor._parse_xml(
            "<invoice><invoice_number>I-1</invoice_number>"
            "<uncertain_fields><field>due_date</field><field>total_amount</field>"
            "</uncertain_fields><line_items/></invoice>")
        self.assertEqual(data["uncertain_fields"], ["due_date", "total_amount"])
        self.assertEqual(data["line_items"], [])

    def test_parse_xml_rejects_doctype(self):
        evil = ('<!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                "<invoice>&x;</invoice>")
        with self.assertRaises(ExtractorError) as cm:
            Extractor._parse_xml(evil)
        self.assertIn("XXE", str(cm.exception))

    def test_parse_xml_malformed_raises(self):
        with self.assertRaises(ExtractorError):
            Extractor._parse_xml("<invoice><broken></invoice>")

    def test_parse_xml_empty_content_raises(self):
        with self.assertRaises(ExtractorError):
            Extractor._parse_xml("   ")


class JsonParseRobustnessTest(unittest.TestCase):
    """Qwen3.5 replies may embed reasoning/fences/brace fragments around the JSON."""

    def test_reasoning_pretext_with_brace_fragment_ignored(self):
        raw = "The table starts with a {\"qty\": 1} note, now the real JSON: " + json.dumps(VALID_INVOICE)
        data = Extractor._parse_json(raw)
        self.assertEqual(data["invoice_number"], "INV-STUB-2026-001")
        self.assertEqual(data["total_amount"], 11800.0)

    def test_reasoning_after_json_ignored(self):
        raw = json.dumps(VALID_INVOICE) + " Re-checked: 900 + 900 = 1800, total 11800 ok"
        data = Extractor._parse_json(raw)
        self.assertEqual(data["total_amount"], 11800.0)

    def test_picks_full_invoice_over_smaller_fragments(self):
        raw = "{ \"note\": { \"code\": 1 } } preamble " + json.dumps(VALID_INVOICE) + " tail { \"x\": 2 }"
        data = Extractor._parse_json(raw)
        self.assertEqual(data["invoice_number"], "INV-STUB-2026-001")

    def test_fenced_null_byte_pretty(self):
        raw = "Sure! Here it is:\n```json\n" + json.dumps(VALID_INVOICE, indent=2) + "\n```\nAll done."
        data = Extractor._parse_json(raw)
        self.assertEqual(data["invoice_number"], "INV-STUB-2026-001")

    def test_truncated_json_raises(self):
        with self.assertRaises(ExtractorError):
            Extractor._parse_json('{"invoice_number": "INV-1", "total_amount": 1')

    def test_empty_content_raises(self):
        with self.assertRaises(ExtractorError):
            Extractor._parse_json("   ")


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