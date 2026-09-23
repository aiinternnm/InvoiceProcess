"""Qwen multimodal extraction via the OpenAI-compatible LM Studio / ngrok endpoint."""
from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from openai import OpenAI, OpenAIError

from .prompt import (
    SCHEMA_LINEITEM_FIELDS,
    SCHEMA_SCALAR_FIELDS,
    build_extraction_prompt,
    build_extraction_prompt_xml,
)
from .utils import ensure_dir, now_dt
from .validator import validate_extraction

log = logging.getLogger("invoice_pipeline.extractor")


class ExtractorError(Exception):
    """Raised when a file cannot be processed by the model (hard failure)."""


class Extractor:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base_url = cfg["base_url"]
        self.api_key = cfg.get("api_key") or "lm-studio"
        self.model = cfg["model"]
        self.max_tokens = cfg.get("max_tokens", 8192)
        self.temperature = cfg.get("temperature", 0)
        self.timeout = cfg.get("timeout_seconds", 300)
        self.vision = cfg.get("vision_enabled", True)
        self.min_pdf_text = cfg.get("min_pdf_text_chars", 40)
        self.max_pdf_pages = cfg.get("max_pdf_pages_as_images", 6)
        self.retries = cfg.get("retries", 2)
        self.backoff = cfg.get("backoff_seconds", 5)
        # max_retries=0 -> the SDK never retries on its own; our own bounded
        # retry/backoff loop below owns retry behaviour (avoids up to 9 attempts).
        self._client = OpenAI(base_url=self.base_url, api_key=self.api_key,
                              timeout=self.timeout, max_retries=0)

    # ---- public API -------------------------------------------------------
    def ping(self) -> Dict[str, Any]:
        """Check connectivity + model availability. Returns info dict."""
        started = time.time()
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
            temperature=0,
        )
        return {
            "ok": True,
            "base_url": self.base_url,
            "model": self.model,
            "latency_ms": int((time.time() - started) * 1000),
            "reply": resp.choices[0].message.content,
        }

    def extract(self, file_path: str, mime_type: str) -> Dict[str, Any]:
        """Extract structured invoice data. Returns {'data': dict, 'meta': dict}.

        JSON is the primary structured output. When JSON fails (malformed /
        unusable / truncated / response_format unsupported / schema-invalid
        reject), a SECOND controlled Qwen request asks for XML, which is parsed
        and mapped into the SAME canonical invoice object. Only one record is
        ever produced for a file; the source format is reported in ``meta``.
        """
        messages = self._build_messages(file_path, mime_type)
        try:
            data, meta = self._chat_json(messages, file_path)
        except ExtractorError as json_err:
            return self._xml_fallback(messages, file_path, json_err)
        if not self._usable(data):
            return self._xml_fallback(
                messages, file_path,
                ExtractorError("JSON parsed but is schema-invalid (reject-level)."))
        meta["source_format"] = "json"
        meta["xml_fallback"] = False
        return {"data": data, "meta": meta}

    def _usable(self, data: Dict[str, Any]) -> bool:
        """Deterministic gate: is the parsed output a usable invoice record?

        Reuses the canonical validator decision (single source of truth for the
        reject rule). No semantic guessing happens here.
        """
        try:
            return validate_extraction(data, self.cfg).decision != "reject"
        except Exception:  # noqa: BLE001 - never let a validator hiccup skip the fallback
            return False

    def _xml_fallback(self, messages: List[Dict[str, Any]], file_path: str,
                      json_err: Exception) -> Dict[str, Any]:
        log.warning("JSON path unusable on %s (%s); requesting XML fallback",
                    os.path.basename(file_path), str(json_err)[:300])
        try:
            data, meta = self._chat_xml(messages, file_path)
        except ExtractorError as exc:
            raise ExtractorError(
                f"Both JSON and XML extraction failed for {os.path.basename(file_path)}. "
                f"JSON error: {json_err}; XML error: {exc}"
            ) from exc
        meta["source_format"] = "xml"
        meta["xml_fallback"] = True
        meta["json_error"] = str(json_err)[:2000]
        return {"data": data, "meta": meta}

    # ---- message building -------------------------------------------------
    def _build_messages(self, file_path: str, mime_type: str) -> List[Dict[str, Any]]:
        prompt = build_extraction_prompt(self.cfg, mime_type)
        mime = (mime_type or "").lower()
        ext = os.path.splitext(file_path)[1].lower()

        if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".gif"):
            if not self.vision:
                raise ExtractorError("Image input requires vision_enabled=true in config.")
            img_mime = self._mime_for(file_path, mime)
            b64 = self._b64_file(file_path, img_mime)
            return [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }]

        if mime == "application/pdf" or ext == ".pdf":
            try:
                text = self._pdf_text(file_path)
            except ExtractorError:
                raise
            except Exception as exc:  # noqa: BLE001 - malformed PDFs should not stop the batch
                raise ExtractorError(f"Could not read PDF {os.path.basename(file_path)}: {exc}") from exc
            if len(text.split()) >= 5 or len(text) >= self.min_pdf_text:
                content = f"{prompt}\n\n---\nPDF TEXT CONTENT:\n{text[:120_000]}"
                return [{"role": "user", "content": content}]
            if self.vision:
                try:
                    images = self._pdf_images(file_path, self.max_pdf_pages)
                except ExtractorError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise ExtractorError(
                        f"PDF pages could not be rendered for vision ({os.path.basename(file_path)}): {exc}"
                    ) from exc
                if images:
                    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
                    for b64, pg in images:
                        content.append({"type": "text", "text": f"\n[Page {pg}]"})
                        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
                    return [{"role": "user", "content": content}]
                raise ExtractorError("PDF had no selectable text and pages could not be rendered for vision.")
            raise ExtractorError(
                "PDF has no extractable text. Enable vision_enabled=true (scanned PDFs) or use a text-based PDF."
            )

        if mime.startswith("text/") or ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
            return [{"role": "user", "content": f"{prompt}\n\n---\nDOCUMENT TEXT:\n{txt[:120_000]}"}]

        raise ExtractorError(f"Unsupported mime/extension for extraction: {mime} / {ext}")

    # ---- the call ---------------------------------------------------------
    def _chat_json(self, messages: List[Dict[str, Any]], file_path: str) -> tuple:
        attempts = self.retries + 1
        last_err: Optional[Exception] = None
        started = time.time()
        for i in range(attempts):
            try:
                kwargs: Dict[str, Any] = dict(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                try:
                    # LM Studio supports json_object for many models; fall back if rejected.
                    resp = self._client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
                except OpenAIError as exc:
                    err_name = exc.__class__.__name__
                    if err_name in ("BadRequestError", "UnprocessableEntityError") and "response_format" in str(exc):
                        log.warning("response_format not supported, retrying without it (%s)", err_name)
                        resp = self._client.chat.completions.create(**kwargs)
                    else:
                        raise
                content, finish, meta = self._consume_response(resp, started, label="JSON")
                try:
                    data = self._parse_json(content)
                except ExtractorError as exc:
                    if str(finish) in ("length", "tokens"):
                        raise ExtractorError(
                            f"model response truncated (finish_reason={finish}); JSON "
                            f"incomplete. Increase model.max_tokens (currently "
                            f"{self.max_tokens}) in config.json."
                        ) from exc
                    raise
                return data, meta
            except (OpenAIError, ExtractorError) as exc:
                last_err = exc
                if isinstance(exc, ExtractorError):
                    log.error("JSON parse/extraction failure on %s: %s", os.path.basename(file_path), exc)
                    raise
                log.warning("API attempt %d/%d failed on %s: %s",
                            i + 1, attempts, os.path.basename(file_path), exc)
                if i < attempts - 1:
                    time.sleep(self.backoff * (i + 1))
        raise ExtractorError(f"Qwen API failed after {attempts} attempts: {last_err}")

    def _chat_xml(self, messages: List[Dict[str, Any]], file_path: str) -> tuple:
        """Controlled SECOND extraction request asking for strict XML.

        The invoice input already travels in ``messages``; a follow-up user turn
        asks Qwen to re-answer it as XML (never sends ``response_format``).
        Same bounded retry/backoff, empty/truncation/reasoning handling and
        safe-parse semantics as the JSON path. The result is mapped to the SAME
        canonical invoice object, so downstream validation/Excel see one shape.
        """
        xml_messages: List[Dict[str, Any]] = list(messages)
        xml_messages.append({
            "role": "user",
            "content": build_extraction_prompt_xml(self.cfg, mime_type=""),
        })
        attempts = self.retries + 1
        last_err: Optional[Exception] = None
        started = time.time()
        for i in range(attempts):
            try:
                kwargs: Dict[str, Any] = dict(
                    model=self.model,
                    messages=xml_messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
                resp = self._client.chat.completions.create(**kwargs)
                content, finish, meta = self._consume_response(resp, started, label="XML")
                try:
                    data = self._parse_xml(content)
                except ExtractorError as exc:
                    if str(finish) in ("length", "tokens"):
                        raise ExtractorError(
                            f"XML fallback response truncated (finish_reason={finish}); "
                            f"raise model.max_tokens (currently {self.max_tokens})."
                        ) from exc
                    raise
                meta["status"] = "ok"
                return data, meta
            except (OpenAIError, ExtractorError) as exc:
                last_err = exc
                if isinstance(exc, ExtractorError):
                    log.error("XML fallback failed on %s: %s", os.path.basename(file_path), exc)
                    raise
                log.warning("XML fallback API attempt %d/%d failed on %s: %s",
                            i + 1, attempts, os.path.basename(file_path), exc)
                if i < attempts - 1:
                    time.sleep(self.backoff * (i + 1))
        raise ExtractorError(f"Qwen XML fallback failed after {attempts} attempts: {last_err}")

    def _consume_response(self, resp: Any, started: float, label: str) -> tuple:
        """Normalize a chat-completions response -> (content, finish_reason, meta).

        Shared by the JSON and XML paths so empty-content / thinking-only /
        truncation behaviour is identical for both formats and gives one clear,
        actionable message instead of a generic failure.
        """
        message = resp.choices[0].message
        finish = getattr(resp.choices[0], "finish_reason", None)
        content = (message.content or "").strip()
        reasoning = self._reasoning_of(message)
        usage = getattr(resp, "usage", None)
        latency_ms = int((time.time() - started) * 1000)

        def _usage_tokens(*names: str):
            for n in names:
                v = getattr(usage, n, None) if usage else None
                if v is not None:
                    return v
            return None

        meta = {
            "latency_ms": latency_ms,
            "finish_reason": finish,
            "input_tokens": _usage_tokens("input_tokens", "prompt_tokens"),
            "output_tokens": _usage_tokens("output_tokens", "completion_tokens"),
            "reasoning_chars": len(reasoning),
            "status": "ok",
        }
        if not content:
            if str(finish) in ("length", "tokens"):
                raise ExtractorError(
                    f"{label}: model returned NO content - response truncated "
                    f"(finish_reason={finish}). Qwen thinking consumed the whole "
                    f"completion budget (model.max_tokens={self.max_tokens}). "
                    f"Increase model.max_tokens in config.json (e.g. 8192)."
                )
            if reasoning:
                raise ExtractorError(
                    f"{label}: model returned only thinking/reasoning text, no final "
                    f"content. Qwen3.5 reasoning counted fully against "
                    f"model.max_tokens={self.max_tokens}, leaving no budget for the "
                    f"answer. Increase model.max_tokens in config.json (e.g. 8192) "
                    f"or disable thinking in LM Studio."
                )
            raise ExtractorError(f"{label}: model returned an empty reply; cannot extract.")
        return content, finish, meta

    # ---- parsers ----------------------------------------------------------
    _INVOICE_KEYS = frozenset(
        ("invoice_number", "line_items", "total_amount", "vendor_gstin", "invoice_date")
    )

    @classmethod
    def _parse_json(cls, content: str) -> Dict[str, Any]:
        """Extract the invoice JSON from a model reply.

        Robust to reasoning/trailing text, markdown fences and brace fragments
        that a thinking model (Qwen3.5) may leave before or after the real JSON:
        scans every balanced JSON object in the reply and keeps the best-scoring
        one (largest span, bonus for invoice-looking keys).
        """
        content = (content or "").strip()
        if not content:
            raise ExtractorError("model returned empty content")
        fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.S)
        if fence:
            candidate = fence.group(1).strip()
            if candidate:
                parsed = cls._best_json(candidate)
                if parsed is not None:
                    return parsed
        parsed = cls._best_json(content)
        if parsed is not None:
            return parsed
        raise ExtractorError(f"model returned non-JSON:\n{content[:800]}")

    @classmethod
    def _best_json(cls, text: str) -> Optional[Dict[str, Any]]:
        """Return the best decodable JSON object found anywhere in `text`."""
        if not text:
            return None
        candidates: List[tuple] = []  # (score, obj)
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj:
                candidates.append((len(text), obj))
        except (json.JSONDecodeError, ValueError):
            pass
        decoder = json.JSONDecoder()
        for m in re.finditer(r"\{", text):
            try:
                obj, end = decoder.raw_decode(text, m.start())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict) and obj:
                span = end - m.start()
                score = span
                if "invoice_number" in obj:
                    score += 1_000_000
                elif any(k in obj for k in cls._INVOICE_KEYS):
                    score += 500_000
                candidates.append((score, obj))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    @classmethod
    def _parse_xml(cls, content: str) -> Dict[str, Any]:
        """Parse a Qwen XML fallback reply into the canonical invoice object.

        * XML declaration/fences are tolerated.
        * ``<!DOCTYPE>`` / ``<!ENTITY>`` are rejected outright (XXE /
          entity-expansion guard) before parsing.
        * Only names from the canonical schema are read; unknown tags are
          ignored. Numeric/date normalization stays in the validator.
        """
        content = (content or "").strip()
        if not content:
            raise ExtractorError("XML fallback: model returned empty content")
        fence = re.search(r"```(?:xml)?\s*(.*?)```", content, re.S)
        if fence:
            content = fence.group(1).strip()
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", content, flags=re.IGNORECASE):
            raise ExtractorError(
                "XML fallback rejected for safety: <!DOCTYPE>/<!ENTITY> constructs "
                "are not allowed (XXE / entity-expansion guard)."
            )
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise ExtractorError(f"XML fallback: malformed XML: {exc}") from exc

        def _name(el: ET.Element) -> str:
            return el.tag.rsplit("}", 1)[-1]  # strip an eventual {namespace}

        def _named_children(el: ET.Element, name: str) -> List[ET.Element]:
            return [c for c in el if _name(c) == name]

        def _first(el: ET.Element, name: str) -> Optional[ET.Element]:
            for c in el:
                if _name(c) == name:
                    return c
            return None

        def _text_or_none(el: ET.Element) -> Optional[str]:
            t = (el.text or "").strip()
            return t or None

        out: Dict[str, Any] = {}
        for child in root:
            if _name(child) in SCHEMA_SCALAR_FIELDS:
                out[_name(child)] = _text_or_none(child)

        uncertain: List[str] = []
        uf = _first(root, "uncertain_fields")
        if uf is not None:
            for child in _named_children(uf, "field"):
                t = _text_or_none(child)
                if t:
                    uncertain.append(t)
        out["uncertain_fields"] = uncertain

        line_items: List[Dict[str, Any]] = []
        lp = _first(root, "line_items")
        if lp is not None:
            for li in _named_children(lp, "line_item"):
                row: Dict[str, Any] = {}
                for child in li:
                    if _name(child) in SCHEMA_LINEITEM_FIELDS:
                        row[_name(child)] = _text_or_none(child)
                if row:
                    line_items.append(row)
        out["line_items"] = line_items
        return out

    @classmethod
    def _reasoning_of(cls, message: Any) -> str:
        """Read Qwen3.5 reasoning_content from the SDK message object.

        Handles both direct attribute access and pydantic model_extra (the
        OpenAI SDK keeps unrecognised fields such as reasoning_content there).
        """
        raw = getattr(message, "reasoning_content", None)
        if not raw:
            extra = getattr(message, "model_extra", None) or {}
            raw = extra.get("reasoning_content")
        return raw if isinstance(raw, str) else ""

    # ---- file helpers -----------------------------------------------------
    _EXT_MIME = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".tif": "image/tiff", ".tiff": "image/tiff", ".bmp": "image/bmp",
        ".webp": "image/webp", ".gif": "image/gif",
    }

    @classmethod
    def _mime_for(cls, path: str, drive_mime: Optional[str]) -> str:
        """Reliable image MIME for base64 data-URIs (mimetypes can return None)."""
        mt = (drive_mime or "").lower()
        if mt.startswith("image/"):
            return mt
        ext = os.path.splitext(path)[1].lower()
        if ext in cls._EXT_MIME:
            return cls._EXT_MIME[ext]
        return mimetypes.guess_type(path)[0] or "application/octet-stream"

    @staticmethod
    def _b64_file(path: str, mime: Optional[str]) -> str:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode()

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

    @staticmethod
    def _pdf_images(path: str, max_pages: int) -> List[tuple]:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        try:
            total = getattr(pdf, "len", None)
            if total is None:
                total = len(pdf)
            n = min(int(total), max_pages)
            out: List[tuple] = []
            for i in range(n):
                page = pdf.get_page(i) if hasattr(pdf, "get_page") else pdf[i]
                bitmap = page.render(scale=2.0)
                pil_img = bitmap.to_pil()
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                out.append((base64.b64encode(buf.getvalue()).decode(), i + 1))
                pil_img.close()
            return out
        finally:
            pdf.close()