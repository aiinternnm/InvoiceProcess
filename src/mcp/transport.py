"""Stdio transports for MCP.

`StdioTransport` wraps a read file object and a write file object — exactly the
shape of stdin/stdout in a subprocess, or a pair of in-process OS-pipe ends for
hermetic tests.  Messages are newline-delimited JSON (see protocol.py).

`open_inproc_pair()` connects two transports inside the current process so the
entire MCP stack (server thread + client) can be exercised without any external
plumbing.  The wire format is byte-for-byte the same as a real subprocess.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .protocol import decode_frame, encode_message


class StdioTransport:
    def __init__(self, read_file, write_file, name: str = "transport"):
        self._read = read_file
        self._write = write_file
        self.name = name
        self.closed = False

    def send(self, message) -> None:
        if self.closed:
            raise OSError(f"{self.name}: send on closed transport")
        self._write.write(encode_message(message))
        self._write.flush()

    def recv(self) -> Optional[dict]:
        """Block for the next frame.  Returns None on EOF."""
        try:
            line = self._read.readline()
        except OSError:
            return None
        if not line:
            try:
                self._read.close()
            except Exception:
                pass
            return None
        return decode_frame(line.decode("utf-8", "replace").rstrip("\r\n"))

    def close(self) -> None:
        """Close the write side to signal EOF; the reader closes the read end."""
        if self.closed:
            return
        self.closed = True
        try:
            self._write.flush()
            self._write.close()
        except Exception:
            pass


def open_inproc_pair():
    """Return (client_transport, server_transport) connected by OS pipes."""
    a_r, a_w = os.pipe()
    b_r, b_w = os.pipe()
    read_a = os.fdopen(a_r, "rb", buffering=0)
    write_a = os.fdopen(a_w, "wb", buffering=0)
    read_b = os.fdopen(b_r, "rb", buffering=0)
    write_b = os.fdopen(b_w, "wb", buffering=0)
    client = StdioTransport(read_b, write_a, name="client")
    server = StdioTransport(read_a, write_b, name="server")
    return client, server


def stdio_files() -> StdioTransport:
    """Transport over the current process's stdin/stdout (real subprocess mode)."""
    return StdioTransport(sys.stdin.buffer, sys.stdout.buffer, name="stdio")