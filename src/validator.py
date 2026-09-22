"""Strict validation / normalization of extracted invoice JSON.

Makes sure values are the right shape (numbers are numbers, dates normalized),
evaluates tax arithmetic, computes a deterministic invoice fingerprint for
duplicate detection, and classifies the record into an append / review / reject
decision. Never fabricates data: unknown fields stay None.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .utils import parse_date, parse_number, safe_boolish

log = logging.getLogger("invoice_pipeline.validator")


class ValidationResult:
    def __init__(self, data: Dict[str, Any], decision: str, reasons: List[str],
                 confidence: Optional[float], arithmetic: Optional[Dict[str, Any]],
                 fingerprint: Dict[str, Any]):
        self.data = data
        self.decision = decision          # 'ok' | 'review' | 'reject'
        self.reasons = reasons            # human readable flags
        self.confidence = confidence
        self.arithmetic = arithmetic      # {ok, diff, checked}
        self.fingerprint = fingerprint

    @property
    def is_applicable(self) -> bool:
        return self.decision == "ok"


def validate_extraction(raw: Dict[str, Any], cfg: Dict[str, Any]) -> ValidationResult:
    d: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, (str, int, float, bool)) and v != "" :
            d[k] = v
        elif isinstance(v, list):
            d[k] = v
        elif isinstance(v, dict):
            d[k] = v
        else:
            d[k] = None

    # ---- normalize keys that matter --------------------------------------
    for f in ("invoice_number", "document_type", "original_invoice_ref", "vendor_name",
              "vendor_address", "vendor_email", "vendor_phone", "buyer_name", "buyer_address",
              "place_of_supply", "description_of_services_or_product", "hsn_code",
              "unit", "currency", "irn", "ack_no", "dispatch_through", "eway_bill_no",
              "motor_vehicle_no", "mode_of_payment", "reference_no", "other_reference",
              "remarks", "invoice_category", "suggested_expense_category", "credit_terms",
              "period_of_service"):
        if d.get(f) is None:
            continue
        d[f] = str(d[f]).strip()
        if not d[f] or d[f].lower() in ("null", "none", "n/a", "na", "unknown"):
            d[f] = None

    # dates
    d["invoice_date"] = parse_date(d.get("invoice_date"))
    d["due_date"] = parse_date(d.get("due_date"))
    d["ack_date"] = parse_date(d.get("ack_date"))

    # numbers
    for f in ("taxable_value", "total_amount", "cgst_rate", "cgst_amount",
              "sgst_rate", "sgst_amount", "igst_rate", "igst_amount",
              "cess_rate", "cess_amount", "quantity", "total_quantity", "confidence"):
        d[f] = parse_number(d.get(f))

    # yes/no
    d["payable_under_rcm"] = safe_boolish(d.get("payable_under_rcm"))

    # invoice number + gstin cleanup
    _norm_text = lambda v: re_clean(v) if v else None
    d["invoice_number"] = _norm_text(d.get("invoice_number"))
    d["vendor_gstin"] = _norm_text(d.get("vendor_gstin"))
    d["buyer_gstin"] = _norm_text(d.get("buyer_gstin"))

    # line items normalization
    d["line_items"] = _normalize_line_items(d.get("line_items"))

    # ---- decide -----------------------------------------------------------
    reasons: List[str] = []
    uncertain = raw.get("uncertain_fields") or []
    uncertain = [u for u in uncertain if isinstance(u, str) and u.strip()]
    d["uncertain_fields"] = uncertain
    d["_flagged_fields"] = uncertain

    confidence = parse_number(d.get("confidence"))
    d["confidence"] = confidence
    low_conf = cfg.get("extraction", {}).get("audit_low_confidence_threshold", 0.6)
    if confidence is not None and confidence < low_conf:
        reasons.append(f"Low model confidence: {confidence:.2f}")

    total = d.get("total_amount")
    taxable = d.get("taxable_value")
    cgst = d.get("cgst_amount") or 0.0
    sgst = d.get("sgst_amount") or 0.0
    igst = d.get("igst_amount") or 0.0
    cess = d.get("cess_amount") or 0.0

    arithmetic: Dict[str, Any] = {"checked": False, "ok": None, "diff": None}
    if total is not None and taxable is not None:
        comp = taxable + cgst + sgst + igst + cess
        diff = round(total - comp, 2)
        tol = max(1.0, abs(total) * 0.02)
        arithmetic = {"checked": True, "ok": abs(diff) <= tol, "diff": diff}
        if not arithmetic["ok"]:
            reasons.append(f"Tax arithmetic mismatch: total {total} vs components {round(comp, 2)} (diff {diff})")

    if uncertain:
        reasons.append(f"Uncertain fields: {', '.join(uncertain[:8])}{' (+more)' if len(uncertain) > 8 else ''}")

    invoice_no = d.get("invoice_number")
    require_no = cfg.get("extraction", {}).get("require_invoice_number_to_append", True)

    if not invoice_no:
        decision = "review" if total is not None else "reject"
        reasons.append("No invoice_number extracted")
    elif total is None and taxable is None:
        decision = "review"
        reasons.append("No amounts extracted (total/taxable missing)")
    else:
        decision = "ok"
        if d.get("vendor_gstin") is None:
            reasons.append("Vendor GST missing (maybe fine for non-GST vendors)")

    fingerprint = _fingerprint(d, cfg.get("duplicates", {}).get("amount_rounding", 2))
    return ValidationResult(data=d, decision=decision, reasons=reasons,
                            confidence=confidence, arithmetic=arithmetic,
                            fingerprint=fingerprint)


def re_clean(v: str) -> Optional[str]:
    import re
    s = str(v).strip()
    if not s or s.lower() in ("null", "none", "n/a", "na", "unknown", "not found", "-"):
        return None
    return re.sub(r"\s+", " ", s)


def _normalize_line_items(items) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out = []
    for idx, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            continue
        row: Dict[str, Any] = {}
        for f in ("description", "hsn_code", "unit", "asin"):
            v = it.get(f)
            row[f] = re_clean(v) if isinstance(v, str) else (v if v is not None else None)
        for f in ("qty", "unit_rate", "taxable_value", "cgst_rate", "cgst_amount",
                  "sgst_rate", "sgst_amount", "igst_rate", "igst_amount", "line_total"):
            row[f] = parse_number(it.get(f))
        sl = parse_number(it.get("sl_no"))
        row["sl_no"] = int(sl) if sl is not None else idx
        # line total fallback
        if row["line_total"] is None and row["taxable_value"] is not None:
            comp = row["taxable_value"] + (row["cgst_amount"] or 0.0) + (row["sgst_amount"] or 0.0) + (row["igst_amount"] or 0.0)
            row["line_total"] = round(comp, 2)
        out.append(row)
    return out


def _fingerprint(d: Dict[str, Any], rounding: int) -> Dict[str, Any]:
    invoice_no = (d.get("invoice_number") or "").upper().replace(" ", "")
    gstin = (d.get("vendor_gstin") or "").upper().replace(" ", "")
    date = d.get("invoice_date") or ""
    total = d.get("total_amount")
    if total is not None:
        total_s = f"{round(total, rounding):.{rounding}f}"
    else:
        total_s = ""
    raw = "|".join([invoice_no, gstin, date, total_s])
    return {
        "invoice_number": invoice_no or None,
        "vendor_gstin": gstin or None,
        "invoice_date": date or None,
        "total_amount": round(total, rounding) if total is not None else None,
        "fingerprint": raw,
    }