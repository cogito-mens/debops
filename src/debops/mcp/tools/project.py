# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe the DebOps project itself."""

import os

from .common import text_result


def tool_project_status(project, policy, arguments):
    views = sorted(project.config.raw.get('views', {}).keys())
    if project.view not in views:
        views.append(project.view)

    inventory_path = os.path.join(policy.view_root, 'inventory')
    secret_path = os.path.join(policy.view_root, 'secret')
    if os.path.exists(os.path.join(policy.view_root, '.encfs.secret')):
        encryption = 'encfs'
    elif os.path.exists(os.path.join(project.path, '.git', 'git-crypt',
                                     'keys')):
        encryption = 'git-crypt'
    else:
        encryption = 'none'

    lines = [
            'Project: {}'.format(project.name),
            'Path: {}'.format(project.path),
            'Type: {}'.format(project.project_type),
            'View: {}'.format(project.view),
            'Views available: {}'.format(', '.join(views)),
            'View root: {}{}'.format(
                policy.view_root,
                '' if os.path.isdir(policy.view_root) else ' (missing)'),
            'Inventory: {}'.format(
                'present' if os.path.isdir(inventory_path) else 'absent'),
            'Secrets: {}'.format(
                'present' if os.path.exists(secret_path) else 'absent'),
            'Encryption: {}'.format(encryption),
    ]
    return text_result('\n'.join(lines))
