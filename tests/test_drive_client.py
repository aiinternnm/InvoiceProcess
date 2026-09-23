"""Tests for src/drive_client.py using a fully mocked Google API surface."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.drive_client import DriveClient, DriveError, _is_supported, owner_of  # noqa: E402


class _FakeRequest:
    def __init__(self, page):
        self.page = page

    def execute(self):
        return self.page


class _FakeFiles:
    def __init__(self, pages, media=None):
        self.pages = list(pages)
        self.media = media or {}
        self.calls = []
        self.media_calls = 0

    def list(self, q=None, pageSize=None, fields=None, pageToken=None,
             supportsAllDrives=None, includeItemsFromAllDrives=None,
             corpora=None, driveId=None):
        self.calls.append({
            "q": q, "pageToken": pageToken,
            "supportsAllDrives": supportsAllDrives,
            "includeItemsFromAllDrives": includeItemsFromAllDrives,
            "corpora": corpora, "driveId": driveId,
        })
        page = self.pages.pop(0) if self.pages else {"files": [], "nextPageToken": None}
        return _FakeRequest(page)

    def get_media(self, fileId=None):
        self.media_calls += 1
        return _FakeMediaRequest(self.media.get(fileId, b""))


class _FakeMediaRequest:
    def __init__(self, payload):
        self.payload = payload


class _FakeSvc:
    def __init__(self, pages, media=None):
        self.files_api = _FakeFiles(pages, media)

    def files(self):
        return self.files_api


class _FakeDownloader:
    def __init__(self, fh, req, chunksize=None):
        self.fh = fh
        self.req = req
        self._done = False

    def next_chunk(self):
        if not self._done:
            self._done = True
            self.fh.write(self.req.payload if self.req else b"")
        return (None, True)


class DriveClientListTest(unittest.TestCase):
    def _client(self, svc=None, sa_path="x.json"):
        c = DriveClient(sa_path)
        if svc is not None:
            c._service = svc
        return c

    def test_lists_only_supported_files_and_filters_folders(self):
        pages = [{
            "nextPageToken": "tok",
            "files": [
                {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf",
                 "size": "1000", "owners": [{"displayName": "A"}], "trashed": False},
                {"id": "f2", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
            ],
        }, {
            "nextPageToken": None,
            "files": [
                {"id": "f3", "name": "b.PNG", "mimeType": "image/png"},
                {"id": "f4", "name": "notes.txt", "mimeType": "text/plain"},
            ],
        }]
        svc = _FakeSvc(pages)
        c = self._client(svc)
        files = c.list_folder("ROOT", recursive=True,
                              allowed_extensions=[".pdf", ".png"])
        self.assertEqual([f["id"] for f in files], ["f1", "f3"])
        self.assertTrue(any("'ROOT' in parents" in c["q"] for c in svc.files_api.calls))

    def test_list_always_sends_shared_drive_booleans(self):
        pages = [{"nextPageToken": None, "files": [
            {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "size": "1000",
             "owners": [{"displayName": "A"}], "trashed": False}]}]
        svc = _FakeSvc(pages)
        c = self._client(svc)
        c.list_folder("ROOT")
        call = svc.files_api.calls[0]
        self.assertTrue(call["supportsAllDrives"])
        self.assertTrue(call["includeItemsFromAllDrives"])
        self.assertIsNone(call["corpora"])
        self.assertIsNone(call["driveId"])

    def test_list_forwards_corpora_and_drive_id(self):
        pages = [{"nextPageToken": None, "files": [
            {"id": "f1", "name": "a.pdf", "mimeType": "application/pdf", "size": "1000",
             "owners": [{"displayName": "A"}], "trashed": False}]}]
        svc = _FakeSvc(pages)
        c = self._client(svc)
        c.list_folder("SUBFOLDER", corpora="drive", drive_id="0A-SHARED")
        call = svc.files_api.calls[0]
        self.assertEqual(call["corpora"], "drive")
        self.assertEqual(call["driveId"], "0A-SHARED")
        self.assertTrue(any("'SUBFOLDER' in parents" in c2["q"] for c2 in svc.files_api.calls))

    def test_list_drive_corpora_requires_drive_id(self):
        svc = _FakeSvc([{"nextPageToken": None, "files": []}])
        c = self._client(svc)
        with self.assertRaises(DriveError):
            c.list_folder("ROOT", corpora="drive", drive_id=None)

    def test_supported_by_mime_when_extension_unknown(self):
        self.assertTrue(_is_supported("noext", "application/pdf", []))
        self.assertTrue(_is_supported("noext", "image/jpeg", []))
        self.assertFalse(_is_supported("x.doc", "application/msword", []))

    def test_missing_service_account_gives_clear_error(self):
        c = DriveClient(os.path.join(tempfile.gettempdir(), "definitely_missing_key.json"))
        with self.assertRaises(FileNotFoundError) as ctx:
            c.list_folder("X")
        self.assertIn("Service account JSON not found", str(ctx.exception))


class DriveClientDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _client(self, media=None):
        c = DriveClient("sa.json")
        c._service = _FakeSvc([], media)
        return c

    def test_download_writes_bytes_and_returns_path(self):
        c = self._client(media={"f1": b"pdf-bytes"})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            dest = c.download({"id": "f1", "name": "inv.pdf", "size": "9"},
                              self.tmp.name, max_size_mb=10)
        self.assertTrue(os.path.exists(dest))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"pdf-bytes")
        self.assertEqual(c._service.files_api.media_calls, 1)

    def test_download_skips_rewrite_when_same_size(self):
        p = os.path.join(self.tmp.name, "f1_inv.pdf")
        with open(p, "wb") as fh:
            fh.write(b"pdf-bytes")
        c = self._client(media={"f1": b"pdf-bytes"})
        returned = c.download({"id": "f1", "name": "inv.pdf", "size": "9",
                               "_download_name": "f1_inv.pdf"}, self.tmp.name)
        self.assertEqual(p, returned)
        self.assertEqual(c._service.files_api.media_calls, 0)

    def test_download_enforces_max_size(self):
        c = self._client()
        with self.assertRaises(DriveError):
            c.download({"id": "big", "name": "huge.pdf", "size": str(20 * 1024 * 1024)},
                       self.tmp.name, max_size_mb=5)

    def test_download_sanitises_filename(self):
        c = self._client(media={"f1": b"pdf-bytes"})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            dest = c.download({"id": "f1", "name": "my invoice (2).pdf", "size": "9"},
                              self.tmp.name)
        self.assertTrue(os.path.basename(dest).startswith("f1_my_invoice"))
        self.assertNotIn("(", os.path.basename(dest))

    def test_download_verifies_md5_on_success(self):
        import hashlib
        payload = b"%PDF-1.4\nfake pdf content bytes\n"
        expected = hashlib.md5(payload).hexdigest()
        c = self._client(media={"f1": payload})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            dest = c.download({"id": "f1", "name": "inv.pdf", "size": str(len(payload)),
                               "md5Checksum": expected}, self.tmp.name, verify_md5=True)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), payload)

    def test_download_md5_mismatch_raises_drive_error(self):
        import hashlib
        payload = b"%PDF-1.4\nfake pdf content bytes\n"
        wrong = hashlib.md5(b"totally different bytes").hexdigest()
        c = self._client(media={"f1": payload})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            with self.assertRaises(DriveError):
                c.download({"id": "f1", "name": "inv.pdf", "size": str(len(payload)),
                            "md5Checksum": wrong}, self.tmp.name, verify_md5=True)

    def test_download_md5_opts_out_when_disabled(self):
        c = self._client(media={"f1": b"pdf-bytes"})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            dest = c.download({"id": "f1", "name": "inv.pdf", "size": "9",
                               "md5Checksum": "deadbeef"}, self.tmp.name, verify_md5=False)
        self.assertTrue(os.path.exists(dest))

    def test_stale_cached_download_redownloaded_when_md5_mismatch(self):
        import hashlib
        payload = b"%PDF-1.4\nfake replacement content\n"
        expected = hashlib.md5(payload).hexdigest()
        stale = os.path.join(self.tmp.name, "f1_inv.pdf")
        with open(stale, "wb") as fh:  # same size, different bytes -> stale cache
            fh.write(b"%PDF-1.4\nold stale cached content\n")
        c = self._client(media={"f1": payload})
        with mock.patch("src.drive_client.MediaIoBaseDownload", _FakeDownloader):
            dest = c.download({"id": "f1", "name": "inv.pdf", "size": str(len(payload)),
                               "md5Checksum": expected}, self.tmp.name, verify_md5=True)
        self.assertEqual(dest, stale)
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), payload)


class OwnerTest(unittest.TestCase):
    def test_owner_from_owners(self):
        meta = {"owners": [{"displayName": "N", "emailAddress": "n@x.com"}],
                "lastModifyingUser": {"displayName": "M", "emailAddress": "m@x.com"}}
        self.assertEqual(owner_of(meta),
                         {"owner_name": "N", "owner_email": "n@x.com"})

    def test_owner_falls_back_to_last_modifier(self):
        meta = {"owners": [], "lastModifyingUser": {"displayName": "M", "emailAddress": "m@x.com"}}
        self.assertEqual(owner_of(meta), {"owner_name": "M", "owner_email": "m@x.com"})

    def test_no_owner_returns_none(self):
        self.assertEqual(owner_of({}), {"owner_name": None, "owner_email": None})


if __name__ == "__main__":
    unittest.main()