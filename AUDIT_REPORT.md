# Invoice Automation System — Technical Audit Report

**Audit date:** 18 Sep 2026
**Project:** `C:\Users\pkuma\Downloads\BD\Process`
**Scope:** Every module inspected; component-level and end-to-end tests run against the real code (isolated temp copies — project files untouched).
**Auditor note:** Findings are based on actual code inspection and executed tests. Anything that could not be tested live is marked `NOT TESTED` with a reason. No project code was modified.

---

## 1. Executive summary

| Question | Verdict |
|---|---|
| Does the implementation fulfill the business requirement? | **Architecturally yes** — core logic correct, idempotency proven by test. **Cannot run end-to-end yet** because the Drive credential and a running Qwen model are not present in this environment. |
| What prevents demo readiness? | Missing Google service-account key; LM Studio not running; unverified vision capability of the loaded Qwen model; `model.base_url` placeholder; dead `--test-model` flag; missing `.gitignore`. |
| Top 3 fixes | (1) Add `credentials/service_account.json` + share folder. (2) Start LM Studio, set `base_url=http://localhost:1234/v1` and the exact model ID; confirm model accepts images. (3) Add `.gitignore`; fix/remove `--test-model`. |

---

## 2. System architecture (as built)

```
Google Drive Folder
        ↓
Find invoice PDFs/images            src/drive_client.py      (service account, recursive, paginated)
        ↓
Detect already-processed / duplicate
        ↓   ★ 3 keys: Drive File ID → SHA-256 hash → invoice fingerprint
Download new invoice               src/drive_client.py download()
        ↓
Send invoice to Qwen 3.5 9B        src/extractor.py         (text PDF → text; scanned PDF/image → base64 pages)
        ↓
Qwen runs through LM Studio        OpenAI-compatible /v1 endpoint
        ↓
Return structured JSON             response_format=json_object
        ↓
Validate extracted information     src/validator.py         (ok / review / reject, tax arithmetic)
        ↓
Map fields to Excel template       src/excel_mapper.py     (config.column_map)
        ↓
Write to worksheet                 src/excel_writer.py     (details + Details_LineItems + _DocParser_ProcessedIDs)
        ↓
Maintain processing/audit info     src/audit.py            (run_*.jsonl + audit_all_runs.csv)
        ↓
Continue even if one invoice fails per-file try/except (verified)
```

**Entry point:** `python main.py` (`main.py`, optionally via `run.bat`).

---

## 3. Module responsibilities

| File | Role |
|---|---|
| `main.py` | Orchestrator: model ping → Drive scan → per-file loop (dedupe → download → hash → extract → validate → write → ledger → audit). |
| `src/config.py` | Load/merge/validate `config.json`; rebase relative paths; enforce placeholder + folder checks. |
| `src/drive_client.py` | Service-account auth, recursive folder listing, byte download. |
| `src/duplicate_checker.py` | 3-key dedupe; persisted via JSONL ledger + workbook re-seed each run. |
| `src/extractor.py` | Qwen (LM Studio) calls, PDF text / page-image / image message building, JSON parsing, retries. |
| `src/prompt.py` | Extraction prompt + JSON schema builder (override file supported). |
| `src/validator.py` | Field normalization, tax-arithmetic check, confidence, ok/review/reject decision, invoice fingerprint. |
| `src/excel_mapper.py` | Config-driven field→column mapping; writes only columns that exist in the header row. |
| `src/excel_writer.py` | Append-only writer; `.tmp`+`os.replace` safe save; one backup per run. |
| `src/audit.py` | Per-file JSONL + consolidated CSV audit. |
| `src/utils.py` | Hashing, date/number parsing, Drive-ID parsing, path helpers. |

---

## 4. Duplicate detection — CRITICAL

Three persistent keys, checked in order per file:

1. **Google Drive File ID** — checked before download (`main.py:164`)
2. **SHA-256 content hash** — checked after download (`main.py:175`)
3. **Invoice fingerprint** — `InvoiceNo | VendorGSTIN | InvoiceDate | TotalAmount(2dp)`, after extraction (`main.py:199`)

