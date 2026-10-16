# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""JSON-RPC 2.0 framing for the DebOps MCP server.

The 2026-07-28 revision of the Model Context Protocol uses
newline-delimited JSON messages: each message is a single line of JSON
terminated by a newline, optionally preceded by advisory header lines.
"""

import json

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Error codes reserved by the MCP specification
UNSUPPORTED_PROTOCOL_VERSION = -32020
REQUEST_TOO_LARGE = -32022

# A single message is limited in size to blunt memory-exhaustion attempts;
# the default is generous enough for large tool outputs
DEFAULT_MAX_MESSAGE_SIZE = 16 * 1024 * 1024

# Advisory header lines which may precede a JSON message
MESSAGE_HEADERS = (b'Content-Length:', b'Mcp-Method:', b'Mcp-Name:')


class JsonRpcError(Exception):
    """A protocol-level error, turned into a JSON-RPC error response."""

    def __init__(self, code, message, data=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id

    def to_response(self):
        return make_error(self.request_id, self.code, self.message,
                          self.data)


def read_message(stream, max_size=DEFAULT_MAX_MESSAGE_SIZE):
    """Read one message from a binary stream.

    Returns the message as bytes, or None when the stream is at its end.
    Empty lines and advisory header lines are skipped. A message which
    exceeds the size limit is discarded and reported as an error.
    """
    while True:
        line = stream.readline(max_size + 1)
        if not line:
            return None
        if not line.endswith(b'\n') and len(line) > max_size:
            # Discard the remainder of the oversized message to
            # resynchronize the stream before reporting the error
            while line and not line.endswith(b'\n'):
                line = stream.readline(max_size + 1)
            raise JsonRpcError(REQUEST_TOO_LARGE, 'Request too large')
        line = line.strip()
        if not line or line.startswith(MESSAGE_HEADERS):
            continue
        return line


def parse_message(data):
    """Parse one message into a JSON-RPC request object."""
    try:
        message = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise JsonRpcError(PARSE_ERROR, 'Parse error')
    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, 'Invalid Request')
    return message


def make_result(request_id, result):
    """Build a JSON-RPC result response."""
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def make_error(request_id, code, message, data=None):
    """Build a JSON-RPC error response."""
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': request_id, 'error': error}


def write_message(stream, message):
    """Write one message to a binary stream as a single line of JSON."""
    stream.write(json.dumps(message).encode('utf-8') + b'\n')
    stream.flush()
