# PROJECT FINAL AUDIT — Shared-Drive Fix + Qwen Extraction Hardening + JSON→XML Fallback + Target Office PC Deployment

**Audit date:** 2026-09-23 · **Suite:** `python -m unittest discover -s tests -t .` → **178 tests, all pass**
**Audit machine:** development PC (`C:\Users\pkuma\Downloads\BD\Process`, Python 3.14 system install — **no project venv, no service-account key, no LM Studio** on this machine).
**Execution machine:** the **TARGET OFFICE PC** (Python + venv + LM Studio + Qwen 3.5 9B 4-bit + service-account key + Google Shared Drive + the Excel template + MCP).

---

## 1. ORIGINAL ISSUE (the blocker)

`python main.py --dry-run` reported **`Found 0 supported files in folder 1jeH4_...`** and stopped.

A separate Shared-Drive-aware diagnostic using the **same service account** listed **MANY actual PDFs** in the **same folder**.

| Proven fact | Evidence |
|---|---|
| Service-account auth | PASS (per operator diagnostic) |
| Service-account access to folder | PASS (per operator diagnostic) |
| Shared-Drive-aware file listing | PASS — many PDFs listed |
| Main application file listing | FAIL — 0 files |
| MIME/filename filter | CORRECT — `_is_supported()` matches `.pdf`/image MIME; not the cause |
| Pagination | CORRECT — `nextPageToken` loop present |

## 2. ROOT CAUSE (proved from code)

`src/drive_client.py` `DriveClient.list_folder()` invoked the Drive API without
**any shared-drive parameters**:

```python
resp = svc.files().list(q=q, pageSize=1000, fields=..., pageToken=token).execute()
```

Google's official *Implement shared drive support* guide states the operations listed
(`files.list` among them) **must** include `supportsAllDrives=true`, and that with
`includeItemsFromAllDrives` "not present or set to false, … shared drive items are not
returned". The `Vendorinvoice` folder lives in Shared Drive `0AEzHZDHjdNwFUk9PVA`, so the
un-parameterised `files.list` returned **0 items** while the shared-drive-aware diagnostic
returned many. Every other stage in the pipeline was verified healthy (see tests).

**This was the entire discrepancy.** No file was ever lost by filtering, duplication,
size-gating, or Excel logic.

## 3. THE FIX (minimal, config-driven, portable)

1. **`src/drive_client.py`** — `list_folder()` now always sends
   `supportsAllDrives=True` and `includeItemsFromAllDrives=True`, and **optionally**
   `corpora` + `driveId` to narrow the search to one Shared Drive (avoids the
   `allDrives` incomplete-search risk; uses the parent-folder query + `nextPageToken`
   pagination exactly as before). Fails fast with a clear `DriveError` if
   `corpora="drive"` but no `drive_id` is given.
2. **`src/config.py`** — new `drive.corpora` / `drive.drive_id` defaults (empty = plain
   My Drive folder) with validation: `corpora` must be `user|drive|allDrives|domain`;
   `corpora="drive"` requires `drive_id`.
3. **`main.py`** — forwards `corpora`/`drive_id` from config to `list_folder` only when
   non-empty (no behaviour change for existing My-Drive configs).
4. **`config.json`** (production) — `"corpora": "drive"`, `"drive_id": "0AEzHZDHjdNwFUk9PVA"`.
5. **`config.example.json` + README** — documented for the office PC.

No Drive files/folders/permissions are created, deleted, moved, or modified by this fix.
No credentials, Excel data, ledger state, LM Studio/Qwen setup, or MCP setup is touched.

## 3B. SECOND AUDIT — QWEN EXTRACTION STAGE (`response_format` 400 / thinking budget)

