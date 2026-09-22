"""Model Context Protocol (MCP) layer.

A dependency-free implementation of a minimal but real MCP endpoint:

  * protocol.py   — JSON-RPC 2.0 framing + MCP constants (newline-delimited stdio)
  * transport.py  — stdio transports (real subprocess stdin/stdout, or in-process
                    OS-pipe pairs so the whole stack is testable without plumbing)
  * server.py     — MCP server: initialize / notifications.initialized / ping /
                    tools/list / tools/call
  * excel_tool.py — tools exposing the Excel workbook to MCP clients
                    (excel_read_seed, excel_append_invoice, excel_mark_seen,
                    excel_flush, excel_close)
  * client.py     — MCP client + the ExcelMcpBackend gateway used by main.py

When config `mcp.enabled` is on, the pipeline writes to Excel through the MCP
client (server in a subprocess on stdio, per the standard transport).  When
off, a direct in-process backend is used instead; both expose the same API.
"""

__version__ = "1.0.0"