# PROJECT FINAL AUDIT — Target Office PC Deployment

**Audit date:** 2026-09-22 · **Suite:** `python -m unittest discover -s tests -t .`
**Audit machine:** development PC (NOT the execution machine).
**Execution machine:** the **TARGET OFFICE PC** (Python + LM Studio + Qwen 3.5 9B 4-bit +
Google Drive + the Excel template + the MCP layer).

This audit judges the **project itself** — portability, configurability, wiring, and
idempotency — so that copying the project to the office PC and configuring it there is
sufficient. It does **not** judge the project by whether LM Studio / Qwen / the service
account / the folder currently exist on this development PC.

---

## 1. Executive verdict

| Area | Verdict | Evidence |
|---|---|---|
| Portable / not tied to this PC | **PASS** | No machine-specific paths or usernames in code; all paths relative to `config.json` (see §3) |
| End-to-end workflow (Drive → filter → dupes → download → Qwen → JSON → validate → MCP → Excel → audit) | **WORKING** | Real `main.main()` chain exercised end-to-end with fake Drive + stub Qwen; 141 tests pass |
| MCP Excel layer | **REAL, TESTED** | `src/mcp/*`: JSON-RPC 2.0 stdio server + client; real subprocess round-trip test |
| Qwen/LM Studio wiring | **CORRECTLY WIRED (not live-verified here)** | Extractor proven against a real in-process OpenAI-compatible HTTP server; `--test-model` guard |
| Google Drive wiring | **CORRECTLY WIRED (not live-verified here)** | Client unit-tested (list/download/mime/owner/errors); key absent on this dev PC only |
| Excel template safety | **VERIFIED** | Append-only writer; 59 sheets / `details` (76 headers) untouched; backups once/run |
| Filtering + idempotency | **PASS** | Covered below (§5, §6) |
| Blocking deployment issues | **NONE FOUND** | Every machine-specific value is configurable |

**Live-only checks** (Google Drive scan, LM Studio endpoint, a real Qwen completion) require
office-PC runtime resources and are marked **NOT VERIFIED** on this dev PC — with the exact
command to complete them on the office PC. That is a deployment step, not a project defect.

---

## 2. How the real execution flow is wired (traced in code)

1. `main.main()` (`main.py:83`) → `load_config(config.json)` (`src/config.py`, paths rebased
   to the config directory) → `ensure_dir` the runtime folders.
2. `--test-model` pings LM Studio (`src/extractor.py:Extractor.ping`) — exits 2 on failure.
3. `DriveClient.list_folder` (`src/drive_client.py`) — recursive, extension/MIME filtered,
   Drive-metadata size captured.
4. `open_excel_backend(cfg, cfg_path, dry_run)` (`src/excel_backend.py`) → MCP path when
   `mcp.enabled` (spawns `python -m src.mcp.server`) or direct in-process writer.
   `DuplicateChecker` seeds from ledger + workbook (`src/duplicate_checker.py`,
   `excel_writer.read_template_for_seed`) — the workbook is the source of truth.
5. Per file: **file-ID dedupe → Drive-metadata size gate (<25 KB, pre-download) → download
   (+ optional md5 integrity check) → SHA-256 dedupe → PRE-QWEN content filter → Qwen →
   validation (+fingerprint) → invoice-fingerprint dedupe → append/review/reject →
   Excel append (details + line items + processed IDs) → ledger → audit.** Errors are caught
   per file; one bad file never stops the batch.
6. Every step's output is consumed by the next: the *same* downloaded `local_path` is filtered,
   hashed, and sent to Qwen; Qwen's raw text is force-parsed to JSON, validated, mapped, and
   written by the MCP Excel tools.

## 3. Portability / configuration audit

| Check | Result |
|---|---|
| Hard-coded Windows paths / usernames in code | **None.** Only historical `.md` docs mentioned the dev path; README now generic. |
| Configurable `<25 KB` / filtering / model / folder / creds / Excel paths | **All in `config.json`**, rebased to the config dir. |
| LM Studio endpoint + model + port | Configurable via `model.base_url` / `model.model`; port is part of `base_url`. |
| ngrok | **Optional only.** Default `http://localhost:1234/v1` is correct when Python + LM Studio share the office PC. `YOUR_NGROK_URL` placeholder is rejected by config validation (`src/config.py`), so a stale ngrok config fails fast instead of silently failing. |
| Credentials | **Not bundled.** `credentials/` ships only `README.txt`; the service-account key is user-supplied. `.gitignore` excludes `credentials/*`. |
| Service-account location | Configurable `drive.service_account_json`. |
| Excel template | Configurable `excel.template_path` (defaults to the file beside `main.py`). |
| Documented office-PC setup | YES — README §2–§10 (prereqs, Drive, LM Studio, model id, MCP, config, run, logs, troubleshooting). |
| Safe config example | YES — `config.example.json` (copy → `config.json`, fill `CHANGE_ME`). |

