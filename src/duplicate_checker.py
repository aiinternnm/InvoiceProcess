"""Duplicate detection.

Three keys, as required:
  1. Primary   -> Google Drive File IDs (from ledger + workbook).
  2. Secondary -> SHA-256 content hash (from ledger + workbook).
  3. Tertiary  -> invoice-level fingerprint (InvoiceNo + VendorGST + InvoiceDate + Total).

The workbook itself is treated as the authoritative source of truth: it is
re-seeded at every run so a crash between "workbook saved" and "ledger updated"
can never cause a duplicate append.  The ledger file is a fast cache.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

log = logging.getLogger("invoice_pipeline.dupes")


class DuplicateChecker:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg["duplicates"]
        self.ledger_file = self.cfg["ledger_file"]
        self._file_ids: Set[str] = set()
        self._hashes: Set[str] = set()
        self._fingerprints: Dict[str, Dict[str, Any]] = {}

    # ---- seeding ----------------------------------------------------------
    def load_ledger(self) -> None:
        if not os.path.exists(self.ledger_file):
            return
        try:
            with open(self.ledger_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._absorb(rec)
        except OSError as exc:
            log.warning("Could not read ledger %s: %s", self.ledger_file, exc)

    def seed_from_workbook(self, rows: List[Dict[str, Any]]) -> None:
        """rows = list of dicts with keys file_id, content_hash, fingerprint..."""
        for row in rows:
            rec = {
                "file_id": row.get("file_id"),
                "content_hash": row.get("content_hash"),
                "invoice_number": row.get("invoice_number"),
                "vendor_gstin": row.get("vendor_gstin"),
                "invoice_date": row.get("invoice_date"),
                "total_amount": row.get("total_amount"),
                "fingerprint": row.get("fingerprint"),
                "excel_row": row.get("row"),
                "status": "processed",
            }
            self._absorb(rec)

    def _absorb(self, rec: Dict[str, Any]) -> None:
        if rec.get("file_id"):
            self._file_ids.add(str(rec["file_id"]))
        if rec.get("content_hash"):
            self._hashes.add(str(rec["content_hash"]).lower())
        fp = rec.get("fingerprint")
        if fp:
            self._fingerprints.setdefault(str(fp), rec)

    # ---- lookups ----------------------------------------------------------
    def file_knowledge(self, file_id: str, content_hash: str) -> Dict[str, Any]:
        """Return earliest record that explains why a file is known, or None."""
        cid = str(file_id)
        ch = str(content_hash).lower()
        for rec in self._fingerprints.values():
            if rec.get("file_id") == cid or rec.get("content_hash") == ch:
                return rec
        return None

    def is_file_known(self, file_id: str, content_hash: str) -> bool:
        return str(file_id) in self._file_ids or str(content_hash).lower() in self._hashes

    def invoice_match(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        """Return the existing record when an invoice fingerprint already exists."""
        rec = self._fingerprints.get(fingerprint)
        if rec and rec.get("status") in ("processed", "duplicate"):
            return rec
        return None

    # ---- recording --------------------------------------------------------
    def record(self, rec: Dict[str, Any]) -> None:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        os.makedirs(os.path.dirname(self.ledger_file), exist_ok=True)
        with open(self.ledger_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._absorb(rec)
        log.debug("Ledger += %s", rec)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "file_ids": len(self._file_ids),
            "hashes": len(self._hashes),
            "fingerprints": len(self._fingerprints),
        }