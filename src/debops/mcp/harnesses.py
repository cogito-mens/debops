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


def _opencode_entry():
    return {'type': 'local', 'command': _serve_entry(),
            'protocol': '2026-07-28',
            # Direct tool calls, not the Code Mode wrapper: the
            # confirmation elicitations and the per-tool UX need them
            'codemode': False}


def _claude_entry():
    return {'type': 'stdio', 'command': 'debops', 'args': ['mcp', 'serve']}


def _codex_entry():
    return {'command': 'debops', 'args': ['mcp', 'serve']}


class Harness(object):
    """One supported harness: its files, format and entry shape.

    'default_filename' is project-relative, 'user_filename' account-
    level ('$XDG_CONFIG_HOME/...' is resolved at call time; '~' too).
    """

    def __init__(self, name, default_filename, config_format, path,
                 entry_builder, note=None, user_filename=None,
                 note_project=None):
        self.name = name
        self.default_filename = default_filename
        self.format = config_format
        self.path = path
        self.entry = entry_builder
        self.note = note
        self.user_filename = user_filename
        self.note_project = note_project


HARNESSES = {
    'opencode': Harness(
            'opencode', os.path.join('.opencode', 'opencode.json'),
            'json', ('mcp', 'servers', 'debops'), _opencode_entry,
            user_filename='$XDG_CONFIG_HOME/opencode/opencode.json'),
    'claude': Harness(
            'claude', '.mcp.json',
            'json', ('mcpServers', 'debops'), _claude_entry,
            user_filename='~/.claude.json'),
    'codex': Harness(
            'codex', os.path.join('.codex', 'config.toml'),
            'toml', ('mcp_servers', 'debops'), _codex_entry,
            note='whether Codex negotiates the 2026-07-28 protocol '
                 'revision is unverified',
            note_project='Codex reads project configuration only in '
                         'trusted projects',
            user_filename='~/.codex/config.toml'),
}


def _user_config_path(harness):
    """The user-level configuration file of the harness, resolved at
    call time (XDG_CONFIG_HOME differs per process)."""
    filename = harness.user_filename
    if filename is None:
        return None
    if filename.startswith('$XDG_CONFIG_HOME/'):
        xdg_config = os.environ.get('XDG_CONFIG_HOME') or os.path.join(
                os.path.expanduser('~'), '.config')
        return os.path.join(xdg_config,
                            filename[len('$XDG_CONFIG_HOME/'):])
    return os.path.expanduser(filename)


# The AGENTS.md section written by 'mcp configure --agents'; the
# markers delimit it so a rerun updates in place
AGENTS_BEGIN = '<!-- debops mcp: begin -->'
AGENTS_END = '<!-- debops mcp: end -->'

AGENTS_SECTION = AGENTS_BEGIN + '''
## MCP server

This project is served by the DebOps MCP server (the `debops` tools).
Prefer them over reading the project files directly: they enforce the
confinement policy (secrets and key material are never served) and
commit writes to git automatically. Browse with `project.files` and
`project.read`, inspect roles and playbooks with the `role.*` and
`playbook.*` tools, and run `playbook.check` before `playbook.run`.
''' + AGENTS_END