**Live evidence (office/run PC):** `POST /v1/chat/completions` with
`response_format={"type":"json_object"}` → **HTTP 400 "response_format not supported"**;
the extractor caught it and re-issued without `response_format` (confirmed by the
`response_format not supported, retrying without it` log line). A direct Qwen test also
showed `max_tokens=100` → **empty content** (reasoning consumed the entire budget) while
`max_tokens=2048` → valid JSON. Qwen3.5 emits `reasoning_content` that counts **fully
against `max_tokens`**.

**Root causes found in code (`src/extractor.py`):**
1. `_parse_json()`'s balanced-brace rescue started at the *first* `{` and `break` on the
   first non-decodable candidate — a brace fragment inside reasoning/fence text *before*
   the real JSON could defeat the whole parse.
2. Empty or truncated `content` (thinking ate the budget) produced a generic
   "model returned non-JSON" with **no hint about `max_tokens`**.
3. `model.max_tokens` default was 4096 — little headroom for thinking + a ~45-field
   invoice JSON, so real invoices risked `finish_reason=length` / empty content.

**Fix (minimal, `src/extractor.py` + a config default):**
1. `_parse_json()` now scans **every** balanced JSON object in the reply via
   `JSONDecoder.raw_decode` and returns the best-scoring one (largest span + bonus for
   invoice-looking keys) — immune to reasoning text, fences, and brace fragments before /
   after / inside the reply.
2. `_chat_json()` reads `reasoning_content` (direct attr or SDK `model_extra`), records
   `reasoning_chars` in `meta`, and raises **actionable errors** for the two real failure
   modes — thinking-only/empty content and truncation (`finish_reason=length`) — each one
   telling the operator to raise `model.max_tokens` or disable thinking.
3. `model.max_tokens` default raised 4096 → **8192** (`src/config.py`, extractor
   fallback, `config.json`, `config.example.json`, README). The `response_format`→fallback
   behaviour is unchanged and was proven end-to-end against the reasoning emulator.

**Scope discipline:** only `src/extractor.py` logic + `max_tokens` defaults changed. No
Drive, filtering, 25 KB gate, dedupe, MCP, Excel, prompt schema, or validator changes.
The office PC's `config.json` should set `"max_tokens": 8192` (it still says 4096).

## 4. WHY THE FIX IS CORRECT (not a guess)

- Confirmed against Google's official `files.list` parameter semantics and the
  shared-drive support guide (`supportsAllDrives` required; `includeItemsFromAllDrives`
  gates shared-drive items; `corpora=drive` + `driveId` = exact single shared drive).
- Param-construction and forwarding are covered by unit + pipeline tests that record the
  exact kwargs sent by `main.py` → `DriveClient.list_folder` → the API call.

## 5. TESTS ACTUALLY EXECUTED (this machine, 2026-09-23)

```
python -m unittest discover -s tests -t .
Ran 166 tests in ~24 s  →  OK  (was 145 before the drive fix; 157 after it)
```

New tests added this audit:

| Test | Proves |
|---|---|
| `list_always_sends_shared_drive_booleans` | `supportsAllDrives`+`includeItemsFromAllDrives` now sent on every listing |
| `list_forwards_corpora_and_drive_id` | `corpora="drive"`, `driveId=<id>` reach the API call with correct parent query |
| `list_drive_corpora_requires_drive_id` | Config misuse fails fast, not silently |
| `config` corpora validation (4) | defaults empty; `drive`+id accepted; `drive` without id rejected; invalid value rejected |
| `main_forwards_shared_drive_params_from_config` | Real `main.main()` passes `corpora`/`drive_id` to the drive layer when configured |
| `main_omits_shared_drive_params_when_not_configured` | Plain My-Drive configs unchanged |
| `test_response_format_fallback_with_reasoning_extracts_end_to_end` | 400 on `response_format` → retry without it → reasoning **+** content both parsed; full `extract()` path succeeds (2 requests, `reasoning_chars>0`) |
| `test_reasoning_only_content_gives_actionable_error` | thinking-only reply → clear "raise model.max_tokens / disable thinking" error (not generic "non-JSON") |
| `test_truncated_json_surfaces_finish_reason` | `finish_reason=length` + incomplete JSON → truncation error naming `max_tokens` |
| `JsonParseRobustnessTest` (6) | parser ignores reasoning text, brace fragments, fences, trailing text; picks the real invoice object over smaller fragments; rejects truncated JSON |

