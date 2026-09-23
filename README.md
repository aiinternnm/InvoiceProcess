# Drive → Qwen → MCP → Excel Invoice Pipeline

End-to-end, idempotent invoice extraction that turns a Google Drive folder of
PDFs / images into structured rows in your existing Excel workbook.

```
Google Drive folder → file discovery → duplicate check → size gate (< 25 KB → skip, no download)
→ download → PRE-QWEN content filter (blank / logo-only / banner / non-invoice → skip)
→ Qwen 3.5 9B (LM Studio, localhost) → strict JSON → validation → Excel template
→ append (details + line items + processed IDs) via the MCP Excel layer → audit log
```

The pipeline is **portable**: it is designed to be copied to the **TARGET OFFICE PC**
(python + LM Studio + Qwen + Google Drive + the Excel template) and configured there.
Nothing is bound to any particular development machine.

---

## 1. What the pipeline does

* Scans a shared Google Drive folder (recursively) for `PDF / JPG / JPEG / PNG / TIF / BMP / WEBP / GIF`.
* **Filters before Qwen** (cheap checks first, expensive AI second):
  1. **Size gate** (before download, from Drive metadata): files below
     `filtering.min_file_size_kb` (default **25 KB**) are skipped — `SKIPPED_SMALL_FILE`.
     Exactly 25 KB is NOT skipped.
  2. **Content filter** (after download): blank/empty/corrupt files → `SKIPPED_EMPTY`;
     logo/letterhead/watermark-only images → `SKIPPED_LOGO_ONLY`; marketing banners /
     decorative graphics → `SKIPPED_NON_INVOICE`. Strong-signal files are sent to Qwen
     (`PROCESSED`). Anything uncertain is flagged `REVIEW_REQUIRED` and is still reviewed
     by Qwen so legitimate but unusual/scanned invoices are never lost.
* **Skips anything already processed** using three persistent duplicate keys:
  1. Google Drive **File ID** (primary — also re-seeded from the workbook every run)
  2. **SHA-256 content hash** (secondary — catches renamed / re-uploaded files)
  3. Invoice fingerprint = `InvoiceNo + VendorGSTIN + InvoiceDate + TotalAmount` (tertiary)
* Downloads each new file, **verifies it** (optional md5 integrity check), sends its actual
  content — extracted PDF text or rendered page images / base64 image data — to **Qwen 3.5 9B**
  running in **LM Studio**, and asks for strict structured **JSON**. The model never fabricates:
  missing/uncertain fields are `null` and flagged for review.
* **JSON is primary; XML is the fallback.** If the JSON reply is malformed, truncated/unusable,
  or parses but fails validation ("reject" level), the extractor makes exactly ONE controlled
  second request asking Qwen for strict **XML** (same canonical schema, never uses
  `response_format`), parses it with an XXE/entity-expansion guard, and maps it into the same
  canonical invoice object that validation and Excel consume — so a fallback never creates a
  second record. If both fail, the file is marked `failed`. Which format produced each record
  is captured in the audit as `source_format` / `xml_fallback`.
* Validates numbers/dates, checks tax arithmetic (`taxable + CGST + SGST + IGST ≈ total`),
  and classifies every record as **append / review / reject**.
* Appends only genuinely new, valid invoices to the existing `details` sheet of the provided
  template — it never renames, reorders, deletes or adds worksheets, never changes existing
  column names, and writes only under columns that already exist.
* Also appends line items to `Details_LineItems` and processed file IDs to `_DocParser_ProcessedIDs`.
* Writes Excel through the **`src.excel_backend` gateway**: with `"mcp": {"enabled": true}`
  (the shipped default) every Excel write happens through a real **MCP server**
  (`python -m src.mcp.server`, JSON-RPC 2.0 over stdio). Setting `mcp.enabled` to `false`
  uses the in-process direct writer — enabling/disabling MCP is purely a config decision.
