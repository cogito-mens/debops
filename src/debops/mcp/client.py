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
import re
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

    @property
    def inflight_id(self):
        """The id of the most recently sent request."""
        return self._next_id - 1

    def notify(self, method, params=None):
        """Send a notification; no response is expected or read."""
        self._connect()
        message = {'jsonrpc': '2.0', 'method': method,
                   '_meta': request_meta(self._capabilities)}
        if params is not None:
            message['params'] = params
        self._connection.sendall(json.dumps(message).encode('utf-8')
                                 + b'\n')

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
        elif re.fullmatch(r'-?(0|[1-9][0-9]*)', value):
            # A leading zero marks a non-decimal convention (an octal
            # file mode, ...), not an integer
            value = int(value)
        arguments[key] = value
    return arguments


# Curated example invocations shown by 'inspect --tool', keyed by tool
# name; each entry is the argument part of a '--call' command line (an
# empty string for tools without parameters)
TOOL_CALL_EXAMPLES = {
    'project_status': [''],
    'project_files': ['path=ansible/inventory'],
    'project_read': ['path=ansible/inventory/hosts'],
    'view_write': ['path=resources/scripts/hello.sh '
                   'content="#!/bin/sh" mode=0755'],
    'view_delete': ['path=playbooks/deploy.yml'],
    'keyring_fetch': ['url=https://keybase.io/drybjed/key.asc'],
    'overrides_write': ['path=templates/nginx/nginx.conf.j2 '
                        'content=...'],
    'config_get': ['key=project.default_view'],
    'environment_list': [''],
    'environment_set': ['name=REGION value=eu-central-1'],
    'environment_unset': ['name=REGION'],
    'inventory_query': ['action=graph',
                        'action=host host=web1.example.com'],
    'inventory_spec': ['spec="version: 0\\nfiles:\\n  hosts.yml: ..." '
                       'dry_run=true'],
    'secret_paths': ['pattern=**/password'],
    'playbooks': ['filter=nginx'],
    'playbook_info': ['playbook=service/nginx'],
    'collection_playbooks': ['', 'collection=debops.debops'],
    'collection_roles': ['filter=nginx'],
    'collection_require': ['name=community.docker'],
    'collection_remove': ['name=community.docker'],
    'collection_sync': [''],
    'role_info': ['name=nginx'],
    'role_variables': ['name=nginx'],
    'role_argspec': ['name=nginx'],
    'role_defaults': ['name=nginx',
                      'name=nginx detail=full filter=nginx__www'],
    'role_tasks': ['name=nginx'],
    'role_handlers': ['name=nginx'],
    'role_templates': ['name=nginx'],
    'role_files': ['name=nginx'],
    'check': ['playbook=service/nginx',
              'playbook=site limit=web1.example.com'],
    'run': ['playbook=service/nginx',
            'playbook=site limit=web1.example.com'],
    'ansible_ping': ['hosts=webservers', 'hosts=all become=true'],
    'ansible_facts': ['hosts=web1.example.com',
                      'hosts=all filter=ansible_distribution*'],
    'project_pull': [''],
    'project_push': [''],
}


def format_examples(name):
    """Render the example invocations of a tool as lines for humans."""
    examples = TOOL_CALL_EXAMPLES.get(name)
    if not examples:
        return []
    lines = ['examples:']
    for arguments in examples:
        suffix = ' ' + arguments if arguments else ''
        lines.append('    debops mcp connect <socket> --call {}{}'
                     .format(name, suffix))
    return lines


# -- formatters for human inspection ---------------------------------------

# A sentence end: a dot followed by whitespace, but not the dot of an
# abbreviation ('e.g.', 'i.e.', 'etc.', 'vs.')
_SENTENCE_END = re.compile(r'(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<!\bvs)\.\s+')


def _first_sentence(text):
    """The first sentence of a description, single-spaced.

    Abbreviations such as 'e.g.' do not end the sentence.
    """
    text = ' '.join(str(text).split())
    parts = _SENTENCE_END.split(text, maxsplit=1)
    if len(parts) > 1:
        return parts[0] + '.'
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


def _confirm_marker(tool):
    """Whether the tool is gated on user confirmation by the policy."""
    return bool(tool.get('_meta', {}).get('org.debops/confirm'))


def _tool_flags(tool):
    """The flag list of a tool: behavior hints plus the confirm gate."""
    flags = annotation_flags(tool.get('annotations', {}))
    if _confirm_marker(tool):
        flags.append('confirm')
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
        flags = _tool_flags(tool)
        suffix = ' [{}]'.format(', '.join(flags)) if flags else ''
        lines.append('    {}  {}{}'.format(
                tool['name'].ljust(width),
                _shorten(tool.get('description', '')),
                suffix))
    return lines


# Descriptions wrap at this width, so the output stays readable when
# piped regardless of the terminal
WRAP_WIDTH = 76


def _wrap_block(prefix, text):
    """Wrap text after a prefix; continuation lines align under it."""
    wrapper = textwrap.TextWrapper(
            width=WRAP_WIDTH,
            initial_indent=prefix,
            subsequent_indent=' ' * len(prefix))
    return wrapper.wrap(' '.join(str(text).split()))


def format_tool(tool):
    """Render one tool in detail, with its parameters, for humans.

    Descriptions are shown in full, wrapped; the list view truncates
    instead.
    """
    if tool.get('description'):
        lines = _wrap_block('{} — '.format(tool['name']),
                            tool['description'])
    else:
        lines = [tool['name']]
    flags = _tool_flags(tool)
    if flags:
        lines.append('annotations: {}'.format(', '.join(flags)))
    if _confirm_marker(tool):
        lines.append('confirmation: asked before the operation runs')
    schema = tool.get('inputSchema', {})
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if properties:
        lines.append('parameters:')
        name_width = max(len(name) + (10 if name in required else 0)
                         for name in properties)
        for name, prop in sorted(properties.items()):
            marker = ', required' if name in required else ''
            column = (name + marker).ljust(name_width)
            prefix = '    {}  ({}): '.format(column,
                                             prop.get('type', '?'))
            lines.extend(_wrap_block(prefix,
                                     prop.get('description', '')))
    else:
        lines.append('parameters: (none)')
    lines.extend(format_examples(tool['name']))
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
