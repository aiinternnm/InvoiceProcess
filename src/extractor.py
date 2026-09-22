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
from typing import Any, Dict, List, Optional

from openai import OpenAI, OpenAIError

from .prompt import build_extraction_prompt
from .utils import ensure_dir, now_dt

log = logging.getLogger("invoice_pipeline.extractor")


class ExtractorError(Exception):
    """Raised when a file cannot be processed by the model (hard failure)."""


class Extractor:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.base_url = cfg["base_url"]
        self.api_key = cfg.get("api_key") or "lm-studio"
        self.model = cfg["model"]
        self.max_tokens = cfg.get("max_tokens", 4096)
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
        """Extract structured invoice JSON. Returns {'data': dict, 'meta': dict}."""
        messages = self._build_messages(file_path, mime_type)
        data, meta = self._chat_json(messages, file_path)
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
                latency_ms = int((time.time() - started) * 1000)
                message = resp.choices[0].message
                content = message.content or ""
                finish = getattr(resp.choices[0], "finish_reason", None)
                usage = getattr(resp, "usage", None)
                # SDK 1.x used prompt_tokens/completion_tokens; 2.x+ renamed
                # them to input_tokens/output_tokens. Read either.
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
                    "status": "ok",
                }
                data = self._parse_json(content)
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

    # ---- parsers ----------------------------------------------------------
    @staticmethod
    def _parse_json(content: str) -> Dict[str, Any]:
        content = content.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", content, re.S)
        if fence:
            content = fence.group(1).strip()
        if content.startswith("{"):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                pass
        # balanced-brace rescue
        start = content.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(content)):
                c = content[i]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = content[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        raise ExtractorError(f"model returned non-JSON:\n{content[:800]}")

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