Pre-existing suite (still green) re-verifies the **downstream pipeline** fed by discovery:
end-to-end filter (small / blank / logo / banner / non-invoice / valid), downloads,
SHA-256 + invoice-fingerprint dedupe (RUN1 → RUN2 → RUN3 idempotency), Qwen extraction,
strict JSON, validation, MCP (real JSON-RPC stdio subprocess) → `details` + line items +
processed-IDs, Excel template safety (locked-file handling), and audit persistence.

## 6. STATUS BY QUESTION

| Question | Answer | Basis |
|---|---|---|
| Primary issue: why 0 files? | Missing Shared-Drive API params (`supportsAllDrives` / `includeItemsFromAllDrives`; now `corpora=drive`+`driveId`) | Code trace + official API docs |
| Is the issue fixable? | YES | Fix implemented |
| Is the proposed fix verified? | YES (code-level + 166-test suite) | same suite, new param + Qwen tests |
| Was the fix implemented? | YES | diff in §3 |
| Does Drive discovery now work? | **YES (wired & parameter-verified); live scan NOT TESTED on this PC** — SA key/folder exist only on the office PC | hermetic param tests; see §7 |
| Does filtering work? | YES | 16+ e2e filter tests, acceptance scenarios |
| Does download work? | YES | drive download tests incl. md5 integrity + size gate |
| Does Qwen extraction work? | YES — `response_format` fallback + reasoning (`reasoning_content`) proven end-to-end against a real in-process OpenAI-compatible HTTP server with the reasoning/truncation behaviours; **live Qwen NOT TESTED here** (LM Studio offline on this PC) | `tests/test_extractor.py` (incl. 4 new tests) |
| Does JSON work? | YES — parser scans every candidate object (reasoning/fence/fragment-proof), truncated output rejected with an actionable error | fenced/ragged/non-JSON + `JsonParseRobustnessTest` (6) |
| Does MCP work? | YES | JSON-RPC stdio server + subprocess round-trip tests |
| Does Excel dataset creation work? | YES | e2e writes to `details`/line-items/processed-IDs on the real template |
| Does duplicate prevention work? | YES | RUN1/2/3 idempotency across both transports |
| Safe to deploy to office PC? | YES with §7 smoke steps | config is portable; nothing machine-specific added |

## 7. REMAINING LIMITATION (must be done on the office PC)

Live Google Drive and live LM Studio **cannot be exercised on this development PC** —
the service-account key is not present and LM Studio/Qwen is not running here (by design
these live on the target machine, per the deployment brief). This is a deployment step,
**not** a project defect. The fix is proven at the API-parameter level and against a
recording end-to-end harness; the office-PC smoke steps below give the final live proof.

```powershell
# 0) Office config: set "max_tokens": 8192 in config.json (Qwen3.5 thinking budget)
python main.py --test-model                              # 1) LM Studio reachable + model loaded
python -m unittest discover -s tests -t .                # 2) suite green on the office PC too
python main.py --dry-run --limit 1                       # 3) first invoice through download+Qwen+JSON (no writes)
python main.py --dry-run                                 # 4) shared-drive scan: expect "Found N supported files" (N > 0) and per-file plan
python main.py --limit 1                                 # 5) first controlled invoice end-to-end (write Excel)
python main.py                                           # 6) full batch
python main.py                                           # 7) re-run: expect new=0 (idempotent)
```

**Qwen-stage live proof (step 3/5):** a healthy file must end with
`extraction_status=ok` and must **not** log `response_format not supported` more than once
(the fallback retry). If a file logs `model returned NO content / truncated` or
`reasoning-only`, raise `model.max_tokens` further (e.g. 16384) or disable thinking in
LM Studio — the error text names the exact knob.

