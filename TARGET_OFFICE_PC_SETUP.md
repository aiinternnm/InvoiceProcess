# TARGET OFFICE PC — Setup Guide

Setup order for running the pipeline where the project ZIP was extracted and Qwen is
already loaded in LM Studio. Run every command from the extracted project folder.

---

## The 6 steps

### Step 1 — Install Python dependencies

Check Python first:

```
python --version
```

It must be 3.10+ (3.11 / 3.12 recommended). Then install everything:

```
python -m pip install -r requirements.txt
```

### Step 2 — Prepare LM Studio (2 things only)

1. In the **Developer** tab click **Start Server**. It must show `http://localhost:1234`
   with status *Serving*.
2. Copy your exact model id: **My Models → right-click your Qwen GGUF → Copy Id**
   (something like `qwen3.5-9b-instruct:latest`).

### Step 3 — Edit `config.json` (2 edits only)

Open `config.json` in a text editor:

- Set `"model"` . `model` = the **exact id you copied in Step 2**
  (the shipped value `qwen3.5-9b-instruct` is a placeholder — a mismatch here is the #1
  cause of exit-code-2 failures).
- Leave `base_url` = `http://localhost:1234/v1` and `api_key` = `lm-studio`.

Everything else (Drive folder id, Excel template path, sheets, MCP) is already correctly set.

### Step 4 — Google Drive service account

Full instructions below in [Step 4 in detail](#step-4-in-detail-google-drive-service-account).

### Step 5 — Verify the model link

```
python main.py --test-model
```

Expected: prints the model and works, exit code 0.
If it says unreachable: Server not started, wrong model id, or wrong port.

### Step 6 — Run

Close any open copy of the Excel template, then:

```
python main.py
```

Open `data\backups\<timestamp>.xlsx` to see the appended invoice rows
(`details` + `Details_LineItems` + `_DocParser_ProcessedIDs`).

Optional first pass (writes nothing to Excel — scans, filters, extracts, audits):

```
python main.py --dry-run
```

Then check `data\audit\audit_all_runs.csv` for per-file statuses:
`PROCESSED` = sent to Qwen, `REVIEW_REQUIRED` = flagged for manual review (normal at start),
`SKIPPED_*` = filtered out with a reason.

---

## Step 4 in detail — Google Drive service account

### Part A — Create the service-account key file

1. Go to https://console.cloud.google.com and create a project (name it anything).
2. **APIs & Services → Library** → search **Google Drive API** → **Enable**.
3. **IAM & Admin → Service Accounts → Create Service Account** → give it a name
   (no roles needed) → **Create and Continue → Done**.
4. Click the service account you just created → **Keys** tab → **Add Key →
   Create new key → JSON** → a `.json` file downloads.
5. **Rename it to `service_account.json`** and place it at:
   `credentials\service_account.json` inside the extracted project folder.

### Part B — Share the folder with that service account

6. Copy the service-account email from the same console page
   (looks like `your-name@your-project.iam.gserviceaccount.com`).
7. In Google Drive, **right-click the invoices folder → Share → add that SA email →
   set permission to Viewer → Send**.

Share **only the source folder**. Never share your whole "My Drive" as the SA.

### Security — does this make Drive access risky?

- **No personal-Drive leak.** A service account is a separate identity. It sees only
  what you explicitly share with it. Your personal files, Gmail, and Docs stay invisible.
- **Read-only by design.** The pipeline authenticates with the `drive.readonly` scope, so
  it can only list + download files already shared with that account. It cannot edit,
  rename, delete, or upload.
- **The only exposure is the one folder you share.** Share just the source folder
  (Viewer access). Even if the key were stolen, an attacker could read only that folder.
- **The key is revocable.** Delete or disable the key (or the whole service account)
  anytime in the cloud console.
- **The key is never committed.** It is gitignored and was excluded from the ZIP, so it
  exists only on the machine you placed it on.

### Step 4 troubleshooting

- If `--test-model` passes but the pipeline reports a Drive "file not found", sharing can
  take a few seconds to propagate — re-share the folder or wait and rerun.
- If the SA email does not appear in the share dialog, double check the spelling and that
  you typed the full `...@....iam.gserviceaccount.com` address.

---

## Exit codes and troubleshooting

| Code | Meaning | Fix |
|---|---|---|
| 0 | Success | — |
| 2 | Qwen/LM Studio unreachable | Start Server; verify `model.model` and `base_url` |
| 3 | Drive failure | Check `credentials\service_account.json`, folder sharing, folder id |
| 4 | Workbook save failed | The template was open in Excel — close it and rerun |

Governing docs: `README.md` (full deployment reference) and `PROJECT_FINAL_AUDIT.md`.