"""End-to-end pipeline runner.

Usage:
    python main.py [--config config.json] [--folder <link-or-id>] [--dry-run]
                   [--mock-extract] [--test-model] [--skip-model-check]

Flow per file:
  Drive discovery -> file-ID dedupe -> size gate (pre-download) ->
  download -> SHA-256 dedupe -> PRE-QWEN content filter (blank / logo-only /
  non-invoice / candidate) -> Qwen extraction -> strict validation ->
  invoice-fingerprint dedupe -> Excel append (details + line items +
  processed IDs) via the configured Excel gateway (in-process direct, or an MCP
  server when mcp.enabled) -> ledger -> audit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

from src.audit import AuditLogger
from src.config import load_config
from src.content_filter import (
    ContentFilter,
    STATUS_EMPTY,
    STATUS_LOGO_ONLY,
    STATUS_NON_INVOICE,
    STATUS_SMALL_FILE,
)
from src.drive_client import DriveClient, DriveError, owner_of
from src.duplicate_checker import DuplicateChecker
from src.excel_backend import open_excel_backend
from src.excel_mapper import Mapper, build_context
from src.excel_writer import ExcelWriterError
from src.extractor import Extractor, ExtractorError
from src.validator import validate_extraction
from src.utils import ensure_dir, now_dt, parse_drive_id, sha256_file

log = logging.getLogger("invoice_pipeline")


def _sample_invoice(product: bool = False) -> Dict[str, Any]:
    """Deterministic mock invoice used by --mock-extract (no model needed)."""
    return {
        "invoice_number": "INV-MOCK-2026-001",
        "invoice_date": "2026-09-15",
        "due_date": "2026-10-15",
        "vendor_name": "Sample Traders Pvt Ltd",
        "vendor_gstin": "27AAACS5842A1ZD",
        "buyer_name": "Example Retail LLP",
        "buyer_gstin": "29AAACB1234F1Z8",
        "place_of_supply": "Karnataka",
        "description_of_services_or_product": "Procurement of sample line 1",
        "hsn_code": "998899",
        "taxable_value": 10000.00,
        "cgst_rate": 9.0,
        "cgst_amount": 900.00,
        "sgst_rate": 9.0,
        "sgst_amount": 900.00,
        "igst_rate": None,
        "igst_amount": None,
        "total_amount": 11800.00,
        "mode_of_payment": "Bank Transfer",
        "document_type": "Invoice",
        "confidence": 0.99,
        "uncertain_fields": [],
        "line_items": [
            {
                "sl_no": 1, "description": "Sample line 1", "hsn_code": "998899",
                "qty": 1, "unit": "Pcs", "unit_rate": 10000.00,
                "taxable_value": 10000.00, "cgst_rate": 9, "cgst_amount": 900,
                "sgst_rate": 9, "sgst_amount": 900, "igst_rate": None, "igst_amount": None,
                "line_total": 11800.00, "asin": None,
            }
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive -> Qwen -> Excel invoice pipeline")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--folder", help="Drive folder link or ID (overrides config.json)")
    parser.add_argument("--dry-run", action="store_true", help="Extract & validate but do not write Excel / ledger.")
    parser.add_argument("--mock-extract", action="store_true", help="Skip the model; inject a mock invoice (tests Excel mapping).")
    parser.add_argument("--test-model", action="store_true", help="Ping the Qwen endpoint and exit.")
    parser.add_argument("--skip-model-check", action="store_true", help="Do not fail fast when model is unreachable.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    audit_dir = cfg["audit"]["dir"]
    ensure_dir(audit_dir)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    drive_cfg, excel_cfg, model_cfg = cfg["drive"], cfg["excel"], cfg["model"]
    if args.folder:
        drive_cfg["folder_id"] = parse_drive_id(args.folder)

    # ---- model check -----------------------------------------------------
    extractor = None
    if args.test_model:
        if args.mock_extract:
            print("--test-model: no model is used when --mock-extract is set; nothing to ping.")
            return 0
        extractor = Extractor(model_cfg)
        try:
            info = extractor.ping()
            print("[OK] model reachable: %s (%s) latency=%dms reply=%r" % (
                info["model"], info["base_url"], info["latency_ms"], info["reply"]))
            return 0
        except Exception as exc:  # noqa: BLE001
            print("[FAIL] Could not reach Qwen endpoint %s: %s" % (model_cfg["base_url"], exc))
            print("  -> start LM Studio (Server) on http://localhost:1234/v1 and load the model,")
            print("     then check model.base_url / model.model in config.json. ngrok is only")
            print("     needed when this machine cannot reach LM Studio directly.")
            return 2
    if not args.mock_extract:
        extractor = Extractor(model_cfg)
        ping_ok = False
        if not args.skip_model_check:
            try:
                info = extractor.ping()
                ping_ok = True
                log.info("Model reachable: %s (%s) latency=%dms reply=%r",
                         info["model"], info["base_url"], info["latency_ms"], info["reply"])
            except Exception as exc:  # noqa: BLE001
                log.error("Could not reach Qwen endpoint %s: %s\n"
                          "  -> fix model.base_url / model in config.json, "
                          "start LM Studio (Server) on http://localhost:1234/v1 "
                          "(ngrok only needed from another machine), or pass "
                          "--skip-model-check / --mock-extract.",
                          model_cfg["base_url"], exc)
        if not ping_ok and not args.skip_model_check:
            return 2

    # ---- Drive ------------------------------------------------------------
    drive = DriveClient(drive_cfg["service_account_json"])
    try:
        files = drive.list_folder(drive_cfg["folder_id"],
                                  recursive=drive_cfg.get("recursive", True),
                                  allowed_extensions=drive_cfg.get("allowed_extensions"))
    except (DriveError, Exception) as exc:  # noqa: BLE001
        log.error("Drive scan failed: %s", exc)
        return 3
    log.info("Found %d supported files in folder %s", len(files), drive_cfg["folder_id"])

    # ---- Excel + duplicates ----------------------------------------------
    backpack = open_excel_backend(cfg, args.config, args.dry_run)
    dupes = DuplicateChecker(cfg)
    dupes.load_ledger()
    dupes.seed_from_workbook(backpack.read_template_for_seed())
    log.info("Duplicate state: %s", dupes.snapshot())

    mapper = Mapper(backpack.headers, cfg["column_map"])

    audit = AuditLogger(cfg["audit"]["dir"], cfg["audit"]["consolidated_csv"])
    content_filter = ContentFilter(cfg.get("filtering", {}))

    counters: Dict[str, int] = {"new": 0, "dup_file_id": 0, "dup_hash": 0, "dup_invoice": 0,
                                "review": 0, "reject": 0, "failed": 0,
                                "skip_small": 0, "skip_empty": 0, "skip_logo": 0,
                                "skip_noninvoice": 0, "review_filter": 0}

    # ---- process loop -----------------------------------------------------
    for file_meta in _sorted(files):
        file_id = file_meta["id"]
        fname = file_meta.get("name", "")
        mime = file_meta.get("mimeType", "")
        owner = owner_of(file_meta)
        base_audit = {
            "file_id": file_id, "file_name": fname, "mime_type": mime,
            "file_size_bytes": file_meta.get("size"),
            "drive_md5": file_meta.get("md5Checksum"),
            "source_folder_id": drive_cfg["folder_id"], "folder_url": drive_cfg.get("folder_url", ""),
            "owner_name": owner["owner_name"], "owner_email": owner["owner_email"],
            "qwen_status": "", "parse_error": "", "review_flags": "",
            "filter_status": "", "skip_reason": "", "filter_score": None,
        }
        try:
            # 1) file-ID dedupe (before download — cheap)
            if dupes.is_file_known(file_id, ""):
                counters["dup_file_id"] += 1
                _audit(audit, base_audit, extraction_status="skipped", duplicate_status="dup_file_id",
                       warning="Drive File ID already processed (ledger/workbook).")
                continue

            # 1b) PRE-QWEN size gate (before download; uses Drive metadata size)
            size_result = content_filter.check_size(base_audit["file_size_bytes"])
            if size_result is not None:
                counters[_counter_for_status(size_result.status)] += 1
                _audit(audit, base_audit, filter_status=size_result.status,
                       skip_reason=size_result.reason, filter_score=size_result.score,
                       extraction_status="skipped", duplicate_status="new",
                       qwen_status="not_run", warning=size_result.reason)
                _mark_seen(backpack, dupes, file_id, "", "skipped_small", args.dry_run)
                continue

            # 2) download + content hash (secondary dedupe)
            log.info("Downloading %s (%s)", fname, file_id)
            local_path = drive.download(file_meta, drive_cfg["download_dir"],
                                        max_size_mb=drive_cfg.get("max_file_size_mb"),
                                        verify_md5=drive_cfg.get("verify_download_md5", False))
            content_hash = sha256_file(local_path)
            if dupes.is_file_known("", content_hash):
                counters["dup_hash"] += 1
                _audit(audit, base_audit, content_hash=content_hash,
                       extraction_status="skipped", duplicate_status="dup_hash",
                       warning="SHA-256 content hash matches an already processed file.")
                _mark_seen(backpack, dupes, file_id, content_hash, "dup_hash", args.dry_run)
                continue
            base_audit["content_hash"] = content_hash

            # 3) PRE-QWEN content filter (blank / logo-only / non-invoice / candidate)
            pre = content_filter.evaluate(local_path, mime,
                                          file_size_bytes=base_audit["file_size_bytes"])
            if pre.decision == "skip":
                counters[_counter_for_status(pre.status)] += 1
                _audit(audit, base_audit, content_hash=content_hash,
                       filter_status=pre.status, skip_reason=pre.reason,
                       filter_score=pre.score, extraction_status="skipped",
                       duplicate_status="new", qwen_status="not_run", warning=pre.reason)
                _mark_seen(backpack, dupes, file_id, content_hash,
                           _ledger_status_for_filter(pre.status), args.dry_run)
                continue
            # review / pass -> proceed to Qwen, but keep the audit trail visible.
            base_audit["filter_status"] = pre.status
            base_audit["skip_reason"] = pre.reason if pre.decision == "review" else ""
            base_audit["filter_score"] = pre.score
            if pre.decision == "review":
                counters["review_filter"] += 1
                log.info("PRE-FILTER REVIEW %s: %s", fname, pre.reason)

            # 4) extraction
            if args.mock_extract:
                raw, meta = _sample_invoice(), {"latency_ms": 0, "finish_reason": "mock",
                                                "input_tokens": 0, "output_tokens": 0, "status": "ok"}
            else:
                res = extractor.extract(local_path, mime)
                raw, meta = res["data"], res["meta"]
            base_audit["qwen_status"] = meta.get("status", "ok")

            # 5) validation
            vresult = validate_extraction(raw, cfg)

            # 6) invoice-fingerprint dedupe (tertiary)
            dup_rec = None
            if cfg["duplicates"].get("check_invoice_fingerprint", True) and vresult.fingerprint.get("fingerprint"):
                dup_rec = dupes.invoice_match(vresult.fingerprint["fingerprint"])
            if dup_rec:
                counters["dup_invoice"] += 1
                _audit(audit, base_audit, content_hash=content_hash,
                       extraction_status="skipped", duplicate_status="dup_invoice",
                       invoice_number=vresult.data.get("invoice_number"),
                       vendor_gstin=vresult.data.get("vendor_gstin"),
                       invoice_date=vresult.data.get("invoice_date"),
                       total_amount=vresult.data.get("total_amount"),
                       warning="Same invoice already on record (InvoiceNo+VendorGST+Date+Amount): " +
                               (str(dup_rec.get("excel_row") or dup_rec.get("file_id"))),
                       review_flags="; ".join(vresult.reasons))
                _mark_seen(backpack, dupes, file_id, content_hash, "dup_invoice", args.dry_run)
                continue

            # 7) append decision
            if vresult.decision == "reject":
                counters["reject"] += 1
                _audit(audit, base_audit, content_hash=content_hash,
                       extraction_status="rejected", duplicate_status="new",
                       invoice_number=vresult.data.get("invoice_number"),
                       total_amount=vresult.data.get("total_amount"),
                       review_flags="; ".join(vresult.reasons), parse_error="Not a usable invoice record.")
                _mark_seen(backpack, dupes, file_id, content_hash, "skipped_reject", args.dry_run)
                continue
            if vresult.decision == "review":
                counters["review"] += 1
                _audit(audit, base_audit, content_hash=content_hash,
                       extraction_status="review", duplicate_status="new",
                       invoice_number=vresult.data.get("invoice_number"),
                       vendor_gstin=vresult.data.get("vendor_gstin"),
                       invoice_date=vresult.data.get("invoice_date"),
                       total_amount=vresult.data.get("total_amount"),
                       confidence=vresult.confidence,
                       review_flags="; ".join(vresult.reasons),
                       warning="Requires manual review before it can be used.")
                _mark_seen(backpack, dupes, file_id, content_hash, "skipped_review", args.dry_run)
                continue

            # 8) stage + persist (Excel writes go through the configured gateway:
            #    in-process DirectExcelBackend, or the MCP server when mcp.enabled)
            ctx = build_context(file_id, fname, now_dt(), vresult, {
                "content_hash": content_hash,
                "owner_name": owner["owner_name"], "owner_email": owner["owner_email"],
                "parse_status": "success",
                "input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
                "finish_reason": meta.get("finish_reason"), "latency_ms": meta.get("latency_ms"),
            })
            row_values = mapper.build_row(vresult.data, ctx)

            rows_for_sheet: List[List[Any]] = []
            if excel_cfg.get("write_line_items", True) and vresult.data.get("line_items"):
                rows_for_sheet = Mapper([], cfg["column_map"]).build_line_item_rows(
                    vresult.data["line_items"], backpack.line_items_headers, {
                        "invoice_number": vresult.data.get("invoice_number"),
                        "vendor_name": vresult.data.get("vendor_name"),
                        "invoice_date": vresult.data.get("invoice_date"),
                        "file_id": file_id,
                    })

            row_idx, line_item_count = backpack.append_invoice(
                row_values, rows_for_sheet, file_id,
                write_line_items=excel_cfg.get("write_line_items", True),
                write_processed_ids=excel_cfg.get("write_processed_ids", True),
                flush=not args.dry_run)

            if not args.dry_run:
                dupes.record({
                    "file_id": file_id, "content_hash": content_hash,
                    "invoice_number": vresult.data.get("invoice_number"),
                    "vendor_gstin": vresult.data.get("vendor_gstin"),
                    "invoice_date": vresult.data.get("invoice_date"),
                    "total_amount": vresult.data.get("total_amount"),
                    "fingerprint": vresult.fingerprint.get("fingerprint"),
                    "excel_row": row_idx, "status": "processed",
                    "seen_at": dt.datetime.now().isoformat(),
                })
            counters["new"] += 1

            _audit(audit, base_audit, content_hash=content_hash,
                   extraction_status="ok" if not args.dry_run else "ok(dry-run)",
                   duplicate_status="new",
                   invoice_number=vresult.data.get("invoice_number"),
                   vendor_gstin=vresult.data.get("vendor_gstin"),
                   invoice_date=vresult.data.get("invoice_date"),
                   total_amount=vresult.data.get("total_amount"),
                   confidence=vresult.confidence,
                   excel_row=row_idx, line_item_count=line_item_count,
                   latency_ms=meta.get("latency_ms"), input_tokens=meta.get("input_tokens"),
                   output_tokens=meta.get("output_tokens"), finish_reason=meta.get("finish_reason"),
                   review_flags="; ".join(vresult.reasons))
            log.info("[OK] row %s -> invoice %s (%s) total=%s",
                     row_idx, vresult.data.get("invoice_number"), fname, vresult.data.get("total_amount"))

            if drive_cfg.get("delete_temp_files", True) and os.path.exists(local_path):
                try:
                    os.remove(local_path)
                except OSError:
                    pass

        except (DriveError, ExtractorError, ExcelWriterError, ValueError) as exc:
            counters["failed"] += 1
            log.error("FAILED %s: %s", fname, exc)
            _audit(audit, base_audit, extraction_status="failed", duplicate_status="new",
                   qwen_status=base_audit.get("qwen_status") or "error", parse_error=str(exc)[:2000])
        except KeyboardInterrupt:
            log.warning("Interrupted; flushing staged rows to the workbook, then exiting.")
            try:
                if not args.dry_run:
                    backpack.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("Workbook flush on interrupt failed: %s", exc)
            audit.close()
            return 130
        except Exception as exc:  # noqa: BLE001 - never let one file kill the batch
            counters["failed"] += 1
            log.error("UNEXPECTED ERROR on %s: %s\n%s", fname, exc, traceback.format_exc())
            _audit(audit, base_audit, extraction_status="failed", duplicate_status="new",
                   qwen_status="error", parse_error=f"{exc.__class__.__name__}: {exc}"[:2000])

    exit_code = 0
    try:
        if not args.dry_run:
            backpack.close()
    except ExcelWriterError as exc:
        log.error("Final Excel save failed: %s", exc)
        exit_code = 4
    finally:
        try:
            audit.close()
        except Exception as exc:  # noqa: BLE001 - never mask the real result
            log.warning("Could not close audit logger cleanly: %s", exc)

    log.info("===== RUN SUMMARY =====")
    log.info("new=%d  dup_by_file_id=%d  dup_by_hash=%d  dup_by_invoice=%d  "
             "review=%d  rejected=%d  failed=%d",
             counters["new"], counters["dup_file_id"], counters["dup_hash"],
             counters["dup_invoice"], counters["review"], counters["reject"],
             counters["failed"])
    log.info("pre-qwen filter: small=%d  empty=%d  logo_only=%d  non_invoice=%d  review_required=%d",
             counters["skip_small"], counters["skip_empty"], counters["skip_logo"],
             counters["skip_noninvoice"], counters["review_filter"])
    return exit_code


def _counter_for_status(status: str) -> str:
    return {
        STATUS_SMALL_FILE: "skip_small",
        STATUS_EMPTY: "skip_empty",
        STATUS_LOGO_ONLY: "skip_logo",
        STATUS_NON_INVOICE: "skip_noninvoice",
    }.get(status, "skip_noninvoice")


def _ledger_status_for_filter(status: str) -> str:
    return {
        STATUS_SMALL_FILE: "skipped_small",
        STATUS_EMPTY: "skipped_empty",
        STATUS_LOGO_ONLY: "skipped_logo_only",
        STATUS_NON_INVOICE: "skipped_non_invoice",
    }.get(status, "skipped_non_invoice")


def _sorted(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(files, key=lambda f: (f.get("name", "").lower(), f.get("id", "")))


def _mark_seen(backpack, dupes: DuplicateChecker, file_id: str,
               content_hash: str, status: str, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        backpack.mark_seen(file_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record processed id for %s: %s", file_id, exc)
    dupes.record({
        "file_id": file_id, "content_hash": content_hash or None,
        "invoice_number": None, "vendor_gstin": None, "invoice_date": None,
        "total_amount": None, "fingerprint": None, "excel_row": None,
        "status": status, "seen_at": dt.datetime.now().isoformat(),
    })


def _audit(audit: AuditLogger, base: Dict[str, Any], **over) -> None:
    rec = dict(base)
    rec.update(over)
    audit.log(rec)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - present setup errors cleanly
        print()
        print(f"ERROR: {exc}")
        print("Fix the issue above (see README.md), then re-run.")
        sys.exit(1)