If step 4 still shows `Found 0 supported files`, the next things to check are (in order):
the service account is a **member of the Shared Drive** (not just the folder),
`drive.drive_id` is the **top-level Shared Drive ID**, and `drive.corpora="drive"` plus
the service-account key path are as shown in `config.json`.

## 8. Deliverables

- **Drive fix:** `src/drive_client.py` (shared-drive params), `src/config.py` (validation), `main.py`
  (forwarding), `config.json` (real Shared Drive id), `config.example.json` + README (docs).
- **Qwen fix:** `src/extractor.py` (candidate-scanning parser, `reasoning_content` handling,
  actionable empty/truncated errors), `max_tokens` default 4096 → 8192 across
  `config.py` / `config.json` / `config.example.json` / README.
- Tests: `tests/test_drive_client.py`, `tests/test_config.py`, `tests/test_main_pipeline.py`,
  `tests/test_extractor.py` (4 new Qwen tests), `tests/helpers.py` (reasoning/truncation emulator).
- This report.

---

# FINAL AUDIT REPORT — architecture normalization + JSON→XML fallback (post-change)

## A. CURRENT ARCHITECTURE (actual final flow, Drive → Excel)

```
Google Drive / Shared Drive
   │  DriveClient.list_folder(supportsAllDrives, includeItemsFromAllDrives, corpora=drive, drive_id)   [main.py:148-162]
   ▼
File discovery (sorted) → --limit N truncation → per file:
   file-ID dedupe → 25 KB size gate (Drive metadata)                        [main.py:210,217]
   download + SHA-256 → content-hash dedupe                                 [main.py:228-239]
   ContentFilter: blank / logo-only / non-invoice / REVIEW_REQUIRED         [main.py:243]
   ▼
Extractor.extract()                                                        [src/extractor.py:63]
   _build_messages(): text-PDF → raw text (input prep) | image → base64 data-URI |
                      scanned-PDF → pypdfium2 page renders (input prep only)   [no Python OCR/field extraction]
   JSON PRIMARY → _chat_json (response_format → 400 → retry w/o, retry/backoff,
                              reasoning_content + empty/truncation diagnostics,
                              _parse_json candidate-scan)
      │  fails (malformed / unusable / truncated / schema-invalid reject)
      ▼
   XML FALLBACK → _chat_xml (ONE controlled 2nd request, no response_format,
                             _parse_xml with XXE/ENTITY guard) → SAME canonical object
      │  both fail → ExtractorError → audit extraction_status=failed
   ▼
validate_extraction (deterministic schema/type/date/number + arithmetic)    [src/validator.py]
   → invoice-fingerprint dedupe (InvoiceNo+VendorGST+Date+Total)            [main.py:275-290]
   → decision ok | review | reject (no Excel row for review/reject)
   ▼
Mapper.build_row + build_line_item_rows (only columns that exist in template)[src/excel_mapper.py]
   ▼
open_excel_backend (mcp.enabled → MCP stdio subprocess | else DirectExcelBackend)[src/excel_backend.py]
   → details + Details_LineItems + _DocParser_ProcessedIDs                  [src/mcp/excel_tool.py]
   ▼
ledger (duplicate cache) + audit JSONL/CSV (now incl. source_format, xml_fallback) [main.py:343-367]
   --dry-run: stops before ALL of these writes (extraction/validation still runs)
```

## B. RESPONSIBILITY MATRIX

