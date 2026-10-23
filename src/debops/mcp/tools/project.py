# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe the DebOps project itself."""

import os

from .common import dirty_paths, text_result
from .executions import project_busy


def tool_project_status(project, policy, arguments):
    views = sorted(project.config.raw.get('views', {}).keys())
    active = policy.view_root is not None
    if active and project.view not in views:
        views.append(project.view)

    if active:
        inventory_path = os.path.join(policy.view_root, 'inventory')
        secret_path = os.path.join(policy.view_root, 'secret')
        if os.path.exists(os.path.join(policy.view_root,
                                       '.encfs.secret')):
            encryption = 'encfs'
        elif os.path.exists(os.path.join(project.path, '.git',
                                         'git-crypt', 'keys')):
            encryption = 'git-crypt'
        else:
            encryption = 'none'
        inventory = ('present' if os.path.isdir(inventory_path)
                     else 'absent')
        secrets = ('present' if os.path.exists(secret_path)
                   else 'absent')
        view_root_present = os.path.isdir(policy.view_root)
    else:
        inventory = secrets = encryption = 'no active view'
        view_root_present = False

    structured = {
        'name': project.name,
        'path': project.path,
        'type': project.project_type,
        'view': project.view if active else None,
        'views': views,
        'viewLocked': policy.view_lock,
        'viewRoot': policy.view_root,
        'viewRootPresent': view_root_present,
        'inventory': inventory,
        'secrets': secrets,
        'encryption': encryption,
    }
    # The pending working-tree changes are the honest state: the tool
    # commits are scoped, so an operator's edits stay pending and
    # visible instead of riding along into someone else's commit
    pending = dirty_paths(project)
    if pending is not None:
        structured['pendingChanges'] = sorted(pending)[:50]
        structured['pendingChangesCount'] = len(pending)
    busy = project_busy(project, policy)
    structured['runInFlight'] = busy
    lines = [
            'Project: {}'.format(project.name),
            'Path: {}'.format(project.path),
            'Type: {}'.format(project.project_type),
    ]
    if busy:
        lines.append('Run: in flight — watch: execution.list, '
                     'execution.output')
    if active:
        lines.append('View: {}{}'.format(
                project.view, ' (locked)' if policy.view_lock else ''))
        lines.append('Views available: {}'.format(', '.join(views)))
        lines.append('View root: {}{}'.format(
                policy.view_root,
                '' if view_root_present else ' (missing)'))
    else:
        lines.append('View: none — list them with "view.list", '
                     'activate one with "view.switch"')
        lines.append('Views available: {}'.format(
                ', '.join(views) if views else 'none registered'))
    lines.append('Inventory: {}'.format(inventory))
    lines.append('Secrets: {}'.format(secrets))
    lines.append('Encryption: {}'.format(encryption))
    if pending is not None:
        if pending:
            shown = sorted(pending)[:50]
            lines.append('Pending changes ({}): {}{}'.format(
                    len(pending), ', '.join(shown),
                    ', …' if len(pending) > 50 else ''))
        else:
            lines.append('Pending changes: none')
    return text_result('\n'.join(lines), structured=structured)