State survives restarts via `data/state/processed_ledger.jsonl` **and** re-seeding from the workbook (the workbook is the source of truth).

| Case | Scenario | Result |
|---|---|---|
| A | Same Drive file processed twice | **PASS** → skip (`dup_file_id`) |
| B | Same invoice renamed / re-uploaded (same bytes) | **PASS** → skip (`dup_file_id` / `dup_hash`) |
| C | Same invoice re-scanned (bytes differ) | **PASS** → skip (`dup_invoice`) |
| D | Two different invoices, similar filenames | **PASS** → both processed |
| E | Same file in multiple folders | **PASS** → second occurrence skipped |

Idempotency acceptance test (real `main.py`, stubbed Drive/model):

- RUN 1: 10 invoices → **10 rows**
- RUN 2: same 10 → **0 new rows**
- RUN 3: 10 + 3 new → **3 new rows** (13 total)

---

## 5. Excel audit

- Uses the real template; destination worksheet **`details`** (also line items → `Details_LineItems`, processed IDs → `_DocParser_ProcessedIDs`).
- All 59 worksheets preserved; no rename/delete/reorder; append-only; existing records untouched.
- All `column_map` targets verified present in the real 76-column `details` header (0 missing).
- Dates/numerics written as real Excel types with correct formats; no dict/list leak into cells.
- Workbook locked (Excel open) → `PermissionError` caught by per-file loop; pending rows flush on retry; final `close()` returns exit 4 if permanently locked. No corruption.

---

## 6. End-to-end test results

| Input | Expected | Actual | Verdict |
|---|---|---|---|
| 5 files, 1 download-fails | 4 written, 1 failed, batch continues | 4 rows + 4 line items + 4 IDs + 1 failed | **PASS** |
| Same 10 files again | 0 new rows | 0 new | **PASS** |
| 10 + 3 new | 3 new rows | 3 new (13 total) | **PASS** |
| Qwen offline | fail-fast, exit 2 | exit 2, clear message | **PASS** |
| Missing service account | exit 3, clear message | exit 3 | **PASS** |
| Excel file locked | graceful | error caught, deferred save | **PASS** |
| Corrupt PDF / non-JSON | per-file failure, continue | `ExtractorError`, batch continues | **PASS** |
| Real Google Drive | — | **NOT TESTED** — reason: `credentials/service_account.json` not provided | — |
| Real Qwen via LM Studio | — | **NOT TESTED** — reason: LM Studio not running; vision capability unverified | — |

---

## 7. Findings & risks

### Critical
1. **Missing Google credential** — `credentials/service_account.json` is absent; app exits 3. Hard blocker.
2. **Vision capability unverified** — image invoices and scanned PDFs are sent as base64 images. A text-only "Qwen 3.5 9B" cannot accept them → all such files would fail. Must confirm the loaded model is a **VL/multimodal** variant.
3. **No `.gitignore` / no git repo** — the service-account key could be committed if git is initialized. Add `.gitignore` before sharing.

### High
4. `model.base_url` still contains the `YOUR_NGROK_URL` placeholder — config validation refuses to run until fixed. For same-PC use set `http://localhost:1234/v1` (ngrok not required).
5. `model.model` string (`qwen3.5-9b-instruct`) must exactly match the LM Studio Copy-ID.

### Medium
6. Per-file full workbook rewrite → slowdown at ~1,000 invoices (bottleneck; batch-save every N invoices).
7. OpenAI SDK default `max_retries=2` **plus** custom retry loop → up to 9 connection attempts on a dead server (double retry layering).
8. Error message advises starting ngrok even though it is unnecessary locally; README over-emphasizes ngrok.

### Low
9. `parse_number("INV-100")` → `-100.0` (hyphenated strings can be misread as negative numbers).
10. `--test-model` flag is parsed but **never used** — it runs the full pipeline, contradicting its help text and README.
11. Dead code: `DATE_ROWS`/`CURRENCY_ROWS` in `validator.py`; `skipped_type` counter never incremented.
12. Files that are duplicates/reviewed/rejected are not removed from `data/downloads` (temp cleanup only on the success path).
13. Audit CSV only appended on clean `close()` — JSONL is the authoritative store (hard-crash safe).

