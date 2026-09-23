"""Tests for src/config.py: defaults merge, path rebasing, validation."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config  # noqa: E402
from tests.helpers import make_workbook  # noqa: E402


def _minimal_cfg(tmp, wb_path, base_url="http://localhost:1234/v1"):
    wb = os.path.abspath(wb_path)
    cfg = {
        "drive": {"folder_id": "folder123", "download_dir": "data/downloads"},
        "excel": {"template_path": wb},
        "model": {"base_url": base_url, "model": "qwen-test"},
        "duplicates": {"ledger_file": "data/state/ledger.jsonl"},
        "audit": {"dir": "data/audit"},
    }
    p = os.path.join(tmp, "cfg.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return p


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wb = make_workbook(os.path.join(self.tmp.name, "template.xlsx"))

    def test_defaults_merged(self):
        p = _minimal_cfg(self.tmp.name, self.wb)
        cfg = load_config(p)
        self.assertEqual(cfg["model"]["base_url"], "http://localhost:1234/v1")
        self.assertEqual(cfg["model"]["max_tokens"], 8192)
        self.assertEqual(cfg["excel"]["flush_every_n"], 1)
        self.assertEqual(cfg["filtering"]["min_file_size_kb"], 25)
        self.assertTrue(cfg["drive"]["recursive"])
        # MCP defaults: disabled unless explicitly enabled in the shipped config
        self.assertFalse(cfg["mcp"]["enabled"])
        self.assertEqual(cfg["mcp"]["transport"], "stdio")
        self.assertEqual(cfg["mcp"]["server_module"], "src.mcp.server")
        self.assertEqual(cfg["mcp"]["timeout_seconds"], 60)

    def test_mcp_overrides_and_transport_validation(self):
        cfg = {"drive": {"folder_id": "x"},
               "excel": {"template_path": os.path.abspath(self.wb)},
               "model": {"base_url": "http://localhost:1234/v1", "model": "m"},
               "mcp": {"enabled": True, "transport": "inproc"}}
        p = os.path.join(self.tmp.name, "mcp.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        loaded = load_config(p)
        self.assertTrue(loaded["mcp"]["enabled"])
        self.assertEqual(loaded["mcp"]["transport"], "inproc")

        cfg["mcp"]["transport"] = "tcp"
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(ValueError):
            load_config(p)

    def test_relative_paths_rebased_to_config(self):
        p = _minimal_cfg(self.tmp.name, self.wb)
        cfg = load_config(p)
        self.assertTrue(os.path.isabs(cfg["excel"]["template_path"]))
        self.assertTrue(os.path.isabs(cfg["audit"]["dir"]))
        self.assertTrue(os.path.isabs(cfg["duplicates"]["ledger_file"]))
        self.assertEqual(cfg["excel"]["template_path"], os.path.abspath(self.wb))

    def test_placeholder_base_url_rejected(self):
        p = _minimal_cfg(self.tmp.name, self.wb,
                         base_url="https://YOUR_NGROK_URL.ngrok.app/v1")
        with self.assertRaises(ValueError):
            load_config(p)

    def test_localhost_url_accepted(self):
        p = _minimal_cfg(self.tmp.name, self.wb)
        cfg = load_config(p)  # must not raise
        self.assertIn("localhost", cfg["model"]["base_url"])

    def test_missing_template_rejected(self):
        cfg = {
            "drive": {"folder_id": "x"},
            "excel": {"template_path": os.path.join(self.tmp.name, "nope.xlsx")},
            "model": {"base_url": "http://localhost:1234/v1", "model": "m"},
        }
        p = os.path.join(self.tmp.name, "bad.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(FileNotFoundError):
            load_config(p)

    def test_missing_folder_rejected(self):
        cfg = {
            "drive": {"folder_id": ""},
            "excel": {"template_path": os.path.abspath(self.wb)},
            "model": {"base_url": "http://localhost:1234/v1", "model": "m"},
        }
        p = os.path.join(self.tmp.name, "nofolder.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        with self.assertRaises(ValueError):
            load_config(p)

    def test_folder_url_infers_folder_id(self):
        cfg = {
            "drive": {"folder_url": "https://drive.google.com/drive/folders/abcDEF123"},
            "excel": {"template_path": os.path.abspath(self.wb)},
            "model": {"base_url": "http://localhost:1234/v1", "model": "m"},
        }
        p = os.path.join(self.tmp.name, "inf.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        cfg = load_config(p)
        self.assertEqual(cfg["drive"]["folder_id"], "abcDEF123")

    def test_output_directories_created(self):
        p = _minimal_cfg(self.tmp.name, self.wb)
        load_config(p)
        for d in ("downloads", os.path.join("state"), "audit", "backups"):
            self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "data", d)),
                            d)

    def _drive_cfg(self, extra):
        cfg = {
            "drive": dict({"folder_id": "folder123"}, **extra),
            "excel": {"template_path": os.path.abspath(self.wb)},
            "model": {"base_url": "http://localhost:1234/v1", "model": "m"},
        }
        p = os.path.join(self.tmp.name, "shared.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return p

    def test_corpora_drive_without_drive_id_rejected(self):
        p = self._drive_cfg({"corpora": "drive"})
        with self.assertRaises(ValueError):
            load_config(p)

    def test_corpora_drive_with_drive_id_accepted(self):
        p = self._drive_cfg({"corpora": "drive", "drive_id": "0A-SHARED"})
        cfg = load_config(p)
        self.assertEqual(cfg["drive"]["corpora"], "drive")
        self.assertEqual(cfg["drive"]["drive_id"], "0A-SHARED")

    def test_invalid_corpora_rejected(self):
        p = self._drive_cfg({"corpora": "nonsense"})
        with self.assertRaises(ValueError):
            load_config(p)

    def test_corpora_defaults_empty(self):
        p = _minimal_cfg(self.tmp.name, self.wb)
        cfg = load_config(p)
        self.assertEqual(cfg["drive"]["corpora"], "")
        self.assertEqual(cfg["drive"]["drive_id"], "")


if __name__ == "__main__":
    unittest.main()