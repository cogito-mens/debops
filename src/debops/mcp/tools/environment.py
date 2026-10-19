# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The environment tools of the MCP server.

The environment the server's tool subprocesses run with comes from the
process, the project, and the project MCP environment files:
'.debops/mcp/environment' (plain variables, tracked by git) and
'.debops/mcp/.env' (tokens, never tracked). Values are served only
from the tracked file; everywhere else is names only.
"""

import os
import re
import subprocess

from .common import error_result, exclusive_project, text_result
from .files import _atomic_write, _is_git_tracked_project

# The MCP environment files, in merge order (the last wins)
MCP_ENV_FILES = ('environment', '.env')

# A dotenv variable name
ENV_NAME_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _mcp_env_path(project, name):
    return os.path.join(project.path, '.debops', 'mcp', name)


def _read_tracked(project):
    """The tracked environment file as (name, value) pairs."""
    from .common import _read_env_file
    path = _mcp_env_path(project, 'environment')
    if not os.path.isfile(path):
        return []
    return sorted(_read_env_file(path).items())


def tool_environment_list(project, policy, arguments):
    name_filter = str(arguments.get('filter') or '').lower()

    def keep(name):
        return not name_filter or name_filter in name.lower()

    tracked = {name: value
               for name, value in _read_tracked(project) if keep(name)}
    tokens = [name for name in sorted(_token_names(project))
              if keep(name)]
    project_names = [name for name in sorted(project.config._env_vars)
                     if keep(name)]
    process_names = [name for name in sorted(os.environ) if keep(name)]

    structured = {'tracked': tracked, 'tokens': tokens,
                  'project': project_names, 'process': process_names}

    shown = len(tracked) + len(tokens) + len(project_names) \
        + len(process_names)
    lines = ['# Environment of the MCP server',
             '{} variable(s)'.format(shown)]
    sections = [
        ('Project MCP environment (.debops/mcp/environment, tracked)',
         ['- {} = {}'.format(name, value)
          for name, value in tracked.items()]),
        ('Project MCP tokens (.debops/mcp/.env, untracked, names only)',
         ['- {}'.format(name) for name in tokens]),
        ('Project environment (.env, .debops/environment)',
         ['- {}'.format(name) for name in project_names]),
        ('Process environment',
         ['- {}'.format(name) for name in process_names]),
    ]
    for title, entries in sections:
        lines.extend(['', '## ' + title])
        if entries:
            lines.extend(entries)
        else:
            lines.append('(none)')
    return text_result('\n'.join(lines), structured=structured)


def _token_names(project):
    """The variable names of the untracked tokens file (never values)."""
    from .common import _read_env_file
    path = _mcp_env_path(project, '.env')
    if not os.path.isfile(path):
        return []
    return list(_read_env_file(path))


def _format_env_value(value):
    """A value as dotenv text: bare when safe, double-quoted else.

    Only backslashes and double quotes are escaped; '${' would be
    interpolated at read time, so values containing it are refused at
    write time.
    """
    if re.fullmatch(r'[A-Za-z0-9_@%+:,./=-]+', value):
        return value
    return '"' + re.sub(r'([\\"])', r'\\\1', value) + '"'


def _upsert_env(text, name, value):
    """Set or unset KEY=VALUE in dotenv text, preserving the rest.

    Returns (new text, changed). With value None the variable is
    removed; a missing one is unchanged.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        key = stripped.split('=', 1)[0].strip()
        if key.startswith('export '):
            key = key[7:].strip()
        if key != name:
            continue
        if value is None:
            del lines[index]
        else:
            lines[index] = '{}={}'.format(name, _format_env_value(value))
        return '\n'.join(lines) + '\n', True
    if value is None:
        return text, False
    lines.append('{}={}'.format(name, _format_env_value(value)))
    return '\n'.join(lines) + '\n', True


def _protect_secret_file(project, path):
    """The tokens file must never enter the git history.

    Refuses outright when the file is already tracked; registers it in
    '.git/info/exclude' (the local, uncommitted ignore list).
    """
    relative = os.path.relpath(path, project.path)
    git_cmd = project.config.raw.get('binaries', {}).get('git', 'git')
    tracked = subprocess.run(
            [git_cmd, 'ls-files', '--error-unmatch', '--', relative],
            cwd=project.path, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode == 0
    if tracked:
        raise ValueError('the tokens file is tracked by git; untrack it '
                         'first: git rm --cached ' + relative)
    exclude = os.path.join(project.path, '.git', 'info', 'exclude')
    with open(exclude) as fp:
        present = '/' + relative in fp.read().splitlines()
    if not present:
        with open(exclude, 'a') as fp:
            fp.write('/' + relative + '\n')


def _write_environment(project, policy, arguments, secret):
    """Shared write path for the environment tools."""
    name = str(arguments.get('name') or '')
    if not ENV_NAME_PATTERN.match(name):
        return error_result('invalid variable name: {!r}'.format(name))
    value = arguments.get('value')
    if value is not None:
        if not isinstance(value, str):
            return error_result('the "value" argument must be a string')
        if '\n' in value or '\r' in value:
            return error_result('the value must be a single line')
        if '${' in value:
            return error_result('the value must not contain "${...}": '
                                'it would be interpolated when read')

    filename = '.env' if secret else 'environment'
    display = '.debops/mcp/' + filename
    path = _mcp_env_path(project, filename)

    # Writes are recoverable and attributable only with git; the tokens
    # file is never committed, but it lives in a tracked project
    if not _is_git_tracked_project(project):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')

    note = '{} {}'.format('environment_set' if value is not None
                          else 'environment_unset', name)
    with exclusive_project(project, policy, note=note):
        old_text = ''
        if os.path.isfile(path):
            with open(path) as fp:
                old_text = fp.read()
        new_text, changed = _upsert_env(old_text, name, value)
        if not changed:
            return text_result('unchanged: {} ({})\n'.format(
                    name, 'not set' if value is None else 'unchanged'))
        if secret:
            try:
                _protect_secret_file(project, path)
            except (ValueError, OSError) as errmsg:
                return error_result(str(errmsg))
            _atomic_write(path, new_text, 0o600, None)
        else:
            try:
                _atomic_write(path, new_text, None, None)
            except OSError as errmsg:
                return error_result('cannot write {}: {}'.format(
                        display, errmsg))
            # The tracked file is committed with provenance, like any
            # write; the tokens file never is
            try:
                project.commit(message='DebOps MCP: ' + note)
            except Exception as errmsg:
                return error_result('the environment was written, but '
                                    'the commit failed: {} (the change '
                                    'is not recorded in git)'
                                    .format(errmsg))

    if value is None:
        return text_result('unset: {} ({})\n'.format(name, display))
    if secret:
        return text_result('set: {} in {} (untracked; the value is '
                           'not shown)\n'.format(name, display))
    return text_result('set: {} = {} ({})\n'.format(
            name, value, display))


def tool_environment_set(project, policy, arguments):
    return _write_environment(project, policy, arguments,
                              secret=bool(arguments.get('secret')))


def tool_environment_unset(project, policy, arguments):
    return _write_environment(project, policy,
                              dict(arguments, value=None),
                              secret=bool(arguments.get('secret')))
