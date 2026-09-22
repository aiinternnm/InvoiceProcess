"""Audit logging: one JSONL record per file + a consolidated CSV for all runs."""
from __future__ import annotations

import csv
import json
import logging
import os
import uuid
from typing import Any, Dict, List

from .utils import ensure_dir, now_iso

log = logging.getLogger("invoice_pipeline.audit")

_AUDIT_FIELDS = [
    "run_id", "processed_at", "file_id", "file_name", "owner_name", "owner_email",
    "mime_type", "file_size_bytes", "content_hash", "drive_md5", "source_folder_id",
    "folder_url", "duplicate_status", "invoice_number", "vendor_gstin", "invoice_date",
    "total_amount", "extraction_status", "qwen_status", "parse_error", "review_flags",
    "confidence", "excel_row", "line_item_count", "latency_ms", "input_tokens",
    "output_tokens", "finish_reason", "warning", "filter_status", "skip_reason",
    "filter_score",
]


class AuditLogger:
    def __init__(self, audit_dir: str, consolidated_csv: str):
        self.audit_dir = audit_dir
        self.consolidated_csv = consolidated_csv
        ensure_dir(audit_dir)
        self.run_id = uuid.uuid4().hex[:12]
        self.started = now_iso()
        self._jsonl_path = os.path.join(audit_dir, f"run_{self.run_id}.jsonl")
        self._fh = open(self._jsonl_path, "w", encoding="utf-8")
        log.info("Audit run %s -> %s", self.run_id, self._jsonl_path)

    def log(self, record: Dict[str, Any]) -> None:
        rec: Dict[str, Any] = {"run_id": self.run_id, "processed_at": now_iso()}
        for f in _AUDIT_FIELDS:
            if f not in ("run_id", "processed_at"):
                rec[f] = record.get(f)
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()
        self._append_consolidated()

    def _append_consolidated(self) -> None:
        if not os.path.exists(self.consolidated_csv):
            with open(self.consolidated_csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(_AUDIT_FIELDS)
        try:
            with open(self.consolidated_csv, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                with open(self._jsonl_path, "r", encoding="utf-8") as jh:
                    for line in jh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        writer.writerow([rec.get(f) for f in _AUDIT_FIELDS])
        except OSError as exc:
            log.warning("Failed to append consolidated audit CSV: %s", exc)

    def summary(self) -> Dict[str, int]:
        s: Dict[str, int] = {}
        try:
            with open(self._jsonl_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    rec = json.loads(line)
                    key = f"extraction_status={rec.get('extraction_status')}"
                    s[key] = s.get(key, 0) + 1
                    key2 = f"duplicate_status={rec.get('duplicate_status')}"
                    s[key2] = s.get(key2, 0) + 1
        except (OSError, json.JSONDecodeError):
            pass
        return s