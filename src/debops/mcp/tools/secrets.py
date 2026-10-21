# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which locate project secrets without reading them.

Secrets in a DebOps project are path-based files in the view's
'secret/' directory, referenced from the inventory with
'lookup("password", secret + "/path/to/file")'. The tool returns
matching paths only — it reads directory entries, never file contents.
"""

import glob
import logging
import os

from ...ansible.inventory import AnsibleInventory
from .common import error_result, text_result
from .common import exclusive_project

logger = logging.getLogger(__name__)

# A glob result larger than this is truncated
SECRET_LIST_LIMIT = 500


def _validate_pattern(pattern):
    """Reject glob patterns which could escape the secrets directory."""
    if os.path.isabs(pattern):
        return False
    if '..' in pattern.split('/'):
        return False
    return True


def tool_secret_paths(project, policy, arguments):
    pattern = str(arguments.get('pattern') or '**')
    if not _validate_pattern(pattern):
        return error_result('invalid pattern (absolute paths and ".." '
                            'are not allowed): ' + pattern)

    secret_root = os.path.join(policy.view_root, 'secret')
    if not os.path.isdir(secret_root):
        # An empty list is the structured answer, so that a harness
        # which shows only the structured side sees it
        return text_result('no secrets directory in this view '
                           '({})'.format(os.path.relpath(
                                   secret_root, project.path)),
                           structured=[])

    # Listing requires unlocked secrets; unlock them only if they are
    # not already unlocked, and lock again only in that case. The
    # execution lock serializes this with playbook runs, so secrets are
    # never closed mid-run.
    with exclusive_project(project, policy, note='secret.paths'):
        inventory = AnsibleInventory(project, name=project.view)
        unlocked = False
        try:
            unlocked = inventory.unlock()
            matches = []
            real_root = os.path.realpath(secret_root)
            for candidate in glob.glob(os.path.join(secret_root, pattern),
                                       recursive=True):
                if not os.path.isfile(candidate):
                    continue
                # A symlinked directory inside secret/ must not lead
                # outside of it
                if os.path.commonpath((real_root,
                                       os.path.realpath(candidate))) \
                        != real_root:
                    continue
                matches.append(os.path.relpath(candidate, secret_root))
        except ChildProcessError as errmsg:
            return error_result('cannot unlock the project secrets: {}'
                                .format(errmsg))
        finally:
            if unlocked:
                inventory.lock()

    matches = sorted(matches)
    total = len(matches)
    truncated = total > SECRET_LIST_LIMIT
    if truncated:
        matches = matches[:SECRET_LIST_LIMIT]
    if not matches:
        return text_result('no secrets match the pattern: ' + pattern,
                           structured=[])

    lines = ['# Secrets matching \'{}\' ({}{})'.format(
            pattern, len(matches),
            ' — truncated from {}'.format(total) if truncated else ''),
            '']
    lines.extend('- {}'.format(match) for match in matches)
    lines.append('')
    lines.append('Reference them from the inventory with: '
                 '{{ lookup("password", secret + "/<path>") }}')
    return text_result('\n'.join(lines), structured=matches)
