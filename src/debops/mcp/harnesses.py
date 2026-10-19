# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Client harness integration for the MCP server.

'debops mcp configure' writes the harness' client configuration into
the project directory, so that the harness finds the server when it
starts there. Each harness has a default file location and an entry
shape; an existing file is merged, never clobbered, and the entry is
keyed by name, so a rerun updates in place instead of duplicating.

Supported harnesses are the ones with a project-local configuration
file AND a known way to speak the 2026-07-28 protocol revision (or a
documented default which negotiates it):

- OpenCode: '.opencode/opencode.json', with 'protocol' pinned
- Claude Code: '.mcp.json' (the portable file, also read by VS Code's
  Copilot harness); asks stdio servers for the revision by default
- Codex: '.codex/config.toml' (TOML; the project must be trusted)
"""

import json
import os
import tempfile

import toml


def _serve_entry():
    return ['debops', 'mcp', 'serve']


def _socket_entry(socket_path):
    return ['debops', 'mcp', 'connect', socket_path]


def _opencode_entry(socket_path):
    command = _socket_entry(socket_path) if socket_path else _serve_entry()
    return {'type': 'local', 'command': command,
            'protocol': '2026-07-28'}


def _claude_entry(socket_path):
    if socket_path:
        return {'type': 'stdio', 'command': 'debops',
                'args': ['mcp', 'connect', socket_path]}
    return {'type': 'stdio', 'command': 'debops', 'args': ['mcp', 'serve']}


def _codex_entry(socket_path):
    if socket_path:
        return {'command': 'debops',
                'args': ['mcp', 'connect', socket_path]}
    return {'command': 'debops', 'args': ['mcp', 'serve']}


class Harness(object):
    """One supported harness: its file, format and entry shape."""

    def __init__(self, name, default_filename, config_format, path,
                 entry_builder, note=None):
        self.name = name
        self.default_filename = default_filename
        self.format = config_format
        self.path = path
        self.entry = entry_builder
        self.note = note


HARNESSES = {
    'opencode': Harness(
            'opencode', os.path.join('.opencode', 'opencode.json'),
            'json', ('mcp', 'servers', 'debops'), _opencode_entry),
    'claude': Harness(
            'claude', '.mcp.json',
            'json', ('mcpServers', 'debops'), _claude_entry),
    'codex': Harness(
            'codex', os.path.join('.codex', 'config.toml'),
            'toml', ('mcp_servers', 'debops'), _codex_entry,
            note='Codex reads project configuration only in trusted '
                 'projects; whether it negotiates the 2026-07-28 '
                 'protocol revision is unverified'),
}


def _harness_fragment(harness, socket_path):
    """The entry as a nested dict along the harness' key path."""
    fragment = harness.entry(socket_path)
    for key in reversed(harness.path):
        fragment = {key: fragment}
    return fragment


def fragment_for_display(harness_name, socket_path=None):
    """The harness configuration fragment as text, for hand-merging
    when a file cannot be rewritten safely."""
    harness = HARNESSES[harness_name]
    fragment = _harness_fragment(harness, socket_path)
    if harness.format == 'toml':
        return toml.dumps(fragment)
    return json.dumps(fragment, indent=2)


def _merge(base, fragment):
    """Merge fragment into base recursively; dicts only."""
    for key, value in fragment.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        elif isinstance(value, dict) and key not in base:
            base[key] = value
        elif isinstance(value, dict):
            raise ValueError('the key {!r} exists and is not a mapping'
                             .format(key))
        else:
            base[key] = value


def _read_config(path, config_format):
    """Parse an existing configuration file; None when absent."""
    if not os.path.exists(path):
        return None
    with open(path) as fp:
        text = fp.read()
    try:
        if config_format == 'toml':
            data = toml.loads(text)
        else:
            data = json.loads(text)
    except ValueError:
        raise ValueError(
                'the file is not plain {}: merge by hand'.format(
                    config_format.upper()))
    if not isinstance(data, dict):
        raise ValueError('the file does not contain a mapping')
    return data


def _write_atomic(path, content):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.mcp-configure-')
    try:
        with os.fdopen(fd, 'w') as fp:
            fp.write(content)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def configure_harness(project_path, harness_name, filename=None,
                      socket_path=None):
    """Write the harness configuration for the project; return a
    (status, lines) pair for the caller to present.

    Raises ValueError with the refusal reason when the existing file
    cannot be merged safely.
    """
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise ValueError('unknown harness: {!r} (supported: {})'.format(
                harness_name, ', '.join(sorted(HARNESSES))))
    relative = filename if filename else harness.default_filename
    path = relative if os.path.isabs(relative) \
        else os.path.join(project_path, relative)

    notes = []
    if (harness_name == 'opencode' and filename is None):
        # The file at the project root is also read, but the
        # '.opencode/' one wins for the same server name
        for shadow in ('opencode.json', 'opencode.jsonc'):
            shadow_path = os.path.join(project_path, shadow)
            if os.path.isfile(shadow_path):
                notes.append('note: {} also exists; the .opencode/ '
                             'configuration takes precedence for the '
                             'same server name'.format(shadow))
    if harness.note is not None:
        notes.append('note: ' + harness.note)

    data = _read_config(path, harness.format)
    if harness.format == 'toml' and data is not None:
        with open(path) as fp:
            # A rewrite would silently drop the comments
            if any(line.lstrip().startswith('#')
                   for line in fp.read().splitlines()):
                raise ValueError(
                        'the file carries comments, which a rewrite '
                        'would lose: merge by hand')
    status = 'updated' if data is not None else 'created'
    if data is None:
        data = {}

    _merge(data, _harness_fragment(harness, socket_path))
    if harness.format == 'toml':
        content = toml.dumps(data)
    else:
        content = json.dumps(data, indent=2) + '\n'

    if os.path.isfile(path):
        with open(path) as fp:
            if fp.read() == content:
                status = 'unchanged'
    if status != 'unchanged':
        _write_atomic(path, content)

    command = ' '.join(_socket_entry(socket_path)
                       if socket_path else _serve_entry())
    lines = ['{}: {} ({}: {})'.format(
            status.capitalize(), relative, harness_name, command)]
    lines.extend(notes)
    if status == 'unchanged':
        lines.append('The configuration is already up to date.')
    return status, lines
