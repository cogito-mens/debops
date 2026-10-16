# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Model Context Protocol (MCP) server for DebOps.

This package implements the 2026-07-28 ("stateless") revision of the
protocol: requests are self-contained, there is no 'initialize' handshake
and there are no sessions. Messages are newline-delimited JSON-RPC 2.0,
carried over stdio or a UNIX socket.

The package uses only the Python standard library so that the
'debops mcp' command works in every installation mode.
"""

from .server import McpServer, PROTOCOL_VERSION  # noqa: F401
from .transport import StdioTransport, UnixSocketTransport  # noqa: F401
from .transport import bridge_stdio_to_unix  # noqa: F401
