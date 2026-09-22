"""Shared helpers: file hashing, date/number parsing, path utilities."""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import re
from typing import Optional

log = logging.getLogger("invoice_pipeline")


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_FOLDER_ID_RE = re.compile(r"folders/([A-Za-z0-9_-]+)")
_FILE_ID_RE = re.compile(r"file/d/([A-Za-z0-9_-]+)")


def parse_drive_id(link_or_id: str) -> str:
    link_or_id = (link_or_id or "").strip()
    if not link_or_id:
        raise ValueError("Empty Drive link / id provided.")
    m = _FOLDER_ID_RE.search(link_or_id)
    if m:
        return m.group(1)
    m = _FILE_ID_RE.search(link_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", link_or_id):
        return link_or_id
    raise ValueError(f"Could not parse a Drive folder id from: {link_or_id!r}")


def drive_view_link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


_DATE_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})"    # ISO
    r"|^(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})$"  # d/m/y or m/d/y
)


def parse_date(value, dayfirst: bool = True) -> Optional[str]:
    """Normalize a date to 'YYYY-MM-DD'. Returns None when unparseable."""
    if value is None:
        return None
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if not s:
        return None
    # remove weekday suffix like (Tue) or Tue sometimes OCR'd
    s = re.sub(r"\([A-Za-z]{3,}\)\s*$", "", s).strip()
    s = re.sub(r"[A-Za-z]{3,}$", "", s).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y",
                "%Y/%m/%d", "%d-%b-%Y", "%d/%b/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return _dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = _DATE_RE.match(s)
    if m:
        if m.group(1):  # ISO
            try:
                return _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
            except ValueError:
                return None
        d, mo, y = m.group(4), m.group(5), m.group(6)
        d, mo = int(d), int(mo)
        y = int(y)
        if y < 100:
            y += 2000 if y < 50 else 1900
        # dayfirst heuristic: if first token > 12 it must be day
        if d > 12:
            day, month = d, mo
        elif mo > 12:
            day, month = mo, d
        else:
            day, month = (d, mo) if dayfirst else (mo, d)
        try:
            return _dt.datetime(y, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


_NUM_RE = re.compile(r"(?:^|\s|[(\[:])([+-]?\d+(?:\.\d+)?)")


def parse_number(value) -> Optional[float]:
    """Parse a possibly locale-formatted number (₹1,23,456.78 / -500) to float."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 4)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", "").replace("₹", "").replace("Rs", "").replace("rs", "").replace("INR", "").strip()
    # The number token must start at the string start or after a non-word
    # character, so an invoice like "INV-100" is never misread as -100.0.
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return round(float(m.group(1)), 4)
    except ValueError:
        return None


def safe_boolish(value):
    """Map common encodings to 'YES'/'NO' or None."""
    if value is None:
        return None
    s = str(value).strip().upper()
    if s in ("YES", "Y", "TRUE", "RCM", "1"):
        return "YES"
    if s in ("NO", "N", "FALSE", "0"):
        return "NO"
    return s if s else None


def now_iso() -> str:
    return _dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def now_dt() -> _dt.datetime:
    return _dt.datetime.now()