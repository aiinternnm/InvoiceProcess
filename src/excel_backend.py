"""Excel write gateway: one writer-facing API, two transports.

* :class:`DirectExcelBackend`  — writes through :class:`ExcelWriter` in-process
  (used when MCP is disabled, in dry-run mode, and in hermetic tests).
* :class:`ExcelMcpBackend`     — the same verb set executed by an MCP server
  over the Model Context Protocol (stdio subprocess by default; in-process pipe
  pair for hermetic tests).  See package `src.mcp`.

main.py only talks to this gateway, so enabling MCP is a *configuration*
decision (`mcp.enabled`) rather than a different code path.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Any, Dict

from .excel_writer import ExcelWriter
from .mcp.client import ExcelMcpBackend

log = logging.getLogger("invoice_pipeline.excel_backend")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DirectExcelBackend:
    """In-process gateway wrapping ExcelWriter + the processed-IDs ledger."""

    def __init__(self, writer: ExcelWriter, cfg: Dict[str, Any]):
        self._writer = writer
        self._cfg = cfg
        self.headers = list(writer.headers)
        self.line_items_headers = writer.line_items_headers
        self.has_processed_sheet = writer.processed_ids_sheet in writer.wb.sheetnames
        self._processed_ids: set = self._load_processed_ids()

    def _load_processed_ids(self) -> set:
        ids: set = set()
        if self.has_processed_sheet:
            pws = self._writer.wb[self._writer.processed_ids_sheet]
            for r in range(2, pws.max_row + 1):
                v = pws.cell(row=r, column=1).value
                if v:
                    ids.add(str(v))
        return ids

    def read_template_for_seed(self):
        return self._writer.read_template_for_seed()

    def append_invoice(self, values, line_items, file_id,
                       write_line_items=True, write_processed_ids=True, flush=True):
        row_idx = self._writer.append_header_mapped_row(values)
        count = 0
        if write_line_items and line_items:
            count = self._writer.append_line_items(list(line_items))
        if write_processed_ids and file_id and str(file_id) not in self._processed_ids:
            self._writer.append_processed_id(str(file_id))
            self._processed_ids.add(str(file_id))
        if flush:
            self._writer.maybe_flush()
        return row_idx, count

    def mark_seen(self, file_id, save=True):
        if self.has_processed_sheet and file_id and str(file_id) not in self._processed_ids:
            self._writer.append_processed_id(str(file_id))
            self._processed_ids.add(str(file_id))
        if save:
            self._writer.save()

    def save(self):
        self._writer.maybe_flush()

    def close(self):
        self._writer.close()


def open_excel_backend(cfg: Dict[str, Any], cfg_path: str, dry_run: bool):
    """Return the gateway chosen by config (MCP when enabled and not dry-run)."""
    mcp_cfg = cfg.get("mcp") or {}
    if not dry_run and mcp_cfg.get("enabled"):
        transport = str(mcp_cfg.get("transport", "stdio")).lower()
        if transport == "inproc":
            return _open_mcp_inproc(cfg)
        return _open_mcp_subprocess(cfg, cfg_path)
    ecfg = cfg["excel"]
    writer = ExcelWriter(
        ecfg["template_path"], ecfg["target_sheet"], ecfg["lineitems_sheet"],
        ecfg["processed_ids_sheet"], ecfg["backup_dir"],
        flush_every_n=ecfg.get("flush_every_n", 1),
    )
    return DirectExcelBackend(writer, cfg)


def _open_mcp_inproc(cfg: Dict[str, Any]) -> ExcelMcpBackend:
    """Run the MCP server in a background thread over OS pipes (hermetic)."""
    from .mcp.excel_tool import ExcelToolService, build_tools
    from .mcp.server import MCPServer, run_server
    from .mcp.transport import open_inproc_pair

    client_t, server_t = open_inproc_pair()

    def _serve() -> None:
        service = ExcelToolService(cfg)
        server = MCPServer(build_tools(service))
        run_server(server, server_t)

    threading.Thread(target=_serve, name="mcp-server-inproc", daemon=True).start()
    backend = ExcelMcpBackend(cfg, client_t)
    return backend


def _open_mcp_subprocess(cfg: Dict[str, Any], cfg_path: str) -> ExcelMcpBackend:
    """Launch `python -m src.mcp.server` and drive it over real stdio pipes."""
    from .mcp.transport import StdioTransport

    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.mcp.server", "--config", os.path.abspath(cfg_path)],
        cwd=_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, env=env,
    )
    transport = StdioTransport(proc.stdout, proc.stdin, name="mcp-subprocess")
    backend = ExcelMcpBackend(cfg, transport)
    backend._proc = proc
    return backend