| Layer | Doing | Not doing |
|---|---|---|
| **Python (orchestration + prep)** | Drive API/params, filtering, size/ext/type checks, dedupe, download, raw-PDF-text read, scanned-PDF page render, base64 encode, build Qwen requests, retry/backoff, parse JSON syntax, parse XML syntax (XXE-guarded), schema/required-field/type/date/number validation, deterministic normalization (₹14,750→14750.0, dates→ISO), state/ledger/audit, MCP+Excel orchestration | **No** OCR, **no** field interpretation, **no** "this probably means total", **no** guessing GSTIN/vendor, **no** semantic repair |
| **Qwen 3.5 9B (LM Studio)** | Understands the invoice, reads layout/labels, extracts invoice no/date/party/GSTIN/taxes/line items/qty/price/discounts/subtotals/grand total, semantic interpretation, produces JSON (primary) or XML (fallback) | Never writes Excel; only answers structured extraction |
| **MCP spreader** | Owns the workbook for the run (`OpenAI`-free JSON-RPC 2.0 stdio subprocess), exposes thin verbs `excel_read_seed / excel_append_invoice / excel_mark_seen / excel_flush / excel_close`; writes only existing columns; preserves other worksheets | No business logic, no schema decisions |
| **Excel template** | Source of truth: header names drive mapping; re-seeded each run for dedupe | Not modified/renamed/reordered |

## C. CHANGES MADE (this audit, nothing else touched)

1. **`src/prompt.py`** — added `SCHEMA_SCALAR_FIELDS` + `SCHEMA_LINEITEM_FIELDS` (single canonical field source shared by JSON schema, XML prompt, and XML parser) and `build_extraction_prompt_xml()` (the fallback instruction block).
2. **`src/extractor.py`**
   - `extract()` orchestrates **JSON primary → XML fallback**: on any JSON-path `ExtractorError`, or when parsed JSON is `reject`-level ("schema-invalid after reasonable handling"), it issues **exactly one** controlled XML re-request (via `_xml_fallback`) and returns the SAME canonical object. meta gains `source_format`, `xml_fallback`, `json_error`.
   - `_chat_xml()`: second request appends the XML instruction; never sends `response_format`; same bounded retry/backoff, empty/truncation/reasoning-only handling.
   - `_consume_response()`: shared normalization (tokens/finish/latency + actionable empty/truncated/reasoning errors) so JSON and XML behave identically.
   - `_parse_xml()`: strips XML fences, **rejects `<!DOCTYPE>`/`<!ENTITY>` outright (XXE/entity-expansion guard)**, maps only canonical tags to the canonical object, ignores unknown tags, tolerates namespaces.
   - `_usable()`: reuses `validate_extraction()` (single source of truth for the reject rule) — deterministic gate only, no guessing.
3. **`main.py` / `src/audit.py`** — audit now records `source_format` and `xml_fallback` per record (2 new columns in the consolidated CSV).
4. **`tests/helpers.py`** — `FakeLMStudioServer(script=[...])` scripts a sequence of replies (e.g. `["bad JSON", "<xml/>"]`) to drive the two-request fallback hermetically.
5. **`README.md`** — documented JSON-primary / XML-fallback behavior and audit fields.
6. **Not changed:** Drive client, filtering, 25 KB gate, dedupe, content filter, MCP transport, Excel mapping/writer, prompt JSON schema fields, validator logic, `--dry-run` / `--limit` semantics, config defaults (max_tokens stays 8192 from the prior audit).

## D. JSON FLOW (primary)

`_chat_json` sends `response_format={"type":"json_object"}`; if LM Studio rejects it (observed live: HTTP 400) it re-issues **without** it once per attempt (`response_format not supported, retrying without it`), with bounded retries + backoff (SDK `max_retries=0`; app owns retries). Reply handled by `_consume_response` (surfaces empty/thinking-only/truncation with an actionable `model.max_tokens` hint), then `_parse_json` scans every balanced JSON object and picks the best (fence/prose-proof). Identified fields flow to validation.

## E. XML FALLBACK FLOW (when and how)

Triggered **only** when the JSON path does not produce a usable canonical object:
1. JSON malformed / non-JSON / unusable / empty / truncated, OR
2. `response_format` rejected **and** the retried reply still unusable, OR
3. JSON parsed but fails validator at reject-level ("schema-invalid after reasonable handling").

