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

import contextlib
import logging
import sys
import time

from ...config import Configuration
from ...projectdir import ProjectDir
from .common import error_result, text_result
from .common import exclusive_project

logger = logging.getLogger(__name__)


def render_sync_report(report):
    if report['action'] == 'pull':
        if not report['changed']:
            return '# git pull\n\nAlready up to date.\n'
        lines = ['# git pull', '',
                 'before: {}'.format(report['before'][:12]),
                 'after: {}'.format(report['after'][:12]),
                 'files changed: {}'.format(report['files_changed'])]
        if report.get('refreshed'):
            lines.append('project refreshed (ansible.cfg regenerated, '
                         'skeleton recreated)')
        if report.get('refresh_error'):
            lines.append('refresh error: {}'.format(
                    report['refresh_error']))
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


def make_sync_tool(project, policy, sync_state, direction, server=None):
    """Build the 'project_pull' / 'project_push' tool handler."""
    def tool(arguments):
        interval = policy.sync_interval if policy is not None else 60
        # The throttle check and the mark must be atomic with the sync,
        # hence inside the exclusive section
        with exclusive_project(project, policy, note=direction):
            now = time.monotonic()
            remaining = interval - (now - sync_state.get(direction, 0.0))
            if remaining > 0:
                return error_result(
                        'git {} was performed recently; retry in {:.0f} '
                        'seconds'.format(direction, remaining))
            sync_state[direction] = now
            # New skills and prompts can arrive by git; fingerprint the
            # project content before the pull to tell what changed
            before = (server.content_fingerprint()
                      if server is not None and direction == 'pull'
                      else None)
            try:
                report = getattr(project, direction)()
            except ValueError as errmsg:
                return error_result(str(errmsg))
            if direction == 'pull' and report.get('changed'):
                # A pull can change the project configuration, and
                # ansible.cfg is generated from it; git does not track
                # the empty skeleton directories. The server's ProjectDir
                # still holds the pre-pull configuration, so the refresh
                # runs on a fresh instance which reads the configuration
                # as just pulled. The refresh is command-line machinery
                # and prints its status to stdout, which is the protocol
                # channel in stdio mode; send it to the log channel
                # instead
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        fresh = ProjectDir(path=project.path,
                                           config=Configuration(),
                                           view=project.view)
                        fresh.refresh()
                    report['refreshed'] = True
                except Exception as errmsg:
                    # The pull itself succeeded; the refresh is
                    # housekeeping and must not eat its report
                    logger.warning('Refresh after the pull failed: %s',
                                   errmsg)
                    report['refresh_error'] = str(errmsg)
                if before is not None:
                    server.announce_content_changes(before)
                # The pulled configuration may change the policy and
                # the tool surface; reload in place, because a restart
                # would kill the in-flight tasks
                if server is not None:
                    server.reload()
        return text_result(render_sync_report(report),
                           structured=report)
    return tool