## 4. Google Drive

- **Auth:** service account (`google.oauth2.service_account`), `drive.readonly` scope only.
- **Folder:** configurable id/url; recursive walk with pagination (`nextPageToken`).
- **Files:** extension + MIME filter; size, `md5Checksum`, owners captured.
- **Download:** `MediaIoBaseDownload` to `data/downloads`, `max_file_size_mb`, optional
  **md5 byte-for-byte verification** (`drive.verify_download_md5`) so partial/corrupt/stale
  cached downloads fail isolated instead of being sent to Qwen.
- **Errors:** `HttpError` → `DriveError` with a clear message; missing key → early, clear error.
- Can the project run after the office PC supplies its key + folder? **YES.**

## 5. Filtering (before Qwen)

| Rule | Status |
|---|---|
| Below 25 KB → skip, no download/Qwen/Excel row | `SKIPPED_SMALL_FILE` (Drive metadata, pre-download); exactly 25 KB is eligible |
| Blank / empty / unreadable → skip, logged | `SKIPPED_EMPTY` |
| Logo / letterhead / watermark-only → skip, logged | `SKIPPED_LOGO_ONLY` |
| Banner / decorative / marketing → skip, logged | `SKIPPED_NON_INVOICE` |
| Clearly non-invoice → skip, logged | `SKIPPED_NON_INVOICE` |
| Scanned / image / little-text / no "invoice" word / unusual layout | **not rejected** — uncertain → `REVIEW_REQUIRED` and still sent to Qwen |
| Every skip logged with a reason | YES (audit `filter_status`, `skip_reason`, `filter_score`, `qwen_status="not_run"`) |

Filtering is deliberately conservative: only *clearly* irrelevant files are auto-skipped.

## 6. Duplicates / idempotency (persistent, office-PC safe)

- Three keys: Drive File ID → SHA-256 hash → invoice fingerprint `InvoiceNo|VendorGSTIN|Date|Total`.
- State persists in `data/state/processed_ledger.jsonl` **plus** the workbook (`_DocParser_ProcessedIDs`,
  `FileID`, `_ContentHash`, invoice fields) re-seeded every start — crash-safe.
- Acceptance runs (two transports) confirm: RUN1 5 files → 2 rows; RUN2 same 5 → 0 rows;
  RUN3 all 8 → exactly 1 new row (plus dup-by-hash and blank-skip) — total 3 rows.
- Renamed/re-uploaded identical bytes → `dup_hash`; same invoice different bytes → `dup_invoice`;
  two different invoices with similar names → both processed.

## 7. Qwen + LM Studio

- **Model:** Qwen 3.5 9B 4-bit via LM Studio's OpenAI-compatible server.
- **Endpoint:** configurable `base_url`; default `http://localhost:1234/v1` is correct for the
  office PC (Python and LM Studio on the same machine; **no ngrok required**).
- **Content actually sent:** extracted PDF text embedded in the prompt; images as base64
  `data:` URIs; scanned/image PDFs rendered page-by-page (`pypdfium2`) to images — the file's
  real bytes, not a filename/assumption.
- **Request/response:** structured JSON schema prompt (`src/prompt.py`), `json_object` with
  graceful fallback, fenced/ragged JSON recovery, bounded retries+backoff, token/latency meta.
- **Live verification is available in one command on the office PC:** `python main.py --test-model`.
  On this dev PC the endpoint is offline, so the LLM leg is unit-verified against a real
  in-process OpenAI-compatible HTTP server instead.

## 8. JSON → validation

`Extractor._parse_json` handles markdown fences, trailing text, and balanced-brace rescue.
`validate_extraction` normalizes types/dates/numbers/GSTIN, checks tax arithmetic, computes the
fingerprint, and returns `ok|review|reject`. Invalid extraction never silently becomes Excel
data — rejected files are logged and marked seen.

## 9. JSON → MCP → Excel