* Writes a full audit trail (every file → `data/audit/run_*.jsonl` + `audit_all_runs.csv`).
* Is **idempotent**: re-running the same folder (or restarting mid-run) never creates
  duplicates. The workbook itself is re-read as the source of truth at every start, so even
  a crash between "saved" and "ledger updated" cannot cause a double append.
* Never lets one bad file stop the batch — failures are logged and the run continues.

---

## 2. TARGET OFFICE PC prerequisites

Install everything below **on the office PC** (the machine that will actually run the pipeline).

| # | Prerequisite | Notes |
|---|---|---|
| 1 | **Python 3.10+** (3.11/3.12 recommended) | `python --version` |
| 2 | **Dependencies** | `python -m pip install -r requirements.txt` |
| 3 | **LM Studio** | https://lmstudio.ai — install on the office PC |
| 4 | **Qwen 3.5 9B 4-bit** | Load the model in LM Studio (My Models → find compatible GGUF). Use **LM Studio → Developer** to start the *Local Server* on `http://localhost:1234`. |
| 5 | **Google Drive** | Google Cloud project with the Drive API enabled + a **service account** key (see §4), and the business folder shared with that account (Viewer). |
| 6 | **Excel template** | `Vendor_Invoice_Details_TEMPLATE_blank.xlsx` placed next to `main.py` (default `excel.template_path`). |
| 7 | **MCP** | No extra install — `src/mcp/` is stdlib-only and runs as `python -m src.mcp.server`. |

### LM Studio specifics

* Start the server: **Developer tab → Start Server** on `http://localhost:1234`.
* Copy the exact model id from **LM Studio → My Models → right-click → Copy ID** and set it as
  `model.model` in `config.json` (e.g. `qwen3.5-9b-instruct:latest`).
