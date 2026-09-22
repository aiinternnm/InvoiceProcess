"""MCP stdio client + the ExcelMCP gateway used by main.py when mcp.enabled.

:class:`MCPClient` speaks the protocol the server implements (initialize,
notifications/initialized, ping, tools/list, tools/call).  :class:`ExcelMcpBackend`
presents the same writer-facing API as :class:`DirectExcelBackend` so main.py
is transport-agnostic.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import queue
import threading
from typing import Any, Dict, List, Optional

from ..excel_writer import ExcelWriterError
from .excel_tool import (
    TOOL_APPEND_INVOICE,
    TOOL_CLOSE,
    TOOL_FLUSH,
    TOOL_MARK_SEEN,
    TOOL_READ_SEED,
)
from .protocol import LATEST_PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS, make_notification, make_request
from .transport import StdioTransport

log = logging.getLogger("invoice_pipeline.mcp.client")


class McpError(Exception):
    """Transport / protocol-level failure (not an Excel write error)."""


class MCPClient:
    def __init__(self, transport: StdioTransport, timeout: float = 60.0):
        self._transport = transport
        self._timeout = timeout
        self._seq = 0
        self._pending: Dict[int, queue.Queue] = {}
        self._capabilities: Dict[str, Any] = {}
        self.server_info: Dict[str, Any] = {}
        self.protocol_version = LATEST_PROTOCOL_VERSION
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name="mcp-client-reader", daemon=True)
        self._reader.start()

    # -- plumbing -----------------------------------------------------------
    def _read_loop(self) -> None:
        while True:
            try:
                msg = self._transport.recv()
            except Exception:
                msg = None
            if msg is None:
                self._pending.clear()
                return
            self._on_message(msg)

    def _on_message(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        if req_id is None:
            return
        q = self._pending.pop(req_id, None)
        if q is not None:
            q.put(msg)

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self._lock:
            self._seq += 1
            req_id = self._seq
            q: queue.Queue = queue.Queue()
            self._pending[req_id] = q
        self._transport.send(make_request(method, params, req_id))
        try:
            msg = q.get(timeout=self._timeout)
        except queue.Empty:
            self._pending.pop(req_id, None)
            raise McpError(f"timed out waiting for response to {method}")
        if "error" in msg:
            err = msg["error"] or {}
            data = err.get("data")
            if isinstance(data, dict) and data.get("kind") == "excel_writer":
                raise ExcelWriterError(err.get("message", "Excel write failed"))
            raise McpError(f"{method}: {err.get('message')} (code {err.get('code')})")
        return msg.get("result")

    # -- MCP methods --------------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        result = self._request("initialize", {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "invoice-pipeline", "version": "1.0.0"},
        })
        picked = result.get("protocolVersion")
        if picked in SUPPORTED_PROTOCOL_VERSIONS:
            self.protocol_version = picked
        self._capabilities = result.get("capabilities") or {}
        self.server_info = result.get("serverInfo") or {}
        self._transport.send(make_notification("notifications/initialized"))
        return result

    def ping(self) -> bool:
        return self._request("ping") == {}

    def list_tools(self) -> List[Dict[str, Any]]:
        return (self._request("tools/list") or {}).get("tools") or []

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        content = (result or {}).get("content") or []
        text = "".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ) or "{}"
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    def close(self) -> None:
        try:
            self._transport.close()
        except Exception:
            pass


class ExcelMcpBackend:
    """Writer-compatible view over the MCP Excel service."""

    def __init__(self, cfg: Dict[str, Any], transport: StdioTransport):
        mcp_cfg = cfg.get("mcp") or {}
        self.client = MCPClient(transport, timeout=float(mcp_cfg.get("timeout_seconds", 60)))
        self.client.initialize()
        seed = self.client.call_tool(TOOL_READ_SEED)
        self.headers = list(seed.get("headers") or [])
        self.line_items_headers = list(seed.get("line_items_headers") or [])
        self.has_processed_sheet = bool(seed.get("has_processed_sheet"))
        self._seed_rows = seed.get("seed") or []
        self._cfg = cfg
        self._proc = None

    def read_template_for_seed(self):
        return self._seed_rows

    @staticmethod
    def _wire(value: Any) -> Any:
        """Make values JSON-safe: datetime/date -> ISO strings (the server's
        ExcelWriter re-converts tracked columns back to native cells)."""
        if isinstance(value, (dt.datetime, dt.date)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): ExcelMcpBackend._wire(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [ExcelMcpBackend._wire(v) for v in value]
        return value

    def append_invoice(self, values, line_items, file_id,
                       write_line_items=True, write_processed_ids=True, flush=True):
        li = list(line_items) if (write_line_items and line_items) else []
        res = self.client.call_tool(TOOL_APPEND_INVOICE, {
            "values": dict(self._wire(values or {})),
            "line_items": self._wire(li),
            "file_id": file_id, "flush": bool(flush),
        })
        return res.get("excel_row"), res.get("line_item_count", 0)

    def mark_seen(self, file_id, save=True):
        self.client.call_tool(TOOL_MARK_SEEN, {"file_id": file_id, "save": bool(save)})

    def save(self):
        self.client.call_tool(TOOL_FLUSH)

    def close(self):
        try:
            self.client.call_tool(TOOL_CLOSE)
        except ExcelWriterError:
            self.client.close()
            raise
        except McpError as exc:
            self.client.close()
            raise ExcelWriterError(f"MCP server did not close cleanly: {exc}") from exc
        finally:
            self.client.close()
            self._wait_proc()

    def _wait_proc(self):
        if self._proc is not None:
            try:
                self._proc.wait(timeout=15)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass