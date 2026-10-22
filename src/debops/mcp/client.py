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
import tempfile
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


def default_socket_path(project=None):
    """The well-known socket path of a project's daemon.

    The runtime directory holds '<project-key>.sock' — the run-log
    naming minus the view (one daemon per project, its view
    switchable inside), computed from the project path on both sides,
    so two same-named projects stay apart and the directory layout
    never leaks into the name. A documentation-mode daemon answers at
    'documentation.sock'. A long name or a deep runtime directory is
    truncated to fit the AF_UNIX path limit; the hash prefix keeps it
    unique.
    """
    base = os.environ.get('XDG_RUNTIME_DIR')
    if not base:
        base = os.path.join(tempfile.gettempdir(),
                            'debops-mcp-{}'.format(os.getuid()))
    directory = os.path.join(base, 'debops', 'mcp')
    if project is None:
        return _fit_socket_path(directory, 'documentation')
    from .runs import project_key
    return _fit_socket_path(directory, project_key(project, view=''))


def _fit_socket_path(directory, key):
    """The socket path, the name truncated to fit the AF_UNIX limit."""
    # 108 bytes on Linux, the terminator included; keep a margin
    path = os.path.join(directory, key + '.sock')
    overflow = len(path.encode('utf-8')) - 100
    if overflow <= 0:
        return path
    hash_part, _dash, name = key.partition('-')
    name = name[:-overflow] if overflow < len(name) else ''
    return os.path.join(directory,
                        '{}-{}.sock'.format(hash_part, name))


