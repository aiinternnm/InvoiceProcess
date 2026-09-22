"""Configuration loading with defaults and validation."""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

from .utils import ensure_dir, parse_drive_id

_DEFAULTS: Dict[str, Any] = {
    "drive": {
        "service_account_json": "credentials/service_account.json",
        "folder_id": "",
        "folder_url": "",
        "recursive": True,
        "allowed_extensions": [".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif"],
        "download_dir": "data/downloads",
        "max_file_size_mb": 50,
        "delete_temp_files": True,
        "verify_download_md5": False,
    },
    "excel": {
        "template_path": "Vendor_Invoice_Details_TEMPLATE_blank.xlsx",
        "target_sheet": "details",
        "lineitems_sheet": "Details_LineItems",
        "processed_ids_sheet": "_DocParser_ProcessedIDs",
        "backup_dir": "data/backups",
        "write_line_items": True,
        "write_processed_ids": True,
        "flush_every_n": 1,
    },
    "model": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "model": "qwen3.5-9b-instruct",
        "temperature": 0,
        "max_tokens": 4096,
        "timeout_seconds": 300,
        "vision_enabled": True,
        "min_pdf_text_chars": 40,
        "max_pdf_pages_as_images": 6,
        "retries": 2,
        "backoff_seconds": 5,
    },
    "extraction": {
        "require_invoice_number_to_append": True,
        "flag_uncertain_fields": True,
        "audit_low_confidence_threshold": 0.6,
        "prompt_override_file": "",
    },
    "duplicates": {
        "ledger_file": "data/state/processed_ledger.jsonl",
        "check_invoice_fingerprint": True,
        "amount_rounding": 2,
    },
    "filtering": {
        "min_file_size_kb": 25,
        "enable_content_filter": True,
        "enable_logo_only_filter": True,
        "min_invoice_signal_score": 2,
        "max_analysis_dimension": 400,
        "texture_dimension": 600,
        "blank_ink_threshold": 0.003,
        "logo_max_ink_ratio": 0.10,
        "logo_max_spread": 0.45,
        "logo_max_bbox_area": 0.25,
        "logo_max_textiness": 0.60,
        "banner_min_saturation": 55,
        "letterhead_max_words": 40,
        "max_pdf_pages_to_inspect": 3,
        "min_pdf_text_chars": 40,
    },
    "audit": {
        "dir": "data/audit",
        "consolidated_csv": "data/audit/audit_all_runs.csv",
    },
    "column_map": {},
    "mcp": {
        "enabled": False,
        "transport": "stdio",
        "server_module": "src.mcp.server",
        "timeout_seconds": 60,
    },
}


def _deep_merge(base: Dict, override: Dict) -> Dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str = "config.json") -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        user_cfg = json.load(fh)
    cfg = _deep_merge(_DEFAULTS, user_cfg)

    # rebase relative paths against the directory that contains config.json
    base = os.path.dirname(os.path.abspath(path))

    def rebase(p: str) -> str:
        if not p:
            return p
        if os.path.isabs(p):
            return p
        return os.path.normpath(os.path.join(base, p))

    for section in ("drive", "excel", "duplicates", "audit"):
        for key in ("service_account_json", "template_path", "backup_dir", "ledger_file", "dir", "consolidated_csv", "download_dir", "prompt_override_file"):
            if key in cfg.get(section, {}):
                cfg[section][key] = rebase(cfg[section][key])

    _validate(cfg)
    for d in (cfg["drive"]["download_dir"], cfg["excel"]["backup_dir"],
              cfg["audit"]["dir"], os.path.dirname(cfg["duplicates"]["ledger_file"])):
        ensure_dir(d)

    if cfg["drive"].get("folder_url") and not cfg["drive"].get("folder_id"):
        cfg["drive"]["folder_id"] = parse_drive_id(cfg["drive"]["folder_url"])
    return cfg


def _validate(cfg: Dict[str, Any]) -> None:
    if not cfg["drive"].get("folder_id") and not cfg["drive"].get("folder_url"):
        raise ValueError("config.json: provide drive.folder_id or drive.folder_url.")
    if not os.path.exists(cfg["excel"]["template_path"]):
        raise FileNotFoundError(f"Excel template not found: {cfg['excel']['template_path']}")
    placeholder = "YOUR_NGROK_URL"
    if placeholder in cfg["model"]["base_url"]:
        raise ValueError(
            "config.json: model.base_url still contains the placeholder. "
            "For a local LM Studio install set http://localhost:1234/v1 (no ngrok needed); "
            "use a ngrok URL only when the client machine cannot reach LM Studio directly."
        )
    if not cfg["model"].get("model"):
        raise ValueError("config.json: model.model is empty. Set the exact model id loaded in LM Studio.")
    if cfg["mcp"].get("enabled"):
        transport = str(cfg["mcp"].get("transport", "stdio")).lower()
        if transport not in ("stdio", "inproc"):
            raise ValueError(f"config.json: mcp.transport must be 'stdio' or 'inproc' (got {transport!r}).")