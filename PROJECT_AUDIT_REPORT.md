# PROJECT AUDIT REPORT — Invoice Automation Pipeline

**Project:** Drive → Qwen (LM Studio) → Excel invoice automation
**Root:** `C:\Users\pkuma\Downloads\BD\Process`
**Audit date:** 21 Sep 2026
**Status:** Code audit complete — all identified software defects fixed, 128/128 tests passing.

> This report supersedes the earlier `AUDIT_REPORT.md` (18 Sep 2026). Line references below are to
> the current sources in this project.

---

## 1. Scope and method

1. Full inventory of the codebase (`main.py`, `src/*`, configs, workbook template, prior report).
2. Live environment inspection (Python version, installed packages, the real Excel workbook
   sheets/headers, localhost reachability of LM Studio).
3. Designed a 128-test regression suite (unit + end-to-end pipeline) that exercises the real
   OpenAI SDK v3 against an in-process LM Studio emulator, and the real pipeline against a fake
   Drive + mock extractor.
4. Fixed every CRITICAL/HIGH/MEDIUM finding, proved each fix with a test, and re-ran the whole
   suite (second pass, all green).

## 2. Environment verified

| Item | Value |
|---|---|
| OS / shell | Windows, PowerShell 5.1 |
| Python | 3.14.7 (`C:\Users\pkuma\AppData\Local\Python\pythoncore-3.14-64`) |
| pip | 26.2.1 |
| openai | 3.15.0 (SDK v3) |
| google-api-core / google-api-python-client / google-auth | 2.38.0 / 2.200.0 / 2.58.0 |
| openpyxl | 3.1.5 |
| pillow / pypdf / pypdfium2 | 12.3.0 / 6.19.0 / 5.13.0 |
| requests | 2.34.2 |
| LM Studio | **offline** during audit → live-model tests `NOT TESTED` (emulator used instead) |
| Google Drive service account | **not present** (`credentials/service_account.json` missing) → live scan `NOT TESTED` (mocked service used) |

Workbook verified live: 59 sheets; `details` = 76 real headers; `Details_LineItems` headers end at
column 18 (`ASIN`, 7 trailing `None` columns are never written); `_DocParser_ProcessedIDs` = `ID, AddedAt`.

## 3. Findings and fixes

### CRITICAL

| # | Finding | Fix |
|---|---|---|
| C1 | `config.json` `model.base_url` was an ngrok placeholder; `load_config` rejected it → app could not start. | Set `http://localhost:1234/v1`; `config.py` now tells the user ngrok is only needed when this machine cannot reach LM Studio directly. |
| C2 | `--test-model` was a dead flag (parsed, never used). | Implemented: pings Qwen, prints model/base_url/latency/reply, exits `0` (ok) / `2` (unreachable). Live-verified (offline → exit 2). |
| C3 | One bad file could not stop the batch, but the workbook could be left unlocked/saved after an interrupt or a failed final save. | `main()` now: flushes staged rows with `writer.maybe_flush()` after each append; on `KeyboardInterrupt` best-effort `writer.close()` then `audit.close()`, exit `130`; tail always closes the audit in `finally`; a failed final save is caught and returns exit `4` (never crashes). |

### HIGH

| # | Finding | Fix |
|---|---|---|
| H1 | Double retry layering: SDK default `max_retries=2` plus the extractor's own bounded retry loop (9 attempts worst case). | `Extractor` constructs `OpenAI(..., max_retries=0)`; the code owns retry/backoff. Proven by `test_retry_then_success` / `test_retries_exhausted_raises`. |
| H2 | SDK v3 renames usage tokens (`input/output`) vs v1 (`prompt/completion`); raw access could error. | `_usage_tokens()` fallback reads either naming. Proven by `test_tokens_recorded_from_sdk_v3_usage`. |
| H3 | Base64 data-URIs could be built as `data:None;base64,...` when Drive reported no MIME, and image events used the raw Drive MIME instead of the resolved one (e.g. `.tif` declared `application/octet-stream`). | New `_mime_for()` (extension map + mimetypes fallback); the data-URI now uses the resolved MIME. Proven by `test_mime_fallback_for_unknown_mime` and `test_image_sent_as_base64_data_uri`. |
| H4 | Excel save errors (e.g. file open in Excel) crashed the run with a raw OSError. | `ExcelWriter.save()` wraps `wb.save`/`os.replace` into `ExcelWriterError` with the message *"Close the file in Excel if it is open, then re-run."*; pipeline converts it to exit `4` and keeps the workbook pristine. Proven by `test_12_workbook_locked_is_graceful_exit_4` and `test_save_wraps_permission_error`. |
| H5 | Invoice mis-parse: `parse_number("INV-100")` returned `-100.0`. | Regex tightened to `(?:^|\s|[(\[:])([+-]?\d+(?:\.\d+)?)`, read through `group(1)` so the sign must be anchored (start / space / bracket). Proven by `test_invoice_number_never_misread_as_negative`. |

### MEDIUM

