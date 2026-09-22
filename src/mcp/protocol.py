"""JSON-RPC 2.0 framing and MCP protocol constants.

MCP speaks JSON-RPC 2.0.  The stdio transport uses one JSON document per line
(newline-delimited frames).  Only single (non-batch) messages are emitted.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

JSON_RPC_VERSION = "2.0"
ENCODING = "utf-8"

# MCP protocol versions this server understands.  The client's requested
# version is echoed back when supported, otherwise our newest is used.
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-06-18")

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_ERROR = -32000  # MCP reserves this band for tools/call handler errors


def encode_message(message: Dict[str, Any]) -> bytes:
    """Serialize a JSON-RPC message as one newline-delimited frame."""
    return (json.dumps(message, ensure_ascii=False) + "\n").encode(ENCODING)


def decode_frame(line: str) -> Dict[str, Any]:
    return json.loads(line)


def make_request(method: str, params: Optional[Dict[str, Any]], req_id: int) -> Dict[str, Any]:
    return {"jsonrpc": JSON_RPC_VERSION, "id": req_id, "method": method, "params": params or {}}


def make_response(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSON_RPC_VERSION, "id": req_id, "result": result}


def make_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSON_RPC_VERSION, "id": req_id, "error": err}


def make_notification(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"jsonrpc": JSON_RPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def is_notification(message: Dict[str, Any]) -> bool:
    return "id" not in message