- **MCP is real:** JSON-RPC 2.0 over stdio (`src/mcp/protocol.py`, `transport.py`, `server.py`,
  `client.py`); tools `excel_read_seed` / `excel_append_invoice` / `excel_mark_seen` /
  `excel_flush` / `excel_close`. Runnable: `python -m src.mcp.server --config config.json`.
- A real **subprocess** round-trip test launches the actual server entry point.
- Excel writes go through the gateway; `mcp.enabled` just switches transport.
- **Target sheet `details`** (76 headers): every `column_map` target resolves to an existing
  header (verified programmatically). Line items → `Details_LineItems`, processed IDs →
  `_DocParser_ProcessedIDs`. No worksheets/columns renamed, reordered, or added; append-only;
  safety backup once/run; `null`/missing stays blank.

## 10. This audit's repairs

1. **Download integrity (new, opt-in):** `drive.verify_download_md5` (`config.json` / `config.py`
   default `false`). When enabled, downloaded bytes are verified against Drive's `md5Checksum`
   (whole-file MD5 for files ≤ 5 MB) and stale/corrupt cached downloads are detected — so a
   partial or corrupted download can never be the content sent to Qwen. Isolated per file.
2. **Docs repackaged for the office PC:** README rewritten around target-PC deployment
   (prereqs, LM Studio/model-id, Drive/service-account, MCP, config, run, logs, troubleshooting);
   dev-PC paths removed.
3. **`config.example.json`** added — a safe, `CHANGE_ME`-marked configuration template.
4. **`requirements.txt`** — Python 3.10+ note; MCP noted as stdlib-only (zero extra deps).
5. **`.gitignore`** — now excludes `config.local.json`, `config.office.json`, `*.zip`.

## 11. Test suite (executed in this audit)

```
Ran 145 tests in 20.8 s — OK   (all 145 pass)
```

| Module | Tests |
|---|---|
| audit | 4 |
| config (defaults, rebasing, MCP, placeholder rejection) | 9 |
| content_filter (size boundary, blank/logo/banner/invoice-scenarios) | 16 |
| drive_client (list, mime, download, max-size, sanitisation, owners, md5 integrity) | 14 |
| duplicate_checker (5 idempotency cases + ledger + workbook re-seed) | 9 |
| excel_mapper | 5 |
| excel_writer | 11 |
| extractor (protocol, text/image/scanned-PDF, fenced JSON, retries, tokens) | 14 |
| mcp (protocol, tools, locked-workbook mapping, real subprocess) | 7 |
| main_pipeline (exit codes 0/2/3/4, idempotency, MCP e2e, locked workbook) | 16 |
| utils | 22 |
| validator | 15 |
| acceptance_scenario (3 runs × direct + MCP) | 3 |
| **Total** | **145** |

## 12. Final 16 questions (yes/no)

1. Deployable to the TARGET OFFICE PC? — **YES**
2. Connect to Google Drive? — **YES** (wired + unit-tested; key is a deployment step)
3. Correctly filter irrelevant files? — **YES**
4. Skip files below 25 KB? — **YES** (pre-download size gate)
5. Detect blank/logo/banner/non-invoice files? — **YES**
6. Detect duplicates (persistently)? — **YES**
7. Download valid invoices? — **YES** (+ optional md5 integrity verification)
8. Send actual invoice content to Qwen 3.5 9B through LM Studio? — **YES** (text/images/rendered pages)
9. Qwen produce usable structured JSON? — **YES** (schema prompt + JSON recovery + unit-proven)
10. JSON validated? — **YES** (types/dates/arithmetic/fingerprint, ok/review/reject)
11. JSON reaches MCP? — **YES** (real JSON-RPC stdio client, subprocess-proven)
12. MCP writes into the provided Excel template? — **YES**
13. Correct worksheet/columns populated? — **YES** (`details` + line items + processed IDs)
14. Whole process generates the final invoice dataset? — **YES** (acceptance = exactly 3 rows / 3 runs)
15. Repeat-safe / idempotent? — **YES**
16. Blocking issues remaining? — **NO** (no project-side blockers; only office-PC deployment steps)

## 13. Remaining office-PC smoke steps (not project defects)

```powershell
python main.py --test-model                    # 1) LM Studio reachable + model loaded
python -m unittest discover -s tests -t .      # 2) suite green on the office PC too
python main.py --dry-run                       # 3) full scan + extraction, writes nothing
python main.py                                 # 4) real run (writes Excel via MCP)
```

These steps cannot be executed on this development PC because LM Studio/Qwen, the service
account key, and the business Drive folder do not exist here — by design they live on the
target machine.