| # | Finding | Fix |
|---|---|---|
| M1 | Dead constants `DATE_ROWS` / `CURRENCY_ROWS` in `validator.py`. | Removed. |
| M2 | `_InvoiceDate` in line items written as a string though the column expects a real date. | `build_line_item_rows` converts `YYYY-MM-DD` → `datetime.date`. Proven by `test_dates_written_as_real_types`. |
| M3 | `audit.log()` never guaranteed every record carries `run_id`/`processed_at`, and dropped fields a consumer expected. | `log()` now writes all schema fields (nullable) + always present `run_id`/`processed_at`. Proven by `test_records_contain_all_audit_fields`. |
| M4 | README / `credentials/README.txt` described ngrok as required. | Rewritten localhost-first; ngrok only when running from another machine. |
| M5 | No `.gitignore` (risk of committing `credentials/service_account.json`). | Created: ignores `credentials/*` (except `README.txt`), `data/`, caches, OS junk. |

No CRITICAL/HIGH/MEDIUM issues remain open. Reported statuses (exit codes) documented:
`0` ok · `2` model unreachable · `3` Drive/auth failure · `4` Excel save/lock · `130` interrupted.

## 4. Test suite (128 tests, all passing — `python -m unittest discover -s tests -t .`)

| Module | Tests | Coverage |
|---|---|---|
| `test_audit` | 4 | JSONL schema fields, consolidated CSV, filter fields on skipped files, non-ASCII survival |
| `test_config` | 8 | defaults merge, relative-path rebasing, placeholder rejection, missing template/folder, folder URL→id, output dirs, localhost accept |
| `test_content_filter` | 16 | size gate, blank/logo/banner/letterhead, genuine invoice, scanner noise, disabled-by-config, filter↔pipeline integration |
| `test_drive_client` | 10 | list filtering (extensions, folders, pagination, recursion), MIME support, missing service-account error, download bytes/size-limit/filename-sanitisation/skip-same-size, owner fallbacks |
| `test_duplicate_checker` | 9 | the 5 documented idempotency cases, ledger persistence, workbook re-seed, skipped records block re-processing |
| `test_excel_mapper` | 5 | header-only mapping, dual `PartyGST`/`VendorGST`, internal audit columns, line items, None → blank |
| `test_excel_writer` | 11 | first-free-row append, header preservation, single backup, flush batching (`flush_every_n`), lock→`ExcelWriterError`, close flush, real date types |
| `test_extractor` | 14 | stub-LM-Studio protocol: ping, text-PDF→text, image→data-URI, unknown MIME fallback, vision-disabled, unsupported file, fenced/trailing JSON, non-JSON error, `response_format` fallback, retries, SDK-v3 usage tokens, scanned-PDF→vision |
| `test_main_pipeline` | 14 | end-to-end: append, batch, bad-file resilience, RUN1/RUN2/RUN3 idempotency, 25KB boundary, pre-Qwen skips, logo, fingerprint dup, review/reject records, offline→exit 2, missing SA→exit 3, locked workbook→exit 4, dry-run writes nothing, workbook re-seed skip |
| `test_utils` | 22 | sha256, `parse_number` (INV-100 regression, ₹/comma/negative), `parse_date`, `parse_drive_id`, `safe_boolish` |
| `test_validator` | 15 | ok/review/reject decisions, arithmetic match/mismatch/tolerance, fingerprint normalization, no fabrication, GSTIN cleaning, line totals |

## 5. Honest status of environment-dependent checks

| Check | Status | Evidence |
|---|---|---|
| Real Qwen extraction (LM Studio) | **NOT TESTED** | `http://localhost:1234/v1/models` connection refused during audit |
| Real Google Drive scan | **NOT TESTED** | no `credentials/service_account.json` present |
| Qwen wire protocol, JSON parsing, retries, `response_format` fallback, vision routing | **SIMULATED (PASS)** | 14 extractor tests against in-process emulator |
| Drive list/download logic | **SIMULATED (PASS)** | 10 tests against mocked Google API surface |
| Pipeline + idempotency + exit codes | **PASS** | 14 end-to-end tests (real `main.main()`, fake Drive/extractor) |

## 6. Recommended next steps (operator, not code)

1. Start LM Studio (Server on `localhost:1234`, model `qwen3.5-9b-instruct`), then run
   `python main.py --test-model` — expect `[OK]` and exit 0.
2. Add `credentials/service_account.json` and share the Drive folder with the service account.
3. First real run: `python main.py --dry-run` (scans + extracts, writes nothing), review the audit
   output, then a real run.

## 7. Deliverables

- `PROJECT_AUDIT_REPORT.md` (this file)
- `README.md` (updated localhost-first Qwen setup) · `credentials/README.txt` (updated)
- `config.json` (safe localhost example) · `.gitignore`
- `main.py` + `src/*` (all fixes above) · `tests/` (128 tests) · `requirements.txt`
- `invoice_automation_final.zip` (this project, **excluding** `credentials/`, `data/`, `__pycache__/`)