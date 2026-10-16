# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Policy for the DebOps MCP server.

The policy decides what the server may touch. It is anchored at the
project's current infrastructure view: tools which access files resolve
their paths inside the view's root directory, and escapes are rejected.
Tool access can be restricted with the 'project.mcp.allow' and
'project.mcp.deny' configuration lists; the deny list wins.
"""

import logging
import os

logger = logging.getLogger(__name__)


def confine_under(root, path):
    """Resolve a path inside a root directory, rejecting escapes.

    Returns the canonical absolute path. Raises ValueError when the
    result would fall outside the root, including through symlinks.
    """
    root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root, path))
    if os.path.commonpath((root, candidate)) != root:
        raise ValueError('path escapes its allowed directory: ' + str(path))
    return candidate


class McpPolicy(object):
    """Confinement and access policy for one project view."""

    def __init__(self, project):
        self.project = project
        # Mirror the view root computation in AnsibleInventory: in legacy
        # projects the inventory lives in '<project>/ansible', in modern
        # projects each view has its own root under 'ansible/views'
        if project.project_type == 'legacy':
            view_root = os.path.join(project.path, 'ansible')
        else:
            view_root = os.path.join(project.path, 'ansible', 'views',
                                     project.view)
        self.view_root = os.path.realpath(view_root)

        mcp_config = project.config.raw.get('project', {}).get('mcp', {})
        if not isinstance(mcp_config, dict):
            logger.warning('Ignoring project.mcp configuration, '
                           'it is not a mapping')
            mcp_config = {}
        self.allow = list(mcp_config.get('allow', []) or [])
        self.deny = list(mcp_config.get('deny', []) or [])
        for name in self.deny:
            logger.notice('Tool disabled by policy: %s', name)

    def is_tool_allowed(self, name):
        """Check if a tool may be exposed; the deny list wins."""
        if name in self.deny:
            return False
        if self.allow and name not in self.allow:
            return False
        return True

    def confine(self, path):
        """Resolve a path inside the view root, rejecting escapes."""
        return confine_under(self.view_root, path)
