# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The 'overrides_write' tool: write into the project's overrides.

The 'ansible/overrides/' directory injects alternative files, templates
and additional tasks into read-only roles via the '*_src' lookups; the
content runs on the managed hosts at the next playbook execution. The
tool is therefore not registered by default; a project enables it with
'project.mcp.overrides'.
"""

import os

from ..policy import confine_under, is_denied_component
from .common import error_result, require_git_tracked
from .files import (_parse_write_mode, _write_committed)

# The only override subdirectories the '*_src' lookups read
OVERRIDE_KINDS = ('files', 'tasks', 'templates')


def tool_overrides_write(project, policy, arguments):
    relative = str(arguments.get('path') or '').strip('/')
    content = arguments.get('content')
    if not isinstance(content, str):
        return error_result('the "content" argument is required')
    try:
        mode = _parse_write_mode(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    try:
        path = confine_under(os.path.join(project.path, 'ansible',
                                          'overrides'), relative)
    except ValueError:
        return error_result('path escapes the overrides directory: '
                            + relative)
    # The kind is checked on the resolved path, so that a 'files/../'
    # detour cannot land outside the known subdirectories
    canonical = os.path.relpath(
            path, os.path.realpath(os.path.join(
                    project.path, 'ansible', 'overrides')))
    parts = canonical.split('/')
    if parts[0] not in OVERRIDE_KINDS or len(parts) < 2:
        return error_result('overrides are written under files/, tasks/ '
                            'or templates/: ' + relative)
    for part in parts:
        if is_denied_component(part):
            return error_result('path is denied by policy: ' + relative)

    # Writes are recoverable and attributable only with git
    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    return _write_committed(
            project, policy, path, 'ansible/overrides/' + relative,
            content, mode, 'overrides_write ' + relative)
