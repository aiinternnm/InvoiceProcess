"""Map extracted/audit data onto the workbook's existing column headers.

The mapping table lives in config.json (column_map). Mapping is executed only
for columns that actually exist in the header row of the target sheet, so the
template's column names are never changed or reordered.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List

log = logging.getLogger("invoice_pipeline.mapper")

# Internal/audit columns auto-filled regardless of what field the model returned.
_INTERNAL_FIELDS = {
    "_FileID": lambda ctx: ctx["file_id"],
    "_FileName": lambda ctx: ctx["file_name"],
    "_ProcessedAt": lambda ctx: ctx["processed_at"],
    "_Hyperlink": lambda ctx: ctx.get("hyperlink"),
    "_Confidence": lambda ctx: ctx.get("confidence"),
    "_ParseStatus": lambda ctx: ctx["parse_status"],
    "_SenderName": lambda ctx: ctx.get("owner_name"),
    "_SenderEmail": lambda ctx: ctx.get("owner_email"),
    "_ContentHash": lambda ctx: ctx["content_hash"],
    "_DupWarning": lambda ctx: ctx.get("dup_warning"),
    "_TaxArithmeticOk": lambda ctx: ctx.get("arith_ok"),
    "_TaxArithmeticDiff": lambda ctx: ctx.get("arith_diff"),
    "_VerificationStatus": lambda ctx: ctx.get("verify_status"),
    "_VerificationNote": lambda ctx: ctx.get("verify_note"),
    "_InputTokens": lambda ctx: ctx.get("input_tokens"),
    "_OutputTokens": lambda ctx: ctx.get("output_tokens"),
    "_FinishReason": lambda ctx: ctx.get("finish_reason"),
    "_LatencyMs": lambda ctx: ctx.get("latency_ms"),
    "_SourceType": lambda ctx: "GoogleDrive",
    "_DueDateSource": lambda ctx: "extracted" if ctx.get("due_date") else None,
}

# Data columns we always set (drive / processing metadata), independent of the model.
_META_COLUMNS = {
    "FileID": lambda ctx: ctx["file_id"],
    "FileName": lambda ctx: ctx["file_name"],
    "ProcessedAt": lambda ctx: ctx["processed_at"],
    "Hyperlink": lambda ctx: ctx.get("hyperlink"),
}


class Mapper:
    def __init__(self, headers: List[str], column_map: Dict[str, Any]):
        """headers: ordered header values of row 1 of the target sheet."""
        self.headers = headers
        self.col_index = {name: i for i, name in enumerate(headers) if name}
        self.column_map = column_map

    def available_columns(self) -> List[str]:
        return list(self.col_index)

    def build_row(self, data: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Return {column_name: value} mapped for each target column."""
        row: Dict[str, Any] = {}

        for field, target in self.column_map.items():
            value = data.get(field)
            if value is None:
                continue
            targets = target if isinstance(target, list) else [target]
            for col in targets:
                if col in self.col_index:
                    row[col] = self._normalize_cell(col, value)

        for col, fn in _META_COLUMNS.items():
            if col in self.col_index and row.get(col) is None:
                row[col] = fn(ctx)

        for col, fn in _INTERNAL_FIELDS.items():
            if col in self.col_index:
                val = fn(ctx)
                if val is not None:
                    row[col] = val
        return row

    def build_line_item_rows(self, line_items: List[Dict[str, Any]],
                             headers: List[str], meta: Dict[str, Any]) -> List[List[Any]]:
        idx = {name: i for i, name in enumerate(headers) if name}
        inv_date = meta.get("invoice_date")
        if isinstance(inv_date, str) and len(inv_date) == 10 and inv_date[4] == "-":
            try:
                inv_date = dt.date.fromisoformat(inv_date)
            except ValueError:
                pass
        key_by_col = {
            "_InvoiceNo": meta.get("invoice_number"),
            "_PartyName": meta.get("vendor_name"),
            "_InvoiceDate": inv_date,
            "_FileID": meta.get("file_id"),
        }
        rows = []
        for it in line_items:
            row: List[Any] = [None] * len(headers)
            for col in idx:
                if col in key_by_col:
                    row[idx[col]] = key_by_col[col]
                    continue
                mapped = None
                if col == "Description":
                    mapped = it.get("description")
                elif col == "HSNCode":
                    mapped = it.get("hsn_code")
                elif col == "Qty":
                    mapped = it.get("qty")
                elif col in ("Unit", "UnitRate", "TaxableValue", "CGSTRate", "CGSTAmt",
                             "SGSTRate", "SGSTAmt", "IGSTRate", "IGSTAmt", "LineTotal", "SlNo", "ASIN"):
                    mapped = it.get({ "Unit": "unit", "UnitRate": "unit_rate",
                                      "TaxableValue": "taxable_value", "CGSTRate": "cgst_rate",
                                      "CGSTAmt": "cgst_amount", "SGSTRate": "sgst_rate",
                                      "SGSTAmt": "sgst_amount", "IGSTRate": "igst_rate",
                                      "IGSTAmt": "igst_amount", "LineTotal": "line_total",
                                      "SlNo": "sl_no", "ASIN": "asin"}[col])
                if mapped is not None:
                    row[idx[col]] = mapped
            rows.append(row)
        return rows

    @staticmethod
    def _normalize_cell(column_name: str, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if column_name.lower().endswith("rate") or column_name.lower() == "cgstrate":
                return value
            return value
        return value


def build_context(file_id: str, file_name: str, processed_at: dt.datetime,
                  validator_result, meta: Dict[str, Any], duplicate_note: str | None = None) -> Dict[str, Any]:
    dup = ""
    if duplicate_note:
        dup = duplicate_note
    return {
        "file_id": file_id,
        "file_name": file_name,
        "processed_at": processed_at,
        "hyperlink": f"https://drive.google.com/file/d/{file_id}/view" if file_id else None,
        "content_hash": meta.get("content_hash"),
        "owner_name": meta.get("owner_name"),
        "owner_email": meta.get("owner_email"),
        "parse_status": meta.get("parse_status", ""),
        "confidence": validator_result.confidence if validator_result else None,
        "arith_ok": validator_result.arithmetic.get("ok") if validator_result else None,
        "arith_diff": validator_result.arithmetic.get("diff") if validator_result else None,
        "verify_status": "ok" if validator_result and validator_result.decision == "ok" else (
            "review" if validator_result and validator_result.decision == "review" else "reject"),
        "verify_note": "; ".join(validator_result.reasons) if validator_result and validator_result.reasons else None,
        "input_tokens": meta.get("input_tokens"),
        "output_tokens": meta.get("output_tokens"),
        "finish_reason": meta.get("finish_reason"),
        "latency_ms": meta.get("latency_ms"),
        "dup_warning": dup or None,
        "due_date": validator_result.data.get("due_date") if validator_result else None,
    }