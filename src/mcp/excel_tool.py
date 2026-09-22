"""MCP Excel tools: the workbook backend exposed over the Model Context Protocol.

The server owns a single :class:`ExcelWriter` for the run (workbook opened once,
safety backup once per run, append-only).  The tools are thin verbs that mirror
the writer-facing API used by main.py, so MCP and direct transports behave
identically.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from ..excel_writer import ExcelWriter

log = logging.getLogger("invoice_pipeline.mcp")

TOOL_READ_SEED = "excel_read_seed"
TOOL_APPEND_INVOICE = "excel_append_invoice"
TOOL_MARK_SEEN = "excel_mark_seen"
TOOL_FLUSH = "excel_flush"
TOOL_CLOSE = "excel_close"


class ExcelToolError(Exception):
    """Raised for invalid tool arguments (mapped to JSON-RPC INVALID_PARAMS)."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    extra: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ExcelToolService:
    """Stateful Excel service backing the MCP tools (one workbook per run)."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        ecfg = cfg["excel"]
        self.writer = ExcelWriter(
            ecfg["template_path"], ecfg["target_sheet"], ecfg["lineitems_sheet"],
            ecfg["processed_ids_sheet"], ecfg["backup_dir"],
            flush_every_n=ecfg.get("flush_every_n", 1),
        )
        self._processed_ids: set = set()
        if self.writer.processed_ids_sheet in self.writer.wb.sheetnames:
            pws = self.writer.wb[self.writer.processed_ids_sheet]
            for r in range(2, pws.max_row + 1):
                v = pws.cell(row=r, column=1).value
                if v:
                    self._processed_ids.add(str(v))

    # -- tool verbs ---------------------------------------------------------
    def read_seed(self, params) -> Dict[str, Any]:
        rows = self.writer.read_template_for_seed()
        return {
            "headers": list(self.writer.headers),
            "line_items_headers": list(self.writer.line_items_headers),
            "has_processed_sheet": self.writer.processed_ids_sheet in self.writer.wb.sheetnames,
            "seed": rows,
            "processed_ids": sorted(self._processed_ids),
        }

    def append_invoice(self, params) -> Dict[str, Any]:
        values = params.get("values")
        line_items = params.get("line_items") or []
        file_id = params.get("file_id")
        flush = bool(params.get("flush", False))
        if not isinstance(values, dict):
            raise ExcelToolError("'values' must be an object of {column header: value}")
        row_idx = self.writer.append_header_mapped_row(values)
        count = 0
        if isinstance(line_items, list) and line_items:
            count = self.writer.append_line_items(self._as_native_line_items(line_items))
        written_id = False
        if (file_id and self.cfg["excel"].get("write_processed_ids", True)
                and str(file_id) not in self._processed_ids):
            self.writer.append_processed_id(str(file_id))
            self._processed_ids.add(str(file_id))
            written_id = True
        saved = False
        if flush:
            self.writer.maybe_flush()
            saved = True
        return {"excel_row": row_idx, "line_item_count": count,
                "processed_id_written": written_id, "saved": saved}

    def _as_native_line_items(self, line_items: List[List[Any]]) -> List[List[Any]]:
        """Restore native cell types for temporal columns the client serialized
        to ISO strings (mirrors Mapper.build_line_item_rows in direct mode)."""
        headers = list(self.writer.line_items_headers)
        date_col = headers.index("_InvoiceDate") if "_InvoiceDate" in headers else None
        out: List[List[Any]] = []
        for raw in line_items:
            row = list(raw)
            if date_col is not None and len(row) > date_col and isinstance(row[date_col], str):
                try:
                    row[date_col] = dt.date.fromisoformat(row[date_col])
                except ValueError:
                    pass
            out.append(row)
        return out

    def mark_seen(self, params) -> Dict[str, Any]:
        file_id = params.get("file_id")
        save = bool(params.get("save", True))
        if not file_id:
            raise ExcelToolError("'file_id' is required")
        row = None
        written = False
        if (self.cfg["excel"].get("write_processed_ids", True)
                and str(file_id) not in self._processed_ids):
            row = self.writer.append_processed_id(str(file_id))
            self._processed_ids.add(str(file_id))
            written = True
        saved = False
        if save:
            self.writer.save()
            saved = True
        return {"row": row, "processed_id_written": written, "saved": saved}

    def flush(self, params) -> Dict[str, Any]:
        self.writer.maybe_flush()
        return {"saved": True}

    def close_tool(self, params) -> Dict[str, Any]:
        self.writer.close()
        return {}


# -- tool registry -----------------------------------------------------------
_OBJ = {"type": "object"}


def build_tools(service: ExcelToolService) -> List[Tool]:
    return [
        Tool(
            TOOL_READ_SEED,
            "Return workbook shape (headers, line-item headers, processed-ID sheet flag) "
            "plus every existing record so a client can seed duplicate detection.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            service.read_seed,
        ),
        Tool(
            TOOL_APPEND_INVOICE,
            "Append one extracted invoice to the workbook (details row + line items + "
            "processed-ID row) and optionally flush to disk. 'values' is the pre-mapped "
            "{column header: value} dict; only columns that exist in the header row are written.",
            {
                "type": "object",
                "properties": {
                    "values": {"type": "object"},
                    "line_items": {"type": "array"},
                    "file_id": {"type": "string"},
                    "flush": {"type": "boolean"},
                },
                "required": ["values"],
                "additionalProperties": False,
            },
            service.append_invoice,
        ),
        Tool(
            TOOL_MARK_SEEN,
            "Record a file as already-seen (skip/failure paths) so it is never reprocessed.",
            {
                "type": "object",
                "properties": {"file_id": {"type": "string"}, "save": {"type": "boolean"}},
                "required": ["file_id"],
                "additionalProperties": False,
            },
            service.mark_seen,
        ),
        Tool(
            TOOL_FLUSH,
            "Persist staged Excel rows according to the configured flush policy.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            service.flush,
        ),
        Tool(
            TOOL_CLOSE,
            "Flush and close the workbook for the run.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            service.close_tool,
        ),
    ]