def configure_agents_md(project_path, harness_name=None,
                        print_only=False):
    """Write the AGENTS.md MCP section of a project; (status, lines).

    AGENTS.md is the instruction file the harnesses share (OpenCode,
    Codex, and Claude Code when no CLAUDE.md exists). The marked
    section is created, replaced in place, or appended; a project is
    required unless printing.
    """
    if project_path is None and not print_only:
        raise ValueError('not a DebOps project directory (the agents '
                         'file is written at the project root)')
    notes = []
    if (harness_name == 'claude' and project_path is not None
            and os.path.isfile(os.path.join(project_path,
                                            'CLAUDE.md'))):
        notes.append('note: Claude Code reads CLAUDE.md first; import '
                     'the section there with @AGENTS.md')
    if print_only:
        lines = ['The AGENTS.md section, not written to {}:'.format(
                os.path.join(project_path, 'AGENTS.md')
                if project_path else 'AGENTS.md')]
        lines.extend(notes)
        lines.append('')
        lines.append(AGENTS_SECTION)
        return 'printed', lines

    path = os.path.join(project_path, 'AGENTS.md')
    content = None
    if os.path.isfile(path):
        with open(path) as fp:
            content = fp.read()
    if content is None:
        new_content = AGENTS_SECTION + '\n'
        status = 'created'
    elif AGENTS_BEGIN in content and AGENTS_END in content:
        before = content[:content.index(AGENTS_BEGIN)]
        after = content[content.index(AGENTS_END) + len(AGENTS_END):]
        new_content = before + AGENTS_SECTION + after
        status = 'updated' if new_content != content else 'unchanged'
    else:
        new_content = (content.rstrip('\n') + '\n\n'
                       + AGENTS_SECTION + '\n')
        status = 'updated'
    if status != 'unchanged':
        _write_atomic(path, new_content)
    lines = ['{}: AGENTS.md'.format(status.capitalize())]
    lines.extend(notes)
    return status, lines


def _harness_fragment(harness):
    """The entry as a nested dict along the harness' key path."""
    fragment = harness.entry()
    for key in reversed(harness.path):
        fragment = {key: fragment}
    return fragment


def fragment_for_display(harness_name):
    """The harness configuration fragment as text, for hand-merging
    when a file cannot be rewritten safely."""
    harness = HARNESSES[harness_name]
    fragment = _harness_fragment(harness)
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
                      user=False, print_only=False):
    """Write the harness configuration; return a (status, lines) pair
    for the caller to present.

    The default mode writes the project-local file of the harness (a
    project directory is required). With 'user' the account-level file
    of the harness is the target and needs no project; the spawned
    server serves a project when launched inside one and the
    documentation mode otherwise. Printing instead of writing is the
    safe fallback for anything uncertain ('printed' status), and asks
    for no directory at all. Raises ValueError with the refusal reason
    when an existing file cannot be merged safely.
    """
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise ValueError('unknown harness: {!r} (supported: {})'.format(
                harness_name, ', '.join(sorted(HARNESSES))))

    notes = []
    if user:
        if filename is not None:
            path = (filename if os.path.isabs(filename)
                    else os.path.abspath(filename))
            relative = filename
        else:
            path = _user_config_path(harness)
            if path is None:
                notes.append('note: no known account-level '
                             'configuration file for this harness')
                print_only = True
            relative = path
    else:
        if project_path is None:
            if not print_only:
                raise ValueError('not a DebOps project directory (the '
                                 'project-local configuration needs one; '
                                 'the account level does not: --user)')
            relative = filename if filename else harness.default_filename
            path = relative
        else:
            relative = filename if filename else harness.default_filename
            path = relative if os.path.isabs(relative) \
                else os.path.join(project_path, relative)
            if (harness_name == 'opencode' and filename is None):
                # The file at the project root is also read, but the
                # '.opencode/' one wins for the same server name
                for shadow in ('opencode.json', 'opencode.jsonc'):
                    shadow_path = os.path.join(project_path, shadow)
                    if os.path.isfile(shadow_path):
                        notes.append(
                                'note: {} also exists; the .opencode/ '
                                'configuration takes precedence for '
                                'the same server name'.format(shadow))
    if harness.note is not None:
        notes.append('note: ' + harness.note)
    if harness.note_project is not None and not user:
        notes.append('note: ' + harness.note_project)

    if print_only:
        lines = ['Configuration for {} ({}), not written to {}:'.format(
                harness_name, ' '.join(_serve_entry()), relative)]
        lines.extend(notes)
        lines.append('')
        lines.append(fragment_for_display(harness_name))
        return 'printed', lines

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

    _merge(data, _harness_fragment(harness))
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

    command = ' '.join(_serve_entry())
    lines = ['{}: {} ({}: {})'.format(
            status.capitalize(), relative, harness_name, command)]
    lines.extend(notes)
    if status == 'unchanged':
        lines.append('The configuration is already up to date.')
    return status, lines
