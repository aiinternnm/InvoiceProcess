"""MCP server: JSON-RPC protocol handling for the Excel tools.

Runnable as a real stdio process::

    python -m src.mcp.server --config config.json

so any MCP client — including this pipeline running as a client with
`mcp.enabled` — can drive the workbook over the standard protocol.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

from ..config import load_config
from .excel_tool import ExcelToolError, ExcelToolService, Tool, build_tools
from .protocol import (
    INVALID_PARAMS,
    INTERNAL_ERROR,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    INVALID_REQUEST,
    SERVER_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    make_error,
    make_response,
)
from .transport import StdioTransport, stdio_files

log = logging.getLogger("invoice_pipeline.mcp")


class MCSError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    """Protocol state machine.  `handle_line` is safe to call from one thread."""

    def __init__(self, tools: List[Tool], name: str = "invoice-excel-mcp",
                 version: str = "1.0.0"):
        self.tools = {t.name: t for t in tools}
        self.server_name = name
        self.server_version = version
        self.protocol_version = LATEST_PROTOCOL_VERSION

    def handle_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Process one raw JSON-RPC line; returns the response message or None."""
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            return make_error(None, PARSE_ERROR, "Parse error")
        if not isinstance(message, dict):
            return make_error(None, INVALID_REQUEST, "Invalid Request: not an object")
        method = message.get("method")
        req_id = message.get("id")
        if "id" not in message or isinstance(req_id, bool):
            self._handle_notification(method, message.get("params") or {})
            return None
        try:
            result = self._dispatch(method, message.get("params") or {})
            return make_response(req_id, result)
        except MCSError as exc:
            return make_error(req_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # noqa: BLE001
            log.exception("MCP handler error for %s", method)
            return make_error(req_id, INTERNAL_ERROR, str(exc) or exc.__class__.__name__)

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        # notifications/initialized and friends are acknowledged silently.
        return None

    def _dispatch(self, method: str, params: Dict[str, Any]) -> Any:
        if method == "initialize":
            want = (params or {}).get("protocolVersion")
            if want in SUPPORTED_PROTOCOL_VERSIONS:
                self.protocol_version = want
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.server_name, "version": self.server_version},
                "instructions": (
                    "Excel invoice-append tools. Call tools/call with the tool name "
                    "and arguments as described by each inputSchema."
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [t.describe() for t in self.tools.values()]}
        if method == "tools/call":
            name = (params or {}).get("name")
            arguments = (params or {}).get("arguments") or {}
            tool = self.tools.get(name)
            if tool is None:
                raise MCSError(INVALID_PARAMS, f"Unknown tool: {name!r}")
            try:
                out = tool.handler(arguments)
            except ExcelToolError as exc:
                raise MCSError(INVALID_PARAMS, str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - Excel errors -> server error band
                kind = _error_kind(exc)
                raise MCSError(SERVER_ERROR, str(exc) or exc.__class__.__name__,
                               {"kind": kind} if kind else None) from exc
            return {"content": [{"type": "text",
                                 "text": json.dumps(out, ensure_ascii=False, default=str)}]}
        raise MCSError(METHOD_NOT_FOUND, f"Method not found: {method}")


def _error_kind(exc: Exception) -> Optional[str]:
    name = exc.__class__.__name__
    if name in ("ExcelWriterError", "PermissionError", "OSError", "FileNotFoundError"):
        return "excel_writer"
    return None


def run_server(server: MCPServer, transport: StdioTransport) -> None:
    """Serve frames until EOF on the transport."""
    try:
        while True:
            message = transport.recv()
            if message is None:
                break
            response = server.handle_line(json.dumps(message, ensure_ascii=False))
            if response is not None:
                transport.send(response)
    finally:
        transport.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-server")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    # Never log to stdout: stdout carries MCP frames.
    logging.basicConfig(level=logging.WARNING, handlers=[logging.StreamHandler(sys.stderr)])
    cfg = load_config(args.config)
    service = ExcelToolService(cfg)
    server = MCPServer(build_tools(service))
    run_server(server, stdio_files())
    return 0


if __name__ == "__main__":
    sys.exit(main())