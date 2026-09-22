PLACE YOUR GOOGLE CLOUD SERVICE ACCOUNT KEY HERE
================================================

Step 1 - Create the credentials
  1. Go to https://console.cloud.google.com -> create/select a project.
  2. API & Services -> "ENABLE APIS AND SERVICES" -> enable the
     "Google Drive API".
  3. IAM & Admin -> Service Accounts -> "Create Service Account"
     (name it e.g. invoice-docreader, no role needed).
  4. Click the created account -> "Keys" -> "Add Key" -> "Create new key"
     -> JSON. It downloads a file like:
        invoice-docreader-1234567890ab.json
  5. Rename/copy that file to THIS folder with the exact name:
        service_account.json
     (config.json points to credentials\service_account.json)

Step 2 - Share your Drive folder with the service account
  1. In Google Drive, right-click the folder
     https://drive.google.com/drive/folders/1jeH4_xIUGG89ozhwbq01NfH52iRPbpxi
  2. "Share" -> add the service account email
     (shown on its detail page, e.g. invoice-docreader@PROJECT.iam.gserviceaccount.com).
  3. Give it at least "Viewer" access.

Step 3 - Verify
  python main.py --test-model     # only needs LM Studio running locally on port 1234
  python main.py --dry-run        # scans Drive, extracts, but writes NO Excel/ledger

  NOTE: LM Studio listens on http://localhost:1234 (already configured in config.json).
  ngrok is ONLY needed when running the pipeline from another machine that cannot
  reach your computer's LM Studio directly (see README.md).

The service account can only see files/folders explicitly shared with it.
Your normal Google account is never involved, so nothing else is exposed.