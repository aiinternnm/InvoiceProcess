"""CHEAP PRE-QWEN FILE FILTER.

Runs BEFORE the file is sent to Qwen so we never spend tokens on irrelevant,
tiny, blank, logo-only or non-invoice files.

Pipeline per file:
  Google Drive metadata (size)        -> below threshold?  SKIPPED_SMALL_FILE
  local file content analysis          -> blank / corrupt?   SKIPPED_EMPTY
                                        -> logo/letterhead?  SKIPPED_LOGO_ONLY
                                        -> decorative/banner? SKIPPED_NON_INVOICE
                                        -> weak signals?     REVIEW_REQUIRED (still sent to Qwen, flagged)
                                        -> invoice candidate  PROCESSED (sent to Qwen)

Deliberately conservative: only *clearly* irrelevant files are auto-skipped.
Anything uncertain is classified REVIEW_REQUIRED and still reviewed by Qwen so
legitimate but unusual invoices are never lost.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PIL import Image, ImageFilter, UnidentifiedImageError

log = logging.getLogger("invoice_pipeline.content_filter")

# --------------------------------------------------------------------------
# Audit status codes (see requirements)
# --------------------------------------------------------------------------
STATUS_PROCESSED = "PROCESSED"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_SMALL_FILE = "SKIPPED_SMALL_FILE"
STATUS_EMPTY = "SKIPPED_EMPTY"
STATUS_LOGO_ONLY = "SKIPPED_LOGO_ONLY"
STATUS_NON_INVOICE = "SKIPPED_NON_INVOICE"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_FAILED = "FAILED"

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif"}

# --- invoice indicator keywords (requirement 5) ---------------------------
# Strong signals: presence alone strongly suggests an invoice-like document.
_STRONG_KEYWORDS = (
    "invoice", "credit note", "debit note", "delivery challan", "challan",
    "quotation", "quote", "estimate", "bill", "subtotal", "grand total",
    "total amount", "amount payable", "taxable value", "taxable amount",
    "invoice no", "invoice number",
)
# Weak / contextual signals: only meaningful in combination with each other.
_WEAK_KEYWORDS = (
    "gstin", "gst", "cgst", "sgst", "igst", "hsn", "tax", "total",
    "amount", "vendor", "supplier", "seller", "sold by", "made by",
    "party name", "vendor name", "buyer", "customer", "bill to", "ship to",
    "consignee", "date", "qty", "quantity", "rate", "description", "item",
    "irn", "eway", "e-way",
)

_CURRENCY_RE = re.compile(r"(?:₹|\brs\b|\brs\.|\binr\b)\s*[\d,.]+", re.I)
_AMOUNT_WORD_RE = re.compile(
    r"\b(?:total|subtotal|amount payable|grand total|amount|taxable|net payable)\b", re.I)
_PRICE_LIKE_RE = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}|\.\d{2}", re.I)
_DATE_RE = re.compile(r"\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}|\d{4}[-/]\d{1,2}[-/]\d{1,2}")

_WORD_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9'\-._]{1,})?")


def _bounded(kw: str) -> str:
    """Word-boundary regex for a keyword so 'invoice' never matches 'invoicing'."""
    return r"\b" + re.escape(kw) + r"\b"


@dataclass
class FilterResult:
    decision: str          # 'pass' | 'skip' | 'review'
    status: str            # one of STATUS_*
    reason: str
    score: Optional[float] = None
    signals: Dict[str, Any] = field(default_factory=dict)

    @property
    def should_proceed(self) -> bool:
        return self.decision in ("pass", "review")


class ContentFilter:
    """Deterministic, dependency-light pre-Qwen classifier.

    All tuning values live in the config 'filtering' section (see config.py
    defaults / config.json) so nothing is scattered through the code.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.min_bytes = max(0, int(self.cfg.get("min_file_size_kb", 25) * 1024))
        self.content_filter_on = bool(self.cfg.get("enable_content_filter", True))
        self.logo_filter_on = bool(self.cfg.get("enable_logo_only_filter", True))
        self.min_signal = float(self.cfg.get("min_invoice_signal_score", 2.0))
        self.max_dim = int(self.cfg.get("max_analysis_dimension", 400))
        self.texture_dim = int(self.cfg.get("texture_dimension", 600))
        self.blank_ink = float(self.cfg.get("blank_ink_threshold", 0.003))
        self.logo_max_ink = float(self.cfg.get("logo_max_ink_ratio", 0.10))
        self.logo_max_spread = float(self.cfg.get("logo_max_spread", 0.45))
        self.logo_max_bbox = float(self.cfg.get("logo_max_bbox_area", 0.25))
        self.logo_max_textiness = float(self.cfg.get("logo_max_textiness", 0.60))
        self.banner_sat = float(self.cfg.get("banner_min_saturation", 55.0))
        self.letterhead_max_words = int(self.cfg.get("letterhead_max_words", 40))
        self.max_pdf_pages = int(self.cfg.get("max_pdf_pages_to_inspect", 3))
        self.min_pdf_text_chars = int(self.cfg.get("min_pdf_text_chars", 40))

    # ======================================================================
    # Public API
    # ======================================================================
    def check_size(self, file_size_bytes: Optional[int]) -> Optional[FilterResult]:
        """Requirement 1: size gate before download/processing.

        Exactly MIN_FILE_SIZE_KB is NOT below the threshold.
        """
        try:
            file_size_bytes = int(file_size_bytes)  # Drive may report size as a str
        except (TypeError, ValueError):
            return None
        if file_size_bytes < self.min_bytes:
            kb = file_size_bytes / 1024.0
            return FilterResult(
                "skip", STATUS_SMALL_FILE,
                f"File size below configured {int(self.min_bytes // 1024)} KB threshold "
                f"(size={kb:.1f} KB)",
                score=0.0,
                signals={"file_size_bytes": file_size_bytes, "min_bytes": self.min_bytes},
            )
        return None

    def evaluate(self, file_path: str, mime_type: str,
                 file_size_bytes: Optional[int] = None) -> FilterResult:
        """Full pre-Qwen evaluation (size + content). Returns a FilterResult."""
        size = file_size_bytes
        if size is None:
            try:
                size = os.path.getsize(file_path)
            except OSError:
                size = None
        size_res = self.check_size(size)
        if size_res is not None:
            return size_res

        if not self.content_filter_on:
            return FilterResult("pass", STATUS_PROCESSED, "Content filtering disabled by config (enable_content_filter=false)")

        mime = (mime_type or "").lower()
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if mime == "application/pdf" or ext == ".pdf":
                return self._evaluate_pdf(file_path)
            if mime.startswith("image/") or ext in _IMAGE_EXTS:
                return self._evaluate_image_file(file_path)
        except FilterDecodeError as exc:
            return FilterResult(
                "skip", STATUS_EMPTY,
                f"Unreadable/corrupt file: {exc}", score=0.0,
                signals={"error": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - filtering must be non-fatal
            log.warning("content filter crashed on %s: %s", os.path.basename(file_path), exc)
            return FilterResult("review", STATUS_REVIEW_REQUIRED,
                                f"Filter error ({exc.__class__.__name__}); requiring manual review")
        # Not a PDF / supported image -> let the model decide is pointless; treat as non-invoice.
        return FilterResult("skip", STATUS_NON_INVOICE,
                            "Unsupported file type for invoice extraction")

    # ======================================================================
    # PDF
    # ======================================================================
    def _evaluate_pdf(self, path: str) -> FilterResult:
        text = ""
        try:
            text = self._pdf_text(path)
        except Exception as pdf_err:  # noqa: BLE001
            pages = self._try_render_pages(path)
            if pages is None:
                return FilterResult("skip", STATUS_EMPTY,
                                    f"Unreadable/corrupt PDF file: {pdf_err}", score=0.0,
                                    signals={"error": str(pdf_err)})
            return self._classify_pdf_pages(pages)

        if self._meaningful_text(text):
            return self._classify_pdf_text(text[:200_000])

        # Scanned / image-only PDF -> inspect rendered pages visually.
        pages = self._try_render_pages(path)
        if not pages:
            return FilterResult("review", STATUS_REVIEW_REQUIRED,
                                "PDF has little/no extractable text and could not be rendered; requiring manual review")
        return self._classify_pdf_pages(pages)

    def _classify_pdf_text(self, text: str) -> FilterResult:
        signals = self._text_signals(text)
        lower = text.lower()
        strong_hit = signals["strong"]
        has_amounts = signals["amount_evidence"]
        has_dates = signals["date_evidence"]
        score = signals["score"]
        words = signals["word_count"]

        if strong_hit or (score >= self.min_signal and has_amounts) or (has_amounts and has_dates):
            return FilterResult(
                "pass", STATUS_PROCESSED,
                "Invoice/document candidate (text signals: strong=%s score=%s)",
                score=float(score), signals=signals)

        # Minimal text with no invoice markers -> letterhead / branding only.
        if words <= self.letterhead_max_words and not has_amounts:
            return FilterResult(
                "skip", STATUS_LOGO_ONLY,
                "PDF text comprises only letterhead/branding without invoice data",
                score=float(score), signals=signals)

        return FilterResult(
            "review", STATUS_REVIEW_REQUIRED,
            "Text present but invoice indicators are weak; requiring manual review",
            score=float(score), signals=signals)

    def _classify_pdf_pages(self, pages: List[Image.Image]) -> FilterResult:
        """Aggregate per-page image classification for scanned/image-only PDFs."""
        results: List[FilterResult] = []
        for page in pages:
            results.append(self._classify_image(page))

        if any(r.decision == "pass" for r in results):
            return FilterResult("pass", STATUS_PROCESSED,
                                "Scanned PDF: page(s) show invoice-like content")
        if all(r.status == STATUS_EMPTY for r in results):
            return FilterResult("skip", STATUS_EMPTY,
                                "Empty/blank PDF (no meaningful page content)")
        if all(r.status in (STATUS_LOGO_ONLY, STATUS_NON_INVOICE, STATUS_EMPTY) for r in results):
            top = next((r for r in results if r.status != STATUS_EMPTY), results[0])
            return FilterResult("skip", top.status,
                                "PDF contains only letterhead/logo/branding without invoice data")
        # Mixed / weakly-signalled pages -> do not risk a false negative.
        return FilterResult("review", STATUS_REVIEW_REQUIRED,
                            "Scanned PDF content uncertain (pages: %s); requiring manual review"
                            % ", ".join(r.status for r in results))

    # ======================================================================
    # Images
    # ======================================================================
    def _evaluate_image_file(self, path: str) -> FilterResult:
        try:
            img = Image.open(path)
            img.load()
        except (UnidentifiedImageError, OSError, ValueError):
            raise FilterDecodeError(f"image decoder failed for {os.path.basename(path)}")
        try:
            img = img.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise FilterDecodeError(f"image decode failed for {os.path.basename(path)}: {exc}")
        return self._classify_image(img)

    def _classify_image(self, img: Image.Image) -> FilterResult:
        stats = self._image_stats(img)

        if stats["ink_ratio"] < self.blank_ink:
            return FilterResult(
                "skip", STATUS_EMPTY,
                f"Blank image: no meaningful content detected (ink coverage {stats['ink_ratio']:.2%})",
                score=0.0, signals=stats)

        # Decorative / marketing banner: colourful, spread across the page, not text.
        if (stats["sat_mean"] >= self.banner_sat and stats["ink_ratio"] >= 0.02
                and stats["spread"] >= self.logo_max_spread
                and stats["textiness"] < self.logo_max_textiness):
            return FilterResult(
                "skip", STATUS_NON_INVOICE,
                "Marketing banner / decorative graphic without invoice content",
                score=float(self._image_signal_score(stats)), signals=stats)

        # Logo / letterhead / watermark / seal: small, clustered, non-text ink
        # placed at a page edge/corner (typical of branding).
        if (self.logo_filter_on
                and stats["ink_ratio"] <= self.logo_max_ink
                and stats["spread"] <= self.logo_max_spread
                and stats["bbox_area"] <= self.logo_max_bbox
                and stats["textiness"] < self.logo_max_textiness
                and stats["touches_edge"]):
            return FilterResult(
                "skip", STATUS_LOGO_ONLY,
                "Image contains logo/branding but no meaningful invoice content",
                score=float(self._image_signal_score(stats)), signals=stats)

        score = self._image_signal_score(stats)
        if score >= self.min_signal:
            return FilterResult(
                "pass", STATUS_PROCESSED,
                "Invoice/document candidate (visual content detection)",
                score=float(score), signals=stats)

        return FilterResult(
            "review", STATUS_REVIEW_REQUIRED,
            "Image content uncertain (visual signal score below threshold); requiring manual review",
            score=float(score), signals=stats)

    # ======================================================================
    # Statistics / scoring
    # ======================================================================
    def _image_stats(self, img: Image.Image) -> Dict[str, Any]:
        w, h = img.size
        if w <= 0 or h <= 0:
            return {"ink_ratio": 0.0, "spread": 0.0, "sat_mean": 0.0, "textiness": 0.0,
                    "bbox_area": 0.0, "touches_edge": False, "ink_area": 0,
                    "texture_width": 0, "texture_height": 0, "aspect_ok": False,
                    "width": w, "height": h}

        long_side = max(w, h)
        scale = min(1.0, self.max_dim / long_side) if long_side else 1.0
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        thumb = img.resize((nw, nh), Image.LANCZOS)
        gray = thumb.convert("L")
        gp, tp = gray.load(), thumb.load()
        total = nw * nh

        GRID = 10
        cell_w, cell_h = nw / GRID, nh / GRID
        cell_thresh = max(2, int(total / (GRID * GRID * 200)))
        ink_px = 0
        cells_hit = 0
        min_r, max_r, min_c, max_c = GRID, -1, GRID, -1
        sat_sum = 0
        for gy in range(GRID):
            y0 = int(gy * cell_h)
            y1 = max(y0 + 1, int((gy + 1) * cell_h))
            for gx in range(GRID):
                x0 = int(gx * cell_w)
                x1 = max(x0 + 1, int((gx + 1) * cell_w))
                cnt = 0
                for y in range(y0, y1):
                    for x in range(x0, x1):
                        v = gp[x, y]
                        if 28 <= v <= 225:
                            cnt += 1
                        r, g, b = tp[x, y]
                        sat_sum += max(r, g, b) - min(r, g, b)
                ink_px += cnt
                if cnt >= cell_thresh:
                    cells_hit += 1
                    if gy < min_r:
                        min_r = gy
                    if gy > max_r:
                        max_r = gy
                    if gx < min_c:
                        min_c = gx
                    if gx > max_c:
                        max_c = gx

        ink_ratio = ink_px / total
        spread = cells_hit / (GRID * GRID)
        bbox_area = ((max_r - min_r + 1) * (max_c - min_c + 1) / (GRID * GRID)) if cells_hit else 0.0
        touches_edge = bool(cells_hit) and (min_r == 0 or max_r == GRID - 1
                                            or min_c == 0 or max_c == GRID - 1)
        sat_mean = sat_sum / total
        aspect_ok = 0.25 <= (w / h if h else 1.0) <= 4.0

        # Texture / "textiness" on a higher-resolution sample so thin print
        # strokes survive. textiness = fraction of ink pixels that sit on an
        # edge. Thin text strokes score high; a solid logo blob scores low.
        tex_long = max(w, h)
        tex_scale = min(1.0, self.texture_dim / tex_long) if tex_long else 1.0
        tw, th = max(8, int(round(w * tex_scale))), max(8, int(round(h * tex_scale)))
        small = gray.resize((tw, th), Image.LANCZOS)
        edges = small.filter(ImageFilter.FIND_EDGES)
        sp = small.load()
        ep = edges.load()
        ink_area = 0
        ink_edge = 0
        for y in range(th):
            for x in range(tw):
                v = sp[x, y]
                if 28 <= v <= 225:
                    ink_area += 1
                    if ep[x, y] > 40:
                        ink_edge += 1
        textiness = (ink_edge / ink_area) if ink_area else 0.0

        return {
            "ink_ratio": ink_ratio, "spread": spread, "bbox_area": bbox_area,
            "touches_edge": touches_edge,
            "sat_mean": sat_mean, "textiness": textiness,
            "ink_area": ink_area, "texture_width": tw, "texture_height": th,
            "aspect_ok": aspect_ok, "width": w, "height": h,
        }

    def _image_signal_score(self, stats: Dict[str, Any]) -> int:
        s = 0
        if stats["textiness"] >= 0.6:
            s += 3
        elif stats["textiness"] >= 0.3:
            s += 2
        if stats["ink_ratio"] >= 0.01:
            s += 1
        if stats["spread"] >= 0.4:
            s += 1
        if stats["sat_mean"] < self.banner_sat:
            s += 1
        if stats["aspect_ok"]:
            s += 1
        return s

    def _text_signals(self, text: str) -> Dict[str, Any]:
        lower = text.lower()
        words = len(_WORD_RE.findall(lower))
        strong = [kw for kw in _STRONG_KEYWORDS if re.search(_bounded(kw), lower)]
        weak = [kw for kw in _WEAK_KEYWORDS if re.search(_bounded(kw), lower)]
        amounts = bool(_CURRENCY_RE.search(lower) or _AMOUNT_WORD_RE.search(lower)
                       or _PRICE_LIKE_RE.search(lower))
        dates = bool(_DATE_RE.search(lower))
        score = 2 * len(strong) + len(weak)
        return {
            "word_count": words, "strong": strong, "weak": weak,
            "score": score, "amount_evidence": amounts, "date_evidence": dates,
        }

    def _meaningful_text(self, text: str) -> bool:
        if not text:
            return False
        words = len(_WORD_RE.findall(text))
        return len(text) >= self.min_pdf_text_chars or words >= 5

    # ======================================================================
    # Low-level readers
    # ======================================================================
    @staticmethod
    def _pdf_text(path: str) -> str:
        from pypdf import PdfReader
        reader = PdfReader(path)
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 - a broken page shouldn't kill the file
                continue
        return "\n".join(parts)

    def _try_render_pages(self, path: str) -> Optional[List[Image.Image]]:
        try:
            from pypdfium2 import PdfDocument
            pdf = PdfDocument(path)
            try:
                total = getattr(pdf, "len", None)
                if total is None:
                    total = len(pdf)
                n = min(int(total), self.max_pdf_pages)
                out: List[Image.Image] = []
                for i in range(n):
                    page = pdf.get_page(i) if hasattr(pdf, "get_page") else pdf[i]
                    bitmap = page.render(scale=1.0)
                    out.append(bitmap.to_pil().convert("RGB"))
                return out
            finally:
                pdf.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("PDF page rendering failed for %s: %s", os.path.basename(path), exc)
            return None


class FilterDecodeError(Exception):
    """Raised when a file cannot be decoded for content inspection."""