class UnixClient(object):
    """Send requests to an MCP server on a UNIX socket, one at a time.

    Requests are synchronous and answered in order, so no response
    correlation is needed. With 'legacy' the client speaks the
    2025-era revision: an 'initialize' handshake when the connection
    opens, and no per-request '_meta' envelope after it.
    """

    def __init__(self, path, timeout=5, capabilities=None,
                 legacy=False):
        self._path = os.path.abspath(os.path.expanduser(path))
        self._timeout = timeout
        self._capabilities = capabilities
        self.legacy = legacy
        # What the server said about itself (the version and the
        # protocol revision), once known
        self.server_version = None
        self.protocol_version = None
        # The bearer token of an authenticated session, riding the
        # '_meta' of every request while set
        self.bearer = None
        self._handshaken = False
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
        if self.legacy and not self._handshaken:
            self._legacy_handshake()

    def _legacy_handshake(self):
        """The 2025-era opening: 'initialize', answered with the era's
        revision, then the 'notifications/initialized' notification."""
        response = self.request('initialize', {
                'protocolVersion': _server.LEGACY_PROTOCOL_VERSIONS[0],
                'capabilities': self._capabilities or {},
                'clientInfo': {'name': 'debops',
                               'version': _server.__version__}})
        if 'error' in response:
            raise ConnectionError('the legacy handshake was refused: {}'
                                  .format(response['error'].get(
                                          'message', 'unknown error')))
        self._handshaken = True
        # The answer carries the server's identity for the client UI
        result = response.get('result') or {}
        self.protocol_version = result.get('protocolVersion')
        self.server_version = (result.get('serverInfo') or {}).get(
                'version')
        self.notify('notifications/initialized')

    def request_raw(self, method, params=None):
        """Send one request; return the raw response line (bytes)."""
        self._connect()
        # The request metadata rides in 'params._meta', per the spec;
        # the legacy era carries the client state in the handshake
        request_params = dict(params or {})
        if not self.legacy:
            request_params['_meta'] = request_meta(self._capabilities)
        if self.bearer is not None and not self.legacy:
            request_params['_meta'][_server.TRUST_TOKEN_KEY] = \
                self.bearer
        message = {'jsonrpc': '2.0', 'id': self._next_id,
                   'method': method, 'params': request_params}
        self._next_id += 1
        try:
            self._connection.sendall(json.dumps(message).encode('utf-8')
                                     + b'\n')
            data = rpc.read_message(self._reader)
        except socket.timeout:
            raise TimeoutError('no response within {} seconds'
                               .format(self._timeout))
        except (ConnectionResetError, BrokenPipeError):
            # The server closed the connection without a response; a
            # fast close can already break the write. Drop the dead
            # connection so the next call reconnects
            self.close()
            raise EOFError('the server closed the connection without '
                           'a response')
        if data is None:
            # The same drop on a clean close
            self.close()
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
        notify_params = dict(params or {})
        if not self.legacy:
            notify_params['_meta'] = request_meta(self._capabilities)
        message = {'jsonrpc': '2.0', 'method': method,
                   'params': notify_params}
        self._connection.sendall(json.dumps(message).encode('utf-8')
                                 + b'\n')

    def request_streaming(self, method, params=None):
        """Send a request which answers with a notification stream,
        closed by the completion; iterate it. Closing the connection
        ends the stream server-side. The per-request timeout does not
        apply: a stream idles indefinitely between events."""
        self._connect()
        request_params = dict(params or {})
        if not self.legacy:
            request_params['_meta'] = request_meta(self._capabilities)
        if self.bearer is not None and not self.legacy:
            request_params['_meta'][_server.TRUST_TOKEN_KEY] = \
                self.bearer
        request_id = self._next_id
        self._next_id += 1
        message = {'jsonrpc': '2.0', 'id': request_id,
                   'method': method, 'params': request_params}
        self._connection.sendall(json.dumps(message).encode('utf-8')
                                 + b'\n')
        self._connection.settimeout(None)
        try:
            while True:
                data = rpc.read_message(self._reader)
                if data is None:
                    raise EOFError('the server closed the connection')
                message = json.loads(data)
                if message.get('id') == request_id:
                    if 'error' in message:
                        details = (message['error'].get('data', {})
                                   or {})
                        raise RuntimeError(details.get(
                                'details') or message['error'].get(
                                        'message', 'unknown error'))
                    return
                yield message
        finally:
            if self._connection is not None:
                try:
                    self._connection.settimeout(self._timeout)
                except OSError:
                    pass

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
    'project.status': [''],
    'project.files': ['path=ansible/inventory'],
    'project.read': ['path=ansible/inventory/hosts'],
    'file.write': ['path=resources/scripts/hello.sh '
                   'content="#!/bin/sh" mode=0755'],
    'file.delete': ['path=playbooks/deploy.yml'],
    'view.list': [''],
    'view.switch': ['view=webservers'],
    'view.create': ['view=webservers'],
    'keyring.fetch': ['url=https://keybase.io/drybjed/key.asc'],
    'overrides.write': ['path=templates/nginx/nginx.conf.j2 '
                        'content=...'],
    'config.get': ['key=project.default_view'],
    'environment.list': [''],
    'environment.set': ['name=REGION value=eu-central-1'],
    'environment.unset': ['name=REGION'],
    'inventory.query': ['action=graph',
                        'action=host host=web1.example.com'],
    'inventory.spec': ['spec="version: 0\\nfiles:\\n  hosts.yml: ..." '
                       'dry_run=true'],
    'secret.paths': ['pattern=**/password'],
    'playbook.list': ['filter=nginx'],
    'playbook.info': ['playbook=service/nginx'],
    'collection.playbooks': ['', 'collection=debops.debops'],
    'collection.roles': ['filter=nginx'],
    'collection.require': ['name=community.docker'],
    'collection.remove': ['name=community.docker'],
    'collection.sync': [''],
    'role.info': ['name=nginx'],
    'role.vars': ['name=nginx'],
    'role.argspec': ['name=nginx'],
    'role.defaults': ['name=nginx',
                      'name=nginx detail=full filter=nginx__www'],
    'role.tasks': ['name=nginx'],
    'role.handlers': ['name=nginx'],
    'role.templates': ['name=nginx'],
    'role.files': ['name=nginx'],
    'check': ['playbook=service/nginx',
              'playbook=site limit=web1.example.com'],
    'run': ['playbook=service/nginx',
            'playbook=site limit=web1.example.com'],
    'execution.list': [''],
    'execution.output': ['id=3 kind=changed',
                         'id=last pattern="^fatal" context=5'],
    'execution.purge': ['older_than=86400'],
    'ansible.doc': ['plugin=ansible.builtin.service',
                    'type=lookup plugin=debops.debops.dpkg_divert'],
    'ansible.ping': ['hosts=webservers', 'hosts=all become=true'],
    'ansible.facts': ['hosts=web1.example.com',
                      'hosts=all filter=ansible_distribution*'],
    'project.pull': [''],
    'project.push': [''],
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
    for tool in sorted(tools, key=lambda tool: tool['name']):
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
    for resource in sorted(resources, key=lambda resource:
                           resource['name']):
        lines.append('    {}  {}'.format(
                resource['name'].ljust(width),
                _shorten(resource.get('description', ''))))
    return lines


def format_prompts(prompts):
    """Render a prompts/list result as lines for humans."""
    lines = ['prompts ({}):'.format(len(prompts))]
    width = max([len(prompt['name']) for prompt in prompts] + [0])
    for prompt in sorted(prompts, key=lambda prompt: prompt['name']):
        lines.append('    {}  {}'.format(
                prompt['name'].ljust(width),
                _shorten(prompt.get('description', ''))))
    return lines