* `model.api_key` can stay `lm-studio` (LM Studio's default) for a local-only server.
* `model.vision_enabled` must be `true` for scanned/image invoices; leave it `true`.
* Verify with `python main.py --test-model` before the first real run.
* **ngrok is OPTIONAL and never required when Python and LM Studio are on the same office PC.**
  It is only relevant if the client and the LM Studio host are different machines; in that
  case put the ngrok URL in `model.base_url` and its auth key in `model.api_key`.
  The config validator rejects a leftover `YOUR_NGROK_URL` placeholder so a bad config fails
  fast instead of silently misdirecting.

---

## 3. Install (on the office PC)

```powershell
cd <folder-where-you-copied-the-project>
python -m pip install -r requirements.txt
```

## 4. Google Drive configuration (service account)

1. Create a Google Cloud project → enable the **Google Drive API**.
2. IAM & Admin → Service Accounts → create one (no role needed) →
   **Keys → Add Key → JSON** → save the downloaded JSON to
   `credentials/service_account.json` (see `credentials\README.txt`).
3. In Drive, share the business folder with the service account email, **Viewer** access.
4. Set `drive.folder_id` (or `drive.folder_url`) in `config.json` to that folder.

The service account can only see files/folders explicitly shared with it.

## 5. Configure — all in `config.json`

| Key | What |
|---|---|
| `drive.service_account_json` | Path to the service-account key (relative to config.json, i.e. `credentials/service_account.json`). |
| `drive.folder_id` / `drive.folder_url` | The office folder. One of the two is enough. |
| `drive.corpora` / `drive.drive_id` | Shared Drive access. When the folder lives in a **Shared Drive**, set `drive.corpora` to `drive` and `drive.drive_id` to the **top-level Shared Drive ID** (right-click the Shared Drive → Settings → "ID" / the URL segment after `folders/...` of the drive root). Leave both empty for a plain My Drive folder. The scanner always sends `supportsAllDrives=true` + `includeItemsFromAllDrives=true` so shared-drive files are found; these two keys narrow the search to the exact shared drive (avoids `allDrives` incomplete-search risk). |
| `drive.verify_download_md5` | `true` = verify downloaded bytes against Drive's `md5Checksum` (whole-file MD5 for files ≤ 5 MB; corrupt downloads fail isolately). Default `false`. |
| `model.base_url` | `http://localhost:1234/v1` when Python and LM Studio share the office PC. |
| `model.api_key` | `lm-studio` for local; the ngrok auth key only if using ngrok. |
| `model.model` | The exact model id loaded in LM Studio. |
| `model.vision_enabled` | `true` = Qwen reads images/scanned PDFs. |
| `model.max_tokens` | Completion cap. Qwen3.5 "thinking" counts fully against it, so give generous headroom for reasoning + a 45-field invoice JSON (default `8192`). If you see "truncated / reasoning-only" errors, raise it further or disable thinking in LM Studio. |
| `excel.template_path` | The provided template relative to config.json. |
| `excel.target_sheet` | `details` (kept). |
| `excel.lineitems_sheet` / `processed_ids_sheet` | `Details_LineItems` / `_DocParser_ProcessedIDs` (kept). |
| `column_map` | Maps every extracted JSON field to a worksheet column; edit freely without touching code. |
| `filtering.min_file_size_kb` | Pre-Qwen size gate (default `25`). Values below are skipped before download. |
| `filtering.enable_content_filter` | `true` = run blank / logo / banner / candidate checks before Qwen. |
| `filtering.enable_logo_only_filter` | `true` = auto-skip logo/letterhead/watermark images. |
| `mcp.enabled` | `true` = route every Excel write through the MCP server (real JSON-RPC stdio subprocess). `false` = in-process direct writes (tests / dry-run). |
| `mcp.transport` | `stdio` (real subprocess, default) or `inproc` (hermetic tests only). |
| `audit.dir` / `audit.consolidated_csv` | Audit output locations (auto-created). |

Every relative path is rebased against the directory containing `config.json`, so the whole
project can sit in any folder on the office PC. A ready-to-fill template is in
`config.example.json` — copy it to `config.json` and change the `CHANGE_ME` values.

## 6. Run

```powershell
python main.py                 # full pipeline (MCP Excel writes enabled by default)
python main.py --dry-run       # full scan + extraction, writes nothing
python main.py --step          # trial mode: process one file, then ask to continue or stop
python main.py --limit 25      # process at most the first 25 files and stop (no prompting)
python main.py --mock-extract  # inject a sample invoice instead of calling Qwen (still needs Drive)
python main.py --test-model    # ping the Qwen/LM Studio endpoint only
python main.py --folder "https://drive.google.com/drive/folders/<ID>"  # override folder for one run
```

or double-click `run.bat`.

Exit codes: `0` ok · `2` model unreachable · `3` Drive failure · `4` final Excel save failed
(a per-file `ExcelWriterError` does not stop the batch, but a final save failure returns 4).

## 7. Pre-Qwen filtering flow

```
Drive metadata / size check  ──below MIN_FILE_SIZE_KB──▶ SKIPPED_SMALL_FILE (no download)
        │
        ▼
download (+ optional md5 verify) + SHA-256 dedupe
        │
        ▼
content check (PIL for images, pypdf + pypdfium2 for PDFs)
        ├── blank / empty / corrupt      ▶ SKIPPED_EMPTY
        ├── logo / letterhead / stamp    ▶ SKIPPED_LOGO_ONLY
        ├── banner / decorative graphic  ▶ SKIPPED_NON_INVOICE
        ├── uncertain                    ▶ REVIEW_REQUIRED (still reviewed by Qwen, flagged)
        └── invoice candidate            ▶ Qwen extraction → validation → Excel
```

Skipped files never reach Qwen (`qwen_status = "not_run"`) and never create an Excel
invoice row, but every one is written to the audit log with its status and reason.

| Status | Meaning |
|---|---|
| `PROCESSED` | Passed pre-filter, sent to Qwen |
| `REVIEW_REQUIRED` | Weak signals; sent to Qwen but flagged for manual attention |
| `SKIPPED_SMALL_FILE` | Size below `min_file_size_kb` |
| `SKIPPED_EMPTY` | Blank / empty / unreadable / corrupt |
| `SKIPPED_LOGO_ONLY` | Logo, letterhead, watermark or seal without invoice data |
| `SKIPPED_NON_INVOICE` | Marketing banner / decorative graphic / clearly non-invoice |
| `DUPLICATE` | Already processed (file ID, hash, or invoice fingerprint) |
| `FAILED` | Processing error |

## 8. Duplicate / idempotency semantics

Three keys, checked in order, seeded from **workbook + ledger** at start:
`dup_file_id` (Drive) → `dup_hash` (SHA-256) → `dup_invoice` (fingerprint =
`InvoiceNo|VendorGSTIN|InvoiceDate|TotalAmount`). Skipped/reviewed/rejected files are also
recorded so they are not re-downloaded every run. The ledger lives in
`data\state\processed_ledger.jsonl`; the workbook is authoritative and re-seeds every start.

| Scenario | Result |
|---|---|
| Same Drive file listed again | skip `dup_file_id` |
| Same invoice renamed + re-uploaded (identical bytes) | skip `dup_hash` |
| Same invoice re-uploaded with different bytes | skip `dup_invoice` (fingerprint) |
| Two genuinely different invoices, similar filenames | both processed (different ids/hashes/fingerprints) |

## 9. Where things land

* `data\backups\` — timestamped copy of the workbook before the first write of each run.
* `data\downloads\` — temp downloaded files (auto-deleted unless `delete_temp_files=false`).
* `data\state\processed_ledger.jsonl` — duplicate ledger (fast cache; workbook is authoritative).
* `data\audit\` — `run_<id>.jsonl` per run + `audit_all_runs.csv` (every file: Drive ID,
  filename, owner/sender, MIME, content hash, timestamps, Qwen status, duplicate status,
  invoice number, extraction status, errors).

## 10. Checking logs / troubleshooting

* Run `python main.py` with output visible; per-file errors are logged and do not stop the run.
* `data\audit\audit_all_runs.csv` is the per-file history — check `extraction_status`,
  `filter_status`, `duplicate_status`, `parse_error`, `warning`.
* Exit code 2 → LM Studio not reachable: start the server, fix `model.base_url`/`model.model`.
* Exit code 3 → Drive issue: check `credentials/service_account.json`, folder id, sharing.
* Exit code 4 / "Could not save workbook" → close the file in Excel and re-run.
* A file stuck in skip/reject that you now want retried: delete its line(s) from
  `data\state\processed_ledger.jsonl` (the workbook reseeds the successfully appended rows).

## 11. Tests

```powershell
python -m unittest discover -s tests -t .
```

Covers the required scenarios (10 KB logo → `SKIPPED_SMALL_FILE`, 24.9 KB, exactly 25 KB
boundary, blank image, logo-only, marketing banner, genuine image invoice, small invoice,
invoice-with-logo, letterhead-only PDF), the three-run idempotency scenario, MCP protocol +
real stdio subprocess round-trip, and end-to-end checks that filtered files never reach
Qwen/Excel and still appear in the audit log.

## 12. Module layout

```
main.py                  orchestration (nothing else changes when you edit one part)
src/content_filter.py    PRE-QWEN filter: size gate + blank/logo/banner/invoice-candidate checks
src/drive_client.py      Drive ingestion (service-account list + download + optional md5 verify)
src/extractor.py         Qwen via LM Studio (text, image, scanned-PDF input)
src/prompt.py            extraction prompt / JSON schema (editable)
src/validator.py         strict JSON validation + arithmetic + fingerprint
src/duplicate_checker.py file-ID / hash / invoice-fingerprint dedupe
src/excel_mapper.py      config-driven JSON→column mapping
src/excel_writer.py      append-only writer (details / line items / processed IDs)
src/excel_backend.py     Excel gateway: DirectExcelBackend ↔ MCP server (stdio subprocess)
src/mcp/                 Model Context Protocol layer: protocol, transport, server, client + Excel tools
src/audit.py             run + consolidated audit logs
src/config.py            config loader/validation
src/utils.py             hashing, date/number parsing
```