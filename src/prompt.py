"""Extraction prompt builder.

If config 'extraction.prompt_override_file' points to a readable file,
its contents are used verbatim.  Otherwise a structured default prompt
is built automatically.

The canonical field lists (:data:`SCHEMA_SCALAR_FIELDS` and
:data:`SCHEMA_LINEITEM_FIELDS`) are the single source of truth for the
invoice schema: the JSON schema string, the XML fallback schema, and the
XML parser in :mod:`src.extractor` all reference them so JSON and XML always
map to the same canonical invoice object.
"""
from __future__ import annotations

import os
from typing import Dict

_SYSTEM_PREFIX = (
    "You are an expert Indian GST invoice data-extractor.\n"
    "You must extract the requested fields into STRICT JSON that exactly matches the schema.\n\n"
)

_SCHEMAS_BY_INPUT = {
    "pdf": (
        "Input: PDF invoice document.\n"
        "You will see either the extracted text of the invoice or page images.\n"
    ),
    "image": (
        "Input: image (JPG/PNG/etc.) of an invoice.\n"
        "Use OCR carefully; pay attention to commas, Indian number formatting (1,23,456.78), "
        "and handwritten annotations.\n"
    ),
    "text": "Input: raw text extracted from a document.\n",
}

_RULES = """RULES:
1. Output ONLY a single valid JSON object. No markdown fences, no explanation.
2. Do NOT invent or hallucinate values. If a field is invisible, cropped out, or you are unsure, set it to null.
3. Numbers must be JSON numbers. Remove currency symbols (₹ / Rs / INR) and all commas.
   Accept Indian number grouping (1,23,456.78) → 123456.78. Preserve negative signs for credit notes.
4. Dates must be ISO 8601: YYYY-MM-DD. Convert from any format (e.g. 12/03/2025 or 12-Mar-2025 → 2025-03-12).
   If truly absent → null.
5. "invoice_number" = the seller's tax invoice / credit note number. If truly absent → null.
6. GSTIN: 15-character alphanumeric string. Do NOT guess partial digits.
7. "total_amount" = grand total payable including ALL taxes and cess.
8. "line_items": extract every row from the invoice table. Use null for missing cells in a row.
   "sl_no": item serial number (if printed, else use position 1,2,...).
9. "hsn_code" may appear multiple times across line items. Set the top-level "hsn_code" to
   a comma-separated list of all HSN codes, or a single code if uniform.
10. "confidence": your overall confidence (0.0–1.0).
11. "uncertain_fields": list of field names you are uncertain about. Empty list if fully confident.
12. Check arithmetic: taxable_value + cgst_amount + sgst_amount + igst_amount (+ cess_amount)
    should equal total_amount within ±1.0 (for rounding). If they diverge, still extract the
    printed numbers and note the concern in "uncertain_fields".
13. For credit notes, total_amount and taxable_value may be positive. Do not negate them.
"""

_SCHEMA_JSON = """
JSON SCHEMA (return this structure):
{
  "invoice_number":       "string or null",
  "document_type":        "Invoice | Credit Note | Debit Note | Delivery Challan | Quotation or null",
  "original_invoice_ref": "string (original invoice number for credit/debit notes) or null",
  "invoice_date":         "YYYY-MM-DD or null",
  "due_date":             "YYYY-MM-DD or null",
  "period_of_service":    "string or null",
  "vendor_name":          "seller / supplier name or null",
  "vendor_gstin":         "15-char GSTIN of seller or null",
  "vendor_address":       "string or null",
  "vendor_email":         "string or null",
  "vendor_phone":         "string or null",
  "buyer_name":           "customer / receiver name or null",
  "buyer_gstin":          "15-char GSTIN of buyer or null",
  "buyer_address":        "string or null",
  "place_of_supply":      "State name or State code + name or null",
  "description_of_services_or_product": "string (brief line description) or null",
  "hsn_code":             "string or comma-separated list or null",
  "quantity":             "number or null",
  "unit":                 "string (Pcs, Kg, Ltr, Box, etc.) or null",
  "total_quantity":       "number or null",
  "taxable_value":        "number (before tax) or null",
  "cgst_rate":            "number (percent) or null",
  "cgst_amount":          "number or null",
  "sgst_rate":            "number (percent) or null",
  "sgst_amount":          "number or null",
  "igst_rate":            "number (percent) or null",
  "igst_amount":          "number or null",
  "cess_rate":            "number (percent) or null",
  "cess_amount":          "number or null",
  "total_amount":         "number (grand total incl. tax) or null",
  "currency":             "string (e.g. INR) or null",
  "irn":                  "string or null",
  "ack_no":               "string or null",
  "ack_date":             "YYYY-MM-DD or null",
  "payable_under_rcm":    "YES | NO | null",
  "dispatch_through":     "string or null",
  "eway_bill_no":         "string or null",
  "motor_vehicle_no":     "string or null",
  "mode_of_payment":      "string (Online / Cheque / Cash / NEFT / UPI etc.) or null",
  "reference_no":         "string (bank ref, UPI ref etc.) or null",
  "other_reference":      "string or null",
  "remarks":              "string or null",
  "invoice_category":     "string (e.g. Purchase, Service, Expense) or null",
  "suggested_expense_category": "string (Freight, Office Supplies, Legal, etc.) or null",
  "credit_terms":         "string (e.g. Net 30) or null",
  "confidence":           "number 0.0–1.0 or null",
  "uncertain_fields":     ["list of field names"],
  "line_items": [
    {
      "sl_no":          "number or null",
      "description":    "string or null",
      "hsn_code":       "string or null",
      "qty":            "number or null",
      "unit":           "string or null",
      "unit_rate":      "number or null",
      "taxable_value":  "number or null",
      "cgst_rate":      "number or null",
      "cgst_amount":    "number or null",
      "sgst_rate":      "number or null",
      "sgst_amount":    "number or null",
      "igst_rate":      "number or null",
      "igst_amount":    "number or null",
      "line_total":     "number (line item total incl tax) or null",
      "asin":           "string or null"
    }
  ]
}
"""