---

## 8. Final verdict table

| Component | Status | Evidence | Severity | Required Action |
|---|---|---|---|---|
| Google Drive auth/list/download | PARTIAL | code verified; no SA key → untested live | CRITICAL | Add `credentials/service_account.json`; share folder with SA |
| Duplicate detection | PASS | all cases tested; ledger persists | HIGH | none |
| Idempotency (RUN1/2/3) | PASS | real `main.py` test | HIGH | none |
| Excel mapping | PASS | 0 missing columns | MEDIUM | none |
| Excel writing/format/safety | PASS | live writes + reload + locked-file test | MEDIUM | none |
| Text-PDF extraction path | PASS | real PDFs extracted | HIGH | none |
| Scanned-PDF / image path | PARTIAL | render works; model vision unverified | CRITICAL | Confirm Qwen is multimodal |
| Qwen / LM Studio integration | NOT TESTED | LM Studio offline | CRITICAL | Start server; fix base_url + model ID |
| JSON parsing | PASS | fence/truncation/non-JSON handled | MEDIUM | none |
| Validation (ok/review/reject) | PASS | 7 samples tested | MEDIUM | none |
| Audit persistence | PASS | jsonl + csv written | LOW | none |
| Failure isolation | PASS | 1 bad file ≠ batch stop | HIGH | none |
| Config validation | PASS | placeholder guarded | LOW | none |
| `--test-model` CLI | FAIL | dead flag runs full pipeline | LOW | implement or remove |
| Security (.gitignore) | FAIL | no `.gitignore` | HIGH | add before git init |
| Performance scalability | PARTIAL | per-file workbook rewrite | MEDIUM | batch saves |
| `parse_number` hyphen bug | FAIL | `INV-100` → `-100.0` | LOW | tighten regex |
| ngrok usage/docs | PARTIAL | not required locally | LOW | use localhost; fix messaging |

---

## 9. Remediation plan (priority order)

1. **Unblock:** create Google Cloud service account → download JSON → place at `credentials/service_account.json` → share the Drive folder with the SA email (Viewer).
2. **Unblock:** start LM Studio → load Qwen model → note exact Model ID → (if images/scanned PDFs needed) load a Qwen **VL/multimodal** variant.
3. **Configure:** in `config.json` set `model.base_url=http://localhost:1234/v1`, `model.model=<exact ID>`, confirm `model.vision_enabled` matches the model.
4. **Verify:** `python main.py --test-model` → `python main.py --dry-run` on a small test folder → real run → re-run to confirm `new=0`.
5. **Protect:** add `.gitignore` covering `credentials/`, `data/`, `*.xlsx`, `__pycache__/`.
6. **Cleanup (nice-to-have):** fix/remove `--test-model`; batch Excel saves; set SDK `max_retries=0` in `extractor.py`; tighten `parse_number`; remove temp files on non-success paths; align README messaging.

---

## 10. Senior/demo readiness checklist

- [ ] `credentials/service_account.json` exists and folder is shared with the SA
- [ ] LM Studio server running; exact model ID present in `config.json`
- [ ] `base_url = http://localhost:1234/v1` (no placeholder)
- [ ] `python main.py --test-model` → "Model reachable", exit 0
- [ ] 2 real invoices (1 text PDF) → 2 rows in `details`, 2 in `Details_LineItems`, 2 IDs in `_DocParser_ProcessedIDs`
- [ ] Re-run → `new=0, dup_by_file_id=2`; row count unchanged
- [ ] Image/scanned-PDF invoice succeeds (proves vision model quality)
- [ ] Excel open during run → graceful continue, no corruption
- [ ] `data/audit/audit_all_runs.csv` populated for every run
- [ ] `.gitignore` in place before any git init/commit

---

*Report generated from live inspection and executed tests. Project files were not modified during the audit.*