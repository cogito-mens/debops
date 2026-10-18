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
import textwrap

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

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


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


def parse_call_arguments(pairs):
    """Parse KEY=VALUE pairs into tool arguments.

    'true'/'false' become booleans and integers become integers;
    everything else stays a string. Raises ValueError for a pair
    without a key or the '='.
    """
    arguments = {}
    for pair in pairs:
        key, separator, value = pair.partition('=')
        if not separator or not key:
            raise ValueError('not a KEY=VALUE pair: ' + pair)
        if value.lower() in ('true', 'false'):
            value = value.lower() == 'true'
        else:
            try:
                value = int(value)
            except ValueError:
                pass
        arguments[key] = value
    return arguments


# -- formatters for human inspection ---------------------------------------

def _first_sentence(text):
    """The first sentence of a description, single-spaced."""
    text = ' '.join(str(text).split())
    sentence, _, rest = text.partition('. ')
    if rest:
        return sentence + '.'
    return text


def _shorten(text, width=64):
    """The first sentence of a description, truncated to a width."""
    text = _first_sentence(text)
    if len(text) > width:
        return text[:width - 3].rstrip() + '...'
    return text


def annotation_flags(annotations):
    """Render tool annotations as short flags for humans."""
    flags = []
    if annotations.get('readOnlyHint'):
        flags.append('read-only')
    elif annotations.get('destructiveHint', True):
        flags.append('destructive')
    else:
        flags.append('non-destructive')
    if annotations.get('idempotentHint'):
        flags.append('idempotent')
    if annotations.get('openWorldHint') is False:
        flags.append('local')
    return flags


def format_discover(result):
    """Render a server/discover result as lines for humans."""
    lines = []
    meta = result.get('_meta', {})
    info = meta.get('io.modelcontextprotocol/serverInfo', {})
    lines.append('name: {}'.format(info.get('name', '?')))
    lines.append('version: {}'.format(info.get('version', '?')))
    if meta.get('org.debops/project') is not None:
        lines.append('project: {}'.format(meta['org.debops/project']))
    if meta.get('org.debops/view') is not None:
        lines.append('view: {}'.format(meta['org.debops/view']))
    lines.append('protocol: {}'.format(
            ', '.join(result.get('supportedVersions', [])) or '?'))
    capabilities = result.get('capabilities', {})
    if capabilities:
        lines.append('capabilities:')
        for name, contents in sorted(capabilities.items()):
            lines.append('    {}: {}'.format(
                    name, json.dumps(contents) if contents else 'yes'))
    else:
        lines.append('capabilities: (none)')
    if result.get('instructions'):
        lines.append('instructions:')
        lines.extend('    ' + line
                     for line in textwrap.wrap(result['instructions'],
                                               width=76))
    lines.append('cache: {} ms, {}'.format(result.get('ttlMs', '?'),
                                           result.get('cacheScope', '?')))
    return lines


def format_tools(tools):
    """Render a tools/list result as lines for humans."""
    lines = ['tools ({}):'.format(len(tools))]
    width = max([len(tool['name']) for tool in tools] + [0])
    for tool in tools:
        flags = annotation_flags(tool.get('annotations', {}))
        suffix = ' [{}]'.format(', '.join(flags)) if flags else ''
        lines.append('    {}  {}{}'.format(
                tool['name'].ljust(width),
                _shorten(tool.get('description', '')),
                suffix))
    return lines


def format_tool(tool):
    """Render one tool in detail, with its parameters, for humans."""
    lines = ['{} — {}'.format(tool['name'], tool.get('description', ''))]
    flags = annotation_flags(tool.get('annotations', {}))
    if flags:
        lines.append('annotations: {}'.format(', '.join(flags)))
    schema = tool.get('inputSchema', {})
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if properties:
        lines.append('parameters:')
        width = max(len(name) for name in properties)
        for name, prop in sorted(properties.items()):
            marker = ', required' if name in required else ''
            lines.append('    {}  ({}): {}'.format(
                    (name + marker).ljust(width + 11),
                    prop.get('type', '?'),
                    _first_sentence(prop.get('description', ''))))
    else:
        lines.append('parameters: (none)')
    return lines


def format_resources(resources):
    """Render a resources/list result as lines for humans."""
    lines = ['resources ({}):'.format(len(resources))]
    width = max([len(resource['name']) for resource in resources] + [0])
    for resource in resources:
        lines.append('    {}  {}'.format(
                resource['name'].ljust(width),
                _shorten(resource.get('description', ''))))
    return lines