# Canonical top-level scalar fields (must mirror _SCHEMA_JSON and validator.py).
SCHEMA_SCALAR_FIELDS = (
    "invoice_number", "document_type", "original_invoice_ref", "invoice_date",
    "due_date", "period_of_service", "vendor_name", "vendor_gstin",
    "vendor_address", "vendor_email", "vendor_phone", "buyer_name", "buyer_gstin",
    "buyer_address", "place_of_supply", "description_of_services_or_product",
    "hsn_code", "quantity", "unit", "total_quantity", "taxable_value",
    "cgst_rate", "cgst_amount", "sgst_rate", "sgst_amount", "igst_rate",
    "igst_amount", "cess_rate", "cess_amount", "total_amount", "currency", "irn",
    "ack_no", "ack_date", "payable_under_rcm", "dispatch_through", "eway_bill_no",
    "motor_vehicle_no", "mode_of_payment", "reference_no", "other_reference",
    "remarks", "invoice_category", "suggested_expense_category", "credit_terms",
    "confidence",
)

# Canonical line-item fields (must mirror _SCHEMA_JSON and validator.py).
SCHEMA_LINEITEM_FIELDS = (
    "sl_no", "description", "hsn_code", "qty", "unit", "unit_rate",
    "taxable_value", "cgst_rate", "cgst_amount", "sgst_rate", "sgst_amount",
    "igst_rate", "igst_amount", "line_total", "asin",
)

_XML_ESCAPE_HINT = (
    "Escape XML special characters in text values: & -> &amp;, < -> &lt;, "
    "> -> &gt;. Use <field></field> (an empty element) for absent/null values. "
    "No CDATA, no DOCTYPE, no comments, no markdown fences.\n"
)


def build_extraction_prompt_xml(cfg: Dict, mime_type: str) -> str:
    """Instruction block for the Qwen JSON -> XML fallback request.

    Asks Qwen to re-answer the SAME invoice with a strict XML representation of
    the same canonical schema. The invoice/input itself travels in the original
    message (text or image); this text is appended as a follow-up user turn.
    """
    template_lines = ["<invoice>"]
    for tag in SCHEMA_SCALAR_FIELDS:
        template_lines.append(f"  <{tag}></{tag}>")
    template_lines.append("  <uncertain_fields>")
    template_lines.append("    <field>field_name_one</field>")
    template_lines.append("  </uncertain_fields>")
    template_lines.append("  <line_items>")
    template_lines.append("    <line_item>")
    for tag in SCHEMA_LINEITEM_FIELDS:
        template_lines.append(f"      <{tag}></{tag}>")
    template_lines.append("    </line_item>")
    template_lines.append("  </line_items>")
    template_lines.append("</invoice>")
    xml_schema = "\n".join(template_lines)

    return (
        "Your previous reply did not come back as usable structured JSON, so "
        "answer the SAME invoice input one more time, but this time output ONLY "
        "a single XML document that exactly matches this structure:\n\n"
        + xml_schema
        + "\n\nRULES:\n"
        "1. One <invoice> root with exactly these tag names. Repeat <line_item> "
        "for every line-item row; omit nothing.\n"
        "2. Text inside a tag is the raw value: numbers as plain numbers (no "
        "currency symbols, no thousands separators, e.g. 14750 or 11800.00), "
        "dates as YYYY-MM-DD.\n"
        "3. <uncertain_fields> holds the names of fields you are unsure about; "
        "empty element if none.\n"
        "4. Do NOT invent values. If a field is genuinely absent/unreadable use "
        "an empty element.\n"
        "5. The value semantics are identical to the JSON schema you were given:\n"
        "   total_amount is the grand total incl. all taxes; taxable_value is "
        "   pre-tax; keep negative signs for credit adjusts; preserve every "
        "   line-item row.\n"
        + _XML_ESCAPE_HINT
        + "Output nothing but the XML document."
    )


def build_extraction_prompt(cfg: Dict, mime_type: str) -> str:
    override = cfg.get("extraction", {}).get("prompt_override_file", "")
    if override and os.path.isfile(override):
        with open(override, "r", encoding="utf-8") as fh:
            return fh.read()
    kind = "text"
    mt = (mime_type or "").lower()
    if mt.startswith("image/"):
        kind = "image"
    elif mt == "application/pdf" or mt.endswith(".pdf"):
        kind = "pdf"
    parts = [_SYSTEM_PREFIX, _SCHEMAS_BY_INPUT.get(kind, _SCHEMAS_BY_INPUT["text"]), _RULES, _SCHEMA_JSON]
    return "\n".join(parts)