Then `_chat_xml` appends one user turn ("re-answer the same invoice as strict XML…") and makes **one** new request (never `response_format`; same retry/backoff; empty/truncation forced the exact same actionable errors). `_parse_xml` strips fences, blocks `<!DOCTYPE>/<!ENTITY>` (XXE), parses with stdlib `ElementTree`, maps canonical tags → canonical object, ignores unknown tags, tolerates namespaces. Downstream validator, dedupe, MCP, and Excel receive the **same canonical shape** — one record per file, never two. If JSON **and** XML both fail → `ExtractorError("Both JSON and XML extraction failed …")` → audit `extraction_status=failed` (never a partial Excel row).

## F. CONNECTIVITY AUDIT (code-level; live requires office PC)

| Link | State |
|---|---|
| Drive→Python | Shared-drive params wired + verified at API-param level (`supportsAllDrives`, `includeItemsFromAllDrives`, `corpora=drive`, `drive_id=0AEzHZDHjdNwFUk9PVA`). Live scan = NOT TESTED here. |
| Python→LM Studio→Qwen | `base_url`/`api_key`/`model` from config; `--test-model` pings chat completions; timeout 300 s; retries bounded. Response-format rejection, reasoning_content, empty-content, finish_reason, malformed output all handled + tested hermetically. Live model id / vision = NOT TESTED here. |
| Python→MCP→Excel | JSON-RPC stdio subprocess round-trip tested over both transports (acceptance scenario RUN1/2/3). |
| Audit/ledger | `source_format`/`xml_fallback` now recorded; ledger is a cache, workbook is the source of truth. |

## G. TEST RESULTS (178, all green — `python -m unittest discover -s tests -t .`, 28 s)

| Area | Result | Evidence |
|---|---|---|
| Drive shared-params | **PASS** | `test_drive_client`, `test_config`, `test_main_pipeline` (param forwarding) |
| Filtering (small/blank/logo/banner/non-invoice/candidate) | **PASS** | `test_content_filter` + e2e acceptance |
| Duplicate 3-layer + rerun idempotency (RUN1/2/3, both transports) | **PASS** | `test_acceptance_scenario` |
| Download + md5/size gate | **PASS** | `test_drive_client` |
| JSON extraction (fallback-on-400, reasoning_content, empty/truncation, parser) | **PASS** | `test_extractor`, `JsonParseRobustnessTest` |
| **XML fallback — malformed JSON → XML → canonical object** | **PASS** | `test_json_parse_failure_triggers_xml_fallback` (2 requests, `source_format=xml`, `xml_fallback=true`, XML instruction appended, validator `decision=ok`, total 11800.0) |
| **XML fallback — schema-invalid JSON → XML** | **PASS** | `test_schema_invalid_json_triggers_xml_fallback` |
| **XML fallback — both fail → failed** | **PASS** | `test_xml_fallback_both_fail_raises` |
| **XXE/safety guard** | **PASS** | `test_xxe_guard_blocks_doctype_in_xml_fallback`, `test_parse_xml_rejects_doctype` |
| **XML parser unit** (fences, namespace, uncertain_fields, line items, malformed, empty) | **PASS** | `XmlParseUnitTest` (7) |
| JSON success keeps `source_format=json`, exactly 1 request | **PASS** | `test_json_success_records_source_format` |
| Excel template preserved; only existing columns written | **PASS** | `test_excel_writer`, `test_excel_mapper`, `test_mcp` |
| MCP both transports | **PASS** | `test_mcp`, acceptance |
| Validator arithmetic/type/date normalization, no fabrication | **PASS** | `test_validator`, `test_utils` |
| Audit fields/CSV | **PASS** | `test_audit` (reads `_AUDIT_FIELDS` dynamically) |
| Failure cases (API 500 after retries, locked file, unreachable model, missing SA key, non-invoice) | **PASS** | negative-path tests (log lines in suite output are these intentional failures) |
| **Live LM Studio / Qwen 3.5 9B / real Drive scan** | **NOT TESTED** | No LM Studio, no SA key, no `E:` project on this dev PC |

