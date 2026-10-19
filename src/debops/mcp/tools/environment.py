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

from .common import mcp_environ, text_result

# The MCP environment files, in merge order (the last wins)
MCP_ENV_FILES = ('environment', '.env')


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
    lines = ['# Environment of the MCP server']
    shown = 0

    tracked = _read_tracked(project)
    section = [entry for entry in tracked
               if not name_filter or name_filter in entry[0].lower()]
    lines.extend(['', '## Project MCP environment '
                  '(.debops/mcp/environment, tracked)'])
    if section:
        lines.extend('- {} = {}'.format(name, value)
                     for name, value in section)
        shown += len(section)
    else:
        lines.append('(none)')

    for title, names in (
            ('Project MCP tokens (.debops/mcp/.env, untracked, '
             'names only)',
             sorted(_token_names(project))),
            ('Project environment (.env, .debops/environment)',
             sorted(project.config._env_vars)),
            ('Process environment', sorted(os.environ))):
        filtered = [name for name in names
                    if not name_filter or name_filter in name.lower()]
        lines.extend(['', '## ' + title])
        if filtered:
            lines.extend('- {}'.format(name) for name in filtered)
            shown += len(filtered)
        else:
            lines.append('(none)')

    lines.insert(1, '{} variable(s)'.format(shown))
    return text_result('\n'.join(lines))


def _token_names(project):
    """The variable names of the untracked tokens file (never values)."""
    from .common import _read_env_file
    path = _mcp_env_path(project, '.env')
    if not os.path.isfile(path):
        return []
    return list(_read_env_file(path))
