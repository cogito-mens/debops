# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""View lifecycle tools: list, switch and create infrastructure views.

A modern project can hold many views; the server's active view is a
property of the server itself, like the checked-out branch of a shared
working tree — the substrate (one git repository, the secrets mounts)
is global, so the view is global too and every response carries it in
the '_meta' stamp. A switch affects all connected clients and is
serialized against executions through the exclusive project lock.
"""

import logging
import os
import re
import subprocess

from .common import (error_result, exclusive_project, json_document,
                     parse_output_format, text_result)

logger = logging.getLogger(__name__)

# View names created through the MCP server are single path segments
# without dashes: the serve --instance names are '<project>-<view>',
# split at the last dash, and dashes would break that parsing. The
# command line stays permissive (nested views); the server is not the
# place for them
VIEW_NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_]*$')

# The answer the view-bound tools give while no view is active
NO_ACTIVE_VIEW = ('no active infrastructure view: list the views with '
                  '"view_list", activate one with "view_switch", or '
                  'create one with "view_create"')


def tool_view_list(project, policy, arguments):
    """The registered views, the active one and the lock state."""
    try:
        output_format = parse_output_format(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    registered = project.config.raw.get('views', {})
    views = []
    for name in sorted(registered):
        views.append({
            'name': name,
            'active': name == project.view,
            'present': os.path.isdir(os.path.join(
                    project.path, 'ansible', 'views', name)),
        })
    active = project.view if policy.view_root is not None else None

    if output_format == 'json':
        return text_result(json_document({
            'active': active, 'locked': policy.view_lock,
            'views': views}))

    lines = ['# Infrastructure views']
    if active is None:
        lines.append('Active: none — ' + NO_ACTIVE_VIEW)
    else:
        lines.append('Active: {}{}'.format(
                active, ' (locked)' if policy.view_lock else ''))
    lines.append('')
    if not views:
        lines.append('No views are registered in the project '
                     'configuration.')
    for entry in views:
        line = '- `{}`'.format(entry['name'])
        marks = []
        if entry['active']:
            marks.append('active')
        if not entry['present']:
            marks.append('missing on disk')
        if marks:
            line += ' — {}'.format(', '.join(marks))
        lines.append(line)
    return text_result('\n'.join(lines) + '\n')


def _secrets_unlocked(project):
    """The encryption method in an unlocked state, or None.

    Unlocked secrets are plaintext state in the working tree and the
    lock-down rearranges the workspace (locks commit pending changes,
    git-crypt rewrites files); a view switch must not happen
    underneath that. The checks mirror the inventory layer's own
    detection: a live encfs mount on the view's secret directory, or
    git-crypt's smudge filter configured (its unlocked state).
    """
    if project.project_type == 'legacy':
        secret_dir = os.path.join(project.path, 'ansible', 'secret')
    else:
        secret_dir = os.path.join(project.path, 'ansible', 'views',
                                  project.view, 'secret')
    if os.path.ismount(secret_dir):
        return 'encfs'
    git = project.config.raw.get('binaries', {}).get('git', 'git')
    try:
        result = subprocess.run(
                [git, 'config', '--local', '--get',
                 'filter.git-crypt.smudge'],
                cwd=project.path, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, universal_newlines=True,
                timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.stdout.strip():
        return 'git-crypt'
    return None


def tool_view_switch(project, policy, arguments, server):
    """Switch the active view, rebuilding the policy and the tools."""
    name = str(arguments.get('view') or '')
    if not name:
        return error_result('the "view" argument is required')
    if policy.view_lock:
        return error_result('the infrastructure view is locked '
                            '(project.mcp.view_lock or --view-lock)')
    if project.project_type == 'legacy':
        return error_result('legacy projects have a single '
                            'infrastructure view')
    unlocked = _secrets_unlocked(project)
    if unlocked is not None:
        return error_result('the project secrets are currently '
                            'unlocked ({}); lock them before switching '
                            'the view (debops project lock)'.format(
                                unlocked))
    if name not in project.config.raw.get('views', {}):
        return error_result('view not found: {}'.format(name))
    if name == project.view and policy.view_root is not None:
        return text_result('unchanged: the active view is already '
                           '{}\n'.format(name))

    previous = project.view if policy.view_root is not None else None
    with exclusive_project(project, policy, note='view_switch ' + name):
        server.switch_view(name)
    logger.notice('Infrastructure view switched: %s -> %s',
                  previous or '(none)', name)
    return text_result('# View switched: {} → {}\n\nAll connected '
                       'clients now see the `{}` view.\n'.format(
                           previous or '(none)', name, name))


def tool_view_create(project, policy, arguments):
    """Create a view with 'debops project mkview' semantics."""
    name = str(arguments.get('view') or '')
    if not name:
        return error_result('the "view" argument is required')
    if not VIEW_NAME_PATTERN.match(name):
        return error_result('invalid view name: {!r} (lowercase '
                            'letters, digits and underscores; dashes '
                            'would break the --instance naming)'.format(
                                name))
    if project.project_type == 'legacy':
        return error_result('legacy projects have a single '
                            'infrastructure view; views are a modern '
                            'project feature')
    if name in project.config.raw.get('views', {}):
        return error_result('view already exists: {}'.format(name))

    with exclusive_project(project, policy, note='view_create ' + name):
        try:
            project.mkview(name)
        except (ValueError, OSError) as errmsg:
            return error_result('cannot create the view: {}'
                                .format(errmsg))
    return text_result('# View created: {}\n\nActivate it with '
                       '"view_switch".\n'.format(name))