## H. KNOWN LIMITATIONS

1. **Vision live test** — scanned-PDF → Qwen-Vision and image → Qwen-Vision are code-verified + hermetically verified (data-URI + rendered pages reach the request), but the office model must actually be a vision-capable build; if not, vision requests fail with clear errors. Must be confirmed live with TEST 5/6.
2. **Live token budget** — `max_tokens=8192` (default) cannot be proven sufficient for the real, multi-page/high-line-count invoices until TEST 2/4 on the office PC. Diagnostic errors now name the exact knob.
3. **XML fallback live behavior** — Qwen3.5's actual XML formatting is unverified (it has never been asked on the office PC); the parser is deliberately tolerant (fences, namespaces, extra tags) so partial deviations degrade to review, not crash.
4. **XML never proven more reliable than tailored JSON here** — by design it is a fallback, not the primary output.
5. **`response_format` rejection cost** — a model that rejects it doubles the chat round rather than the single retry-without interplay already observed.

## I. DATA SAFETY / IDEMPOTENCY

- One record per file: JSON **or** XML maps to one canonical object; there is no path that writes two copies (dedupe gates are reached exactly once in the per-file flow, and the XML request happens before validation/dedupe, never after an append).
- Failed/both-failed extractions → audit `failed`/`rejected`, no Excel row.
- Rerun/idempotency: workbook re-seeded every run (crash-safe), then File-ID → SHA-256 → invoice-fingerprint dedupe; `mark_seen` records skip reasons for skipped files. Restart, retry, or JSON-fail→XML-fallback cannot create duplicates.
- No Drive permissions/files modified; no global packages touched; all tests run on the system interpreter (hermetic, no network), production runs on the office `.venv`.

## J. PRODUCTION READINESS

- **Actually executed & PASSED here:** full 178-test suite; hermetic Drive-param verification; two-request JSON→XML fallback end-to-end through the real OpenAI SDK against the in-process LM Studio emulator; XXE guard; validator arithmetic; MCP round-trip; Excel mapping.
- **Code exists but NOT live-verified:** Qwen 3.5 9B connectivity/id extraction, real Shared-Drive scan, real vision input, real XML output from Qwen, real Excel writes on the office machine.
- **Therefore:** the project is ready to run the **single-invoice controlled test** below. It is **NOT** ready for the 6,500+ file batch until steps 1–4 pass.

## K. RECOMMENDED NEXT TEST (safest first, then a controlled real invoice)

```powershell
# 1) LM Studio + model id (TEST 1)
python main.py --test-model
# 2) controlled ONE invoice: REAL file, download+prep+Qwen+JSON+validation, no writes (TEST 2/4/5/6)
python main.py --dry-run --limit 1
#    - expect extraction_status=ok, source_format=json and no xml_fallback for a healthy file
# 3) force/observe XML fallback live (TEST 3): use one invoice that makes JSON fail,
#    or temporarily set model.max_tokens low; confirm a second request returns XML
#    and the audit row shows source_format=xml, xml_fallback=1
# 4) controlled WRITE on a SAFE COPY of the Excel template (TEST 7): copy the template,
#    point excel.template_path at the copy, run python main.py --limit 1, verify the details row
# 5) re-run the same file (TEST 8): expect new=0 / no duplicate row
# 6) only after 1–5 pass: python main.py --limit 10, then the full batch
```

The first production gate remains: **ONE real invoice** from Drive → download → correct input prep → Qwen JSON → validation → (XML only on JSON failure) → canonical object → Excel copy → correct `details` row → rerun idempotency. Do not run the full Drive dataset before that.