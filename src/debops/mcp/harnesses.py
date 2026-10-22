# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Client harness integration for the MCP server.

'debops mcp configure' writes the harness' client configuration into
the project directory, so that the harness finds the server when it
starts there. Each harness has a default file location and an entry
shape; an existing file is merged, never clobbered, and the entry is
keyed by name, so a rerun updates in place instead of duplicating.

An entry either launches the server per invocation ('debops mcp
serve', the default) or bridges the harness to a running daemon
('--connect' writes 'debops mcp connect <socket>'). Connect entries
are per-instance and always named: the default entry must never be
repurposed into a bridge — outside a project it launches the
documentation mode, which a stray socket would silently replace.
'--docs' enforces that mode in the launched server, even inside a
project.

Supported harnesses are the ones with a project-local configuration
file AND a known protocol story — the 2026-07-28 revision, a
documented negotiation of it, or the 2025-era revision served with
'--legacy':

- OpenCode: '.opencode/opencode.json', with 'protocol' pinned
- Claude Code: '.mcp.json' (the portable file, also read by VS Code's
  Copilot harness); asks stdio servers for the revision by default
- Codex: '.codex/config.toml' (TOML; the project must be trusted)
- Pi: '.pi/mcp.json' (the project must be trusted); speaks the 2025-era
  protocol, so its entry serves with '--legacy'
- Antigravity CLI: '.agents/mcp_config.json'; Cursor: '.cursor/
  mcp.json'; Kiro: '.kiro/settings/mcp.json' — the 2025-era protocol,
  so their entries serve with '--legacy'
- Zed: '.zed/settings.json' ('context_servers'; JSONC — a commented
  settings file is refused for a hand-merge); the 2025-era protocol,
  so its entry serves with '--legacy'
- Goose, Cline, JetBrains AI Assistant and Amp have no writable
  project file; their entries print for a hand-merge (the YAML
  snippet, the JSON fragment or the CLI command)
"""

import json
import os
import re
import stat
import tempfile

import toml

# Entry names follow the strictest harness's rule (Pi)
NAME_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')

# The default entry name; reserved for the launcher, never a bridge
DEFAULT_NAME = 'debops'


def _serve_argv(verbosity=0, log=None, legacy=False, docs=False):
    """The argv of an entry which launches the server per call."""
    argv = ['debops', 'mcp', 'serve']
    if docs:
        # The documentation mode, enforced: read-only even inside a
        # project directory
        argv.append('--docs')
    if legacy:
        argv.append('--legacy')
    if log is not None:
        argv.append('--log')
        if log:
            argv.append(log)
    if verbosity:
        argv.append('-' + 'v' * verbosity)
    return argv


def _connect_argv(path):
    """The argv of an entry which bridges to a running daemon."""
    return ['debops', 'mcp', 'connect', path]


def _opencode_entry(argv):
    return {'type': 'local', 'command': argv,
            'protocol': '2026-07-28',
            # Direct tool calls, not the Code Mode wrapper: the
            # confirmation elicitations and the per-tool UX need them
            'codemode': False}


def _claude_entry(argv):
    return {'type': 'stdio', 'command': argv[0], 'args': argv[1:]}


def _plain_entry(argv):
    return {'command': argv[0], 'args': argv[1:]}


def _pi_entry(argv):
    return {'command': argv[0], 'args': argv[1:],
            # Direct tool calls, not the codemode wrapper: the
            # confirmation elicitations and the per-tool UX need them
            'exposure': 'direct',
            'description': 'Manage the DebOps project (inventory, '
                           'playbooks, executions)'}


def _zed_entry(argv):
    # Zed's 'context_servers' shape (the settings file is JSONC; a
    # commented one is refused for a hand-merge)
    return {'command': argv[0], 'args': argv[1:], 'env': {}}


def _goose_text(argv):
    """The Goose 'extensions' snippet ('~/.config/goose/config.yaml')."""
    return ('extensions:\n'
            '  debops:\n'
            '    name: DebOps\n'
            '    type: stdio\n'
            '    cmd: debops\n'
            '    args: {}\n'
            '    enabled: true\n'
            '    timeout: 300\n'.format(json.dumps(argv[1:])))


def _amp_text(argv):
    """The Amp CLI registration command."""
    return 'amp mcp add debops -- {}\n'.format(' '.join(argv))


class Harness(object):
    """One supported harness: its files, format and entry shape.

    'default_filename' is project-relative, 'user_filename' account-
    level ('$XDG_CONFIG_HOME/...' is resolved at call time; '~' too).
    Both None marks a print-only harness: nothing writable is known,
    so 'configure' prints the fragment for a hand-merge. 'print_text'
    renders the printed form when the generic fragment does not fit
    (a YAML snippet, a CLI command). 'path' is the key path of the
    servers mapping; the entry name is appended at configure time.
    """

    def __init__(self, name, default_filename, config_format, path,
                 entry_builder, note=None, user_filename=None,
                 note_project=None, legacy=False, print_text=None):
        self.name = name
        self.default_filename = default_filename
        self.format = config_format
        self.path = path
        self.entry = entry_builder
        self.note = note
        self.user_filename = user_filename
        self.note_project = note_project
        # The harness speaks only the 2025-era protocol revision; its
        # spawned entries serve with '--legacy'
        self.legacy = legacy
        self.print_text = print_text


HARNESSES = {
    'opencode': Harness(
            'opencode', os.path.join('.opencode', 'opencode.json'),
            'json', ('mcp', 'servers'), _opencode_entry,
            user_filename='$XDG_CONFIG_HOME/opencode/opencode.json'),
    'claude': Harness(
            'claude', '.mcp.json',
            'json', ('mcpServers',), _claude_entry,
            user_filename='~/.claude.json'),
    'codex': Harness(
            'codex', os.path.join('.codex', 'config.toml'),
            'toml', ('mcp_servers',), _plain_entry,
            note='whether Codex negotiates the 2026-07-28 protocol '
                 'revision is unverified',
            note_project='Codex reads project configuration only in '
                         'trusted projects',
            user_filename='~/.codex/config.toml'),
    'pi': Harness(
            'pi', os.path.join('.pi', 'mcp.json'),
            'json', ('mcpServers',), _pi_entry,
            note="Pi cannot answer confirmation elicitations; "
                 "confirm-gated tools refuse while "
                 "'project.mcp.confirm_required' is on",
            note_project='Pi reads project configuration only in '
                         'trusted projects',
            user_filename='~/.pi/agent/mcp.json',
            legacy=True),
    'antigravity': Harness(
            'antigravity', os.path.join('.agents', 'mcp_config.json'),
            'json', ('mcpServers',), _plain_entry,
            note='legacy support only',
            legacy=True),
    'cursor': Harness(
            'cursor', os.path.join('.cursor', 'mcp.json'),
            'json', ('mcpServers',), _plain_entry,
            note='legacy support only',
            user_filename='~/.cursor/mcp.json',
            legacy=True),
    'kiro': Harness(
            'kiro', os.path.join('.kiro', 'settings', 'mcp.json'),
            'json', ('mcpServers',), _plain_entry,
            note='legacy support only',
            user_filename=os.path.join('~', '.kiro', 'settings',
                                       'mcp.json'),
            legacy=True),
    'zed': Harness(
            'zed', os.path.join('.zed', 'settings.json'),
            'json', ('context_servers',), _zed_entry,
            note='legacy support only',
            user_filename='$XDG_CONFIG_HOME/zed/settings.json',
            legacy=True),
    # No writable file is known for these; 'configure' prints the
    # fragment for a hand-merge
    'goose': Harness(
            'goose', None, 'json', ('mcpServers',), _plain_entry,
            note='legacy support only; the snippet goes into '
                 '~/.config/goose/config.yaml',
            print_text=_goose_text, legacy=True),
    'cline': Harness(
            'cline', None, 'json', ('mcpServers',), _plain_entry,
            note='legacy support only; paste into the Cline MCP '
                 'settings (MCP Servers, then Configure)',
            legacy=True),
    'jetbrains': Harness(
            'jetbrains', None, 'json', ('mcpServers',), _plain_entry,
            note='legacy support only; paste at Settings -> Tools -> '
                 'AI Assistant -> Model Context Protocol',
            legacy=True),
    'amp': Harness(
            'amp', None, 'json', ('mcpServers',), _plain_entry,
            note='legacy support only',
            print_text=_amp_text, legacy=True),
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
`playbook.*` tools, and run `check` before `run`.
''' + AGENTS_END

# The documentation-mode variant ('mcp configure --docs'): read-only
# content, and no nudging towards the execution tools the mode lacks
AGENTS_SECTION_DOCS = AGENTS_BEGIN + '''
## MCP server

The DebOps MCP server (the `debops` tools) serves the DebOps
documentation here, read-only; no project is managed through it.
Inspect roles with the `role.*` tools, playbooks with `playbook.list`
and `playbook.info`, collections with `collection.roles` and
`collection.playbooks`, and plugin documentation with `ansible.doc`.
''' + AGENTS_END


def configure_agents_md(project_path, harness_name=None,
                        print_only=False, docs=False):
    """Write the AGENTS.md MCP section of a project; (status, lines).

    AGENTS.md is the instruction file the harnesses share (OpenCode,
    Codex, and Claude Code when no CLAUDE.md exists). The marked
    section is created, replaced in place, or appended; a project is
    required unless printing. With 'docs' the section describes the
    documentation mode the entry enforces.
    """
    if project_path is None and not print_only:
        raise ValueError('not a DebOps project directory (the agents '
                         'file is written at the project root)')
    section = AGENTS_SECTION_DOCS if docs else AGENTS_SECTION
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
        lines.append(section)
        return 'printed', lines

    path = os.path.join(project_path, 'AGENTS.md')
    content = None
    if os.path.isfile(path):
        with open(path) as fp:
            content = fp.read()
    if content is None:
        new_content = section + '\n'
        status = 'created'
    elif AGENTS_BEGIN in content and AGENTS_END in content:
        before = content[:content.index(AGENTS_BEGIN)]
        after = content[content.index(AGENTS_END) + len(AGENTS_END):]
        new_content = before + section + after
        status = 'updated' if new_content != content else 'unchanged'
    else:
        new_content = (content.rstrip('\n') + '\n\n'
                       + section + '\n')
        status = 'updated'
    if status != 'unchanged':
        _write_atomic(path, new_content)
    lines = ['{}: AGENTS.md'.format(status.capitalize())]
    lines.extend(notes)
    return status, lines


def _entry_argv(harness, connect, verbosity, log, docs=False):
    """The command an entry runs: the bridge, or the server launch."""
    if connect is not None:
        return _connect_argv(connect)
    return _serve_argv(verbosity=verbosity, log=log,
                       legacy=harness.legacy, docs=docs)


def _harness_fragment(harness, argv, name):
    """The entry as a nested dict along the harness' key path."""
    fragment = {name: harness.entry(argv)}
    for key in reversed(harness.path):
        fragment = {key: fragment}
    return fragment


def fragment_for_display(harness_name, connect=None, name=DEFAULT_NAME,
                         verbosity=0, log=None, docs=False):
    """The harness configuration fragment as text, for hand-merging
    when a file cannot be rewritten safely."""
    harness = HARNESSES[harness_name]
    argv = _entry_argv(harness, connect, verbosity, log, docs=docs)
    if harness.print_text is not None:
        return harness.print_text(argv)
    fragment = _harness_fragment(harness, argv, name)
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


def _target_path(harness, project_path, filename, user,
                 print_only=False):
    """The file to write and its display name, plus the notes."""
    notes = []
    if user:
        if filename is not None:
            path = (filename if os.path.isabs(filename)
                    else os.path.abspath(filename))
            relative = filename
        else:
            path = _user_config_path(harness)
            relative = path if path is not None else '(no known file)'
        return path, relative, notes
    if project_path is None:
        if not print_only:
            raise ValueError('not a DebOps project directory (the '
                             'project-local configuration needs one; '
                             'the account level does not: --user)')
        # Printing asks for no directory at all
        relative = filename or harness.default_filename \
            or '(no known file)'
        return relative, relative, notes
    relative = filename if filename else harness.default_filename
    if relative is None:
        # A print-only harness has no file to write
        return None, '(no known file)', notes
    path = relative if os.path.isabs(relative) \
        else os.path.join(project_path, relative)
    if harness.name == 'opencode' and filename is None:
        # The file at the project root is also read, but the
        # '.opencode/' one wins for the same server name
        for shadow in ('opencode.json', 'opencode.jsonc'):
            shadow_path = os.path.join(project_path, shadow)
            if os.path.isfile(shadow_path):
                notes.append(
                        'note: {} also exists; the .opencode/ '
                        'configuration takes precedence for '
                        'the same server name'.format(shadow))
    return path, relative, notes


def _read_for_rewrite(path, config_format):
    """Read the existing configuration file for a rewrite; the TOML
    files keep their comments, which a rewrite would lose."""
    data = _read_config(path, config_format)
    if config_format == 'toml' and data is not None:
        with open(path) as fp:
            # A rewrite would silently drop the comments
            if any(line.lstrip().startswith('#')
                   for line in fp.read().splitlines()):
                raise ValueError(
                        'the file carries comments, which a rewrite '
                        'would lose: merge by hand')
    return data


def _check_connect_path(path, notes):
    """The socket path sanity of a bridge entry."""
    if os.path.exists(path):
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            raise ValueError('not a socket: ' + path)
    else:
        notes.append('note: {} does not exist; the daemon creates the '
                     'socket when it starts'.format(path))


def configure_harness(project_path, harness_name, filename=None,
                      user=False, print_only=False, name=DEFAULT_NAME,
                      connect=None, verbosity=0, log=None, docs=False):
    """Write the harness configuration; return a (status, lines) pair
    for the caller to present.

    The default entry launches the server per invocation. With
    'connect' the entry bridges the harness to a running daemon's
    socket — always under its own 'name': the default entry must never
    be repurposed into a bridge, since outside a project the launcher
    serves the documentation mode. 'verbosity' and 'log' ride the
    launched command; a bridge ignores them (the daemon's own flags
    govern). With 'docs' the launched server always runs in the
    documentation mode, even inside a project — an enforcement, so it
    never combines with a bridge. The default mode writes the
    project-local file of the harness (a project directory is
    required). With 'user' the account-level file of the harness is
    the target and needs no project; the spawned server serves a
    project when launched inside one and the documentation mode
    otherwise. Printing instead of writing is the safe fallback for
    anything uncertain ('printed' status), and asks for no directory
    at all. Raises ValueError with the refusal reason when an existing
    file cannot be merged safely.
    """
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise ValueError('unknown harness: {!r} (supported: {})'.format(
                harness_name, ', '.join(sorted(HARNESSES))))
    if not NAME_PATTERN.match(name):
        raise ValueError('invalid entry name {!r} (letters, digits, '
                         "'-' and '_' only)".format(name))
    if connect is not None and name == DEFAULT_NAME:
        raise ValueError("a bridge entry needs its own --name: the "
                         "default entry launches the server (in the "
                         "documentation mode outside a project)")
    if docs and connect is not None:
        raise ValueError("a bridge takes the daemon's mode; --docs "
                         "needs a launched entry")

    path, relative, notes = _target_path(harness, project_path,
                                         filename, user,
                                         print_only=print_only)
    if path is None and filename is None:
        # Nothing writable is known for this harness; the fragment
        # prints for a hand-merge
        if user:
            notes.append('note: no known account-level configuration '
                         'file for this harness')
        else:
            notes.append('note: no writable configuration file is '
                         'known for this harness')
        print_only = True
    if connect is not None:
        _check_connect_path(connect, notes)
        if verbosity or log is not None:
            notes.append("note: the bridge is a dumb relay; the "
                         "daemon's own flags govern its logging")
    if harness.note is not None:
        notes.append('note: ' + harness.note)
    if harness.note_project is not None and not user:
        notes.append('note: ' + harness.note_project)

    argv = _entry_argv(harness, connect, verbosity, log, docs=docs)
    command = ' '.join(argv)
    if print_only:
        if path is None and filename is None:
            lines = ['Configuration for {} ({}), to add by hand:'
                     .format(harness_name, command)]
        else:
            lines = ['Configuration for {} ({}), not written to '
                     '{}:'.format(harness_name, command, relative)]
        lines.extend(notes)
        lines.append('')
        lines.append(fragment_for_display(harness_name,
                                          connect=connect, name=name,
                                          verbosity=verbosity, log=log,
                                          docs=docs))
        return 'printed', lines

    data = _read_for_rewrite(path, harness.format)
    status = 'updated' if data is not None else 'created'
    if data is None:
        data = {}

    _merge(data, _harness_fragment(harness, argv, name))
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

    lines = ['{}: {} ({}: {})'.format(
            status.capitalize(), relative,
            name if name != DEFAULT_NAME else harness_name, command)]
    lines.extend(notes)
    if status == 'unchanged':
        lines.append('The configuration is already up to date.')
    return status, lines


def _entry_argv_of(entry):
    """The command argv of a harness entry, across the shapes."""
    command = entry.get('command')
    if isinstance(command, list):
        return command
    if isinstance(command, str):
        return [command] + list(entry.get('args') or [])
    return None


def _entry_is_ours(entry):
    """Whether an entry launches one of our commands."""
    argv = _entry_argv_of(entry)
    return (argv is not None and len(argv) >= 3
            and os.path.basename(argv[0]) == 'debops'
            and argv[1] == 'mcp' and argv[2] in ('serve', 'connect'))


def _entry_is_bridge(entry):
    argv = _entry_argv_of(entry)
    return argv is not None and argv[1:3] == ['mcp', 'connect']


def _remove_entries(project_path, harness_name, filename=None,
                    user=False, name=None, bridges_only=False):
    """Remove our entries from the harness' configuration file.

    With 'name', only that entry (and with 'bridges_only' — the
    disconnect case — only when it is a bridge: the launcher goes only
    with a full remove). Without it, every entry of ours goes. Returns
    (status, lines); status is 'removed', 'unchanged' (nothing of ours
    present) or 'not configured' (no file).
    """
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise ValueError('unknown harness: {!r} (supported: {})'.format(
                harness_name, ', '.join(sorted(HARNESSES))))
    path, relative, notes = _target_path(harness, project_path,
                                         filename, user)
    if path is None or not os.path.isfile(path):
        return 'not configured', ['Nothing of ours is configured for '
                                  '{} here.'.format(harness_name)]

    data = _read_for_rewrite(path, harness.format)
    servers = data
    for key in harness.path:
        servers = servers.get(key) if isinstance(servers, dict) else None
    if not isinstance(servers, dict):
        return 'unchanged', ['No server entries in {}.'.format(relative)]

    if name is not None:
        entry = servers.get(name)
        if entry is None:
            return 'unchanged', ['No entry {!r} in {}.'.format(
                    name, relative)]
        if not _entry_is_ours(entry):
            raise ValueError(
                    'the entry {!r} is not ours; refusing to remove '
                    'it (the command is: {})'.format(
                            name, ' '.join(_entry_argv_of(entry) or [])))
        if bridges_only and not _entry_is_bridge(entry):
            raise ValueError(
                    'the entry {!r} launches the server, it is not a '
                    'bridge; remove it with --remove'.format(name))
        doomed = [name]
    else:
        doomed = [key for key, entry in servers.items()
                  if _entry_is_ours(entry)]
        if not doomed:
            return 'unchanged', ['Nothing of ours in {}.'.format(
                    relative)]

    for key in doomed:
        del servers[key]
    if harness.format == 'toml':
        content = toml.dumps(data)
    else:
        content = json.dumps(data, indent=2) + '\n'
    _write_atomic(path, content)
    lines = ['Removed: {} ({})'.format(relative, ', '.join(sorted(doomed)))]
    lines.extend(notes)
    return 'removed', lines


def disconnect_harness(project_path, harness_name, name, filename=None,
                       user=False):
    """Remove the named bridge entry of ours from the harness'
    configuration file."""
    return _remove_entries(project_path, harness_name,
                           filename=filename, user=user, name=name,
                           bridges_only=True)


def remove_harness(project_path, harness_name, filename=None,
                   user=False, name=None):
    """Remove all of our entries from the harness' configuration file
    (with 'name', only that one)."""
    return _remove_entries(project_path, harness_name,
                           filename=filename, user=user, name=name)
