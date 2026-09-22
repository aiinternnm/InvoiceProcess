"""Google Drive ingestion via a service account.

Two responsibilities kept separate:
  * DriveClient.list_folder() -> metadata for supported files (recursive).
  * DriveClient.download() -> write bytes of a file to a temp path.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from .utils import ensure_dir, md5_file, parse_drive_id

log = logging.getLogger("invoice_pipeline.drive")

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

_FIELDS = ("files(id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,owners,"
           "lastModifyingUser,parents,trashed,webViewLink)")

# Drive's md5Checksum is the whole-file MD5 for files up to this size; for larger
# files it can be partial/absent, so integrity verification is only enforced here.
_MD5_RELIABLE_BYTES = 5 * 1024 * 1024


class DriveError(Exception):
    pass


class DriveClient:
    def __init__(self, service_account_json: str):
        self._sa_path = service_account_json
        self._service = None
        self._creds = None

    # ---- auth -------------------------------------------------------------
    def _get_service(self):
        if self._service is not None:
            return self._service
        if not os.path.exists(self._sa_path):
            raise FileNotFoundError(
                f"Service account JSON not found at {self._sa_path!r}. "
                "Add your Google Cloud service-account key there (see README 'Google Drive setup')."
            )
        creds = Credentials.from_service_account_file(self._sa_path, scopes=SCOPES)
        if creds and creds.token and creds.expired:
            creds.refresh(Request())
        self._creds = creds
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        log.info("Authenticated to Google Drive via service account (email=%s)", getattr(creds, "service_account_email", "?"))
        return self._service

    def _client_email(self) -> str:
        return getattr(self._creds, "service_account_email", "") if self._creds else ""

    # ---- listing ----------------------------------------------------------
    def list_folder(self, folder_id: str, recursive: bool = True,
                    allowed_extensions: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        svc = self._get_service()
        allowed = [e.lower() for e in (allowed_extensions or [])]
        out: List[Dict[str, Any]] = []

        def walk(current: str, depth: int) -> None:
            q = f"'{current}' in parents and trashed=false"
            token = None
            while True:
                try:
                    resp = svc.files().list(
                        q=q, pageSize=1000, fields=f"nextPageToken,{_FIELDS}", pageToken=token
                    ).execute()
                except HttpError as exc:
                    raise DriveError(f"files.list failed for {current}: {exc}") from exc
                for item in resp.get("files", []):
                    mime = item.get("mimeType", "")
                    name = item.get("name", "")
                    if mime == "application/vnd.google-apps.folder":
                        if recursive:
                            walk(item["id"], depth + 1)
                        continue
                    if _is_supported(name, mime, allowed):
                        item["_drive_path"] = current
                        item["_depth"] = depth
                        out.append(item)
                token = resp.get("nextPageToken")
                if not token:
                    break

        walk(folder_id, 0)
        return out

    # ---- download ---------------------------------------------------------
    def download(self, file: Dict[str, Any], dest_dir: str,
                 max_size_mb: Optional[int] = None,
                 verify_md5: bool = False) -> str:
        svc = self._get_service()
        file_id = file["id"]
        ensure_dir(dest_dir)
        size = file.get("size")
        if size:
            size = int(size)
            if max_size_mb and size > max_size_mb * 1024 * 1024:
                raise DriveError(
                    f"{file['name']}: size {size / 1048576:.1f} MB exceeds max_file_size_mb={max_size_mb}"
                )
        dest_name = file.get("_download_name") or f"{file_id}_{_safe_name(file.get('name', 'file'))}"
        dest = os.path.join(dest_dir, dest_name)
        expected_md5 = (file.get("md5Checksum") or "").lower()
        can_verify = bool(verify_md5 and expected_md5 and size and int(size) <= _MD5_RELIABLE_BYTES)
        if os.path.exists(dest) and os.path.getsize(dest) == (size or 0):
            if can_verify and md5_file(dest).lower() != expected_md5:
                log.warning("Stale/corrupt cached download detected for %s: re-downloading.",
                            file.get("name"))
                try:
                    os.remove(dest)
                except OSError:
                    pass
            else:
                return dest
        try:
            req = svc.files().get_media(fileId=file_id)
            with open(dest, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, req, chunksize=1024 * 1024)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        pct = int(status.progress() * 100)
                        if pct % 25 == 0:
                            log.debug("%s download %d%%", file.get("name"), pct)
        except HttpError as exc:
            raise DriveError(f"download failed for {file.get('name')} ({file_id}): {exc}") from exc
        if can_verify:
            got = md5_file(dest).lower()
            if got != expected_md5:
                log.warning("Integrity check failed for %s: md5Checksum=%s got=%s",
                            file.get("name"), expected_md5, got)
                raise DriveError(
                    f"Downloaded bytes failed integrity check for {file.get('name')} "
                    f"(drive md5={expected_md5} vs downloaded {got}). The file may be corrupt; "
                    "skipping this file and continuing the batch."
                )
        return dest

    # ---- convenience ------------------------------------------------------
    def resolve_folder_id(self, link_or_id: str) -> str:
        return parse_drive_id(link_or_id)

    def client_email(self) -> str:
        return self._client_email()


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return keep[:120]


def _is_supported(name: str, mime: str, allowed_extensions: List[str]) -> bool:
    lower_name = name.lower()
    for ext in allowed_extensions:
        if lower_name.endswith(ext):
            return True
    mime = (mime or "").lower()
    return mime.startswith("application/pdf") or mime.startswith("image/")


def owner_of(file: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Best-effort owner/sender metadata (may be None for service account view)."""
    lmu = file.get("lastModifyingUser") or {}
    owners = file.get("owners") or [{}]
    owner = owners[0] if owners else {}
    name = owner.get("displayName") or lmu.get("displayName")
    email = owner.get("emailAddress") or lmu.get("emailAddress")
    return {"owner_name": name or None, "owner_email": email or None}