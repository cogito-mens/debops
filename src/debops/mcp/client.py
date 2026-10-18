# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""A small synchronous MCP client over a UNIX socket.

This is the human-facing client behind the 'debops mcp connect' and
'debops mcp inspect' commands, used to debug and inspect a running
server; it is not a general-purpose library.
"""

import json
import os
import socket

from . import rpc
from . import server as _server


def request_meta(capabilities=None):
    """The '_meta' this client sends with every request."""
    return {
        'io.modelcontextprotocol/protocolVersion': _server.PROTOCOL_VERSION,
        'io.modelcontextprotocol/clientInfo': {
            'name': 'debops', 'version': _server.__version__,
        },
        'io.modelcontextprotocol/clientCapabilities': capabilities or {},
    }


class UnixClient(object):
    """Send requests to an MCP server on a UNIX socket, one at a time.

    Requests are synchronous and answered in order, so no response
    correlation is needed.
    """

    def __init__(self, path, timeout=5, capabilities=None):
        self._path = os.path.abspath(os.path.expanduser(path))
        self._timeout = timeout
        self._capabilities = capabilities
        self._connection = None
        self._reader = None
        self._next_id = 1

    def _connect(self):
        if self._connection is not None:
            return
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout)
        try:
            connection.connect(self._path)
        except OSError as errmsg:
            connection.close()
            raise ConnectionError('cannot connect to {}: {}'
                                  .format(self._path, errmsg))
        self._connection = connection
        self._reader = connection.makefile('rb')

    def request_raw(self, method, params=None):
        """Send one request; return the raw response line (bytes)."""
        self._connect()
        message = {'jsonrpc': '2.0', 'id': self._next_id,
                   'method': method,
                   '_meta': request_meta(self._capabilities)}
        if params is not None:
            message['params'] = params
        self._next_id += 1
        try:
            self._connection.sendall(json.dumps(message).encode('utf-8')
                                     + b'\n')
            data = rpc.read_message(self._reader)
        except socket.timeout:
            raise TimeoutError('no response within {} seconds'
                               .format(self._timeout))
        except ConnectionResetError:
            raise EOFError('the server closed the connection without '
                           'a response')
        if data is None:
            raise EOFError('the server closed the connection without '
                           'a response')
        return data

    def request(self, method, params=None):
        """Send one request; return the parsed response."""
        return json.loads(self.request_raw(method, params))

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
            self._reader = None


def request_unix(path, method, params=None, timeout=5):
    """Send one request to an MCP server on a UNIX socket.

    Returns the raw response line. Raises ConnectionError when the
    socket cannot be connected, EOFError when the server closes the
    connection without a response (for example because of the kill
    switch), and TimeoutError when no response arrives in time.
    """
    client = UnixClient(path, timeout=timeout)
    try:
        return client.request_raw(method, params)
    finally:
        client.close()
