# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which synchronize the project with its git origin.

The tools never take a remote or a branch: the configured 'origin' and
the current branch's upstream are used. Calls are throttled per
direction, so that the tools cannot be used to hammer the origin, and
serialized with playbook executions, so a sync cannot change roles
mid-run.
"""

import logging
import time

from .common import error_result, text_result
from .execute import _EXECUTION_LOCK

logger = logging.getLogger(__name__)


def render_sync_report(report):
    if report['action'] == 'pull':
        if not report['changed']:
            return '# git pull\n\nAlready up to date.\n'
        lines = ['# git pull', '',
                 'before: {}'.format(report['before'][:12]),
                 'after: {}'.format(report['after'][:12]),
                 'files changed: {}'.format(report['files_changed'])]
        if report['commits']:
            lines.extend(['', '## New commits'])
            lines.extend('- {}'.format(subject)
                         for subject in report['commits'])
        return '\n'.join(lines) + '\n'

    if not report['pushed']:
        return ('# git push\n\nNothing to push; the project is up to '
                'date with the origin.\n')
    lines = ['# git push', '',
             'pushed {} commit(s) to the origin:'.format(
                     len(report['commits'])), '']
    lines.extend('- {}'.format(subject) for subject in report['commits'])
    return '\n'.join(lines) + '\n'


def make_sync_tool(project, policy, sync_state, direction):
    """Build the 'project_pull' / 'project_push' tool handler."""
    def tool(arguments):
        interval = policy.sync_interval if policy is not None else 60
        now = time.monotonic()
        remaining = interval - (now - sync_state.get(direction, 0.0))
        if remaining > 0:
            return error_result(
                    'git {} was performed recently; retry in {:.0f} '
                    'seconds'.format(direction, remaining))
        with _EXECUTION_LOCK:
            sync_state[direction] = time.monotonic()
            try:
                report = getattr(project, direction)()
            except ValueError as errmsg:
                return error_result(str(errmsg))
        return text_result(render_sync_report(report))
    return tool
