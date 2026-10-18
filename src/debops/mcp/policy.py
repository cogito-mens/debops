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

from ..config import Configuration
from ..utils import strtobool

logger = logging.getLogger(__name__)


class KillSwitch(object):
    """The MCP kill switch: the global 'mcp.enabled' configuration option.

    The value is read from the ambient configuration (package defaults,
    system and user configuration) — never from the project, so that
    a project cannot re-enable a server an administrator disabled. The
    configuration is reloaded when its files change on disk, so
    'debops mcp disable' takes effect without a restart.
    """

    def __init__(self, config_factory=None):
        self._factory = config_factory or Configuration
        self._config = None
        self._config_dirs = []
        self._fingerprint = None

    def _fingerprint_now(self):
        """A fingerprint of the ambient configuration files on disk."""
        fingerprint = []
        for config_dir in self._config_dirs:
            if not os.path.isdir(config_dir):
                fingerprint.append(None)
                continue
            entries = []
            for name in sorted(os.listdir(config_dir)):
                path = os.path.join(config_dir, name)
                if os.path.isfile(path):
                    entries.append((name, os.stat(path).st_mtime_ns))
            fingerprint.append(tuple(entries))
        return tuple(fingerprint)

    def _reload(self):
        config = self._factory()
        self._config_dirs = list(config._config_dirs)
        self._config = config
        self._fingerprint = self._fingerprint_now()

    def is_enabled(self):
        if self._config is None:
            self._reload()
        elif self._fingerprint_now() != self._fingerprint:
            logger.notice('MCP configuration changed, reloading')
            self._reload()
        return bool(self._config.raw.get('mcp', {}).get('enabled', True))


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


# Path components and file names never served by the read tools: the
# secrets directories of the infrastructure views, git metadata (which
# includes git-crypt key material), encfs key material and Python
# virtual environments
READ_DENY_COMPONENTS = frozenset((
        'secret', '.git', '.encfs.secret', '.encfs6.keyfile',
        '.encfs6.xml', '__pycache__', 'venv'))

# ansible-playbook options the execution tools may pass through;
# 'project.mcp.execute_options' restricts the set
EXECUTE_OPTIONS = ('limit', 'tags', 'skip_tags')

# Spellings which no normalization rule can produce: the short flags
EXECUTE_OPTION_ALIASES = {'-l': 'limit', '-t': 'tags'}


def _canonical_execute_option(option):
    """Normalize an execute_options entry to its canonical option name.

    Entries may use the tool name ('limit'), the ansible-playbook flag
    spellings ('--limit', '--skip-tags') or the short flags ('-l', '-t').
    Returns None for anything else.
    """
    option = str(option)
    if option in EXECUTE_OPTION_ALIASES:
        return EXECUTE_OPTION_ALIASES[option]
    normalized = option.lstrip('-').replace('-', '_')
    if normalized in EXECUTE_OPTIONS:
        return normalized
    return None


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

        # Additional project-relative paths denied to the read tools
        self.read_deny = [str(entry).strip('/')
                          for entry in mcp_config.get('read_deny', [])
                          or []]
        for entry in self.read_deny:
            logger.notice('Path denied to read tools by policy: %s', entry)

        # Playbook execution gate: 'none' registers no execution tools,
        # 'check' (default) allows check mode only, 'all' allows changes
        self.execute = str(mcp_config.get('execute', 'check'))
        if self.execute not in ('none', 'check', 'all'):
            logger.warning('Unknown project.mcp.execute value %r, '
                           'using "check"', self.execute)
            self.execute = 'check'
        try:
            self.execute_timeout = int(
                    mcp_config.get('execute_timeout', 3600))
        except (TypeError, ValueError):
            self.execute_timeout = 3600
        if self.execute != 'none':
            logger.notice('Playbook execution enabled: %s '
                          '(timeout %ss)', self.execute, self.execute_timeout)

        # Minimum interval between git syncs, so that the tools cannot
        # be used to hammer the origin
        try:
            self.sync_interval = int(mcp_config.get('sync_interval', 60))
        except (TypeError, ValueError):
            self.sync_interval = 60

        # Which collection content the tools may serve. Bundled is the
        # packaged DebOps collection; external is anything else Ansible
        # can see. The project's own content is always allowed.
        bundled = mcp_config.get('bundled_content', True)
        if isinstance(bundled, str):
            bundled = strtobool(bundled)
        self.bundled_content = bool(bundled)
        external = mcp_config.get('external_content', True)
        if isinstance(external, str):
            external = strtobool(external)
        self.external_content = bool(external)
        if not self.bundled_content or not self.external_content:
            logger.notice('Collection content restricted by policy: '
                          'bundled=%s external=%s',
                          self.bundled_content, self.external_content)

        # A glob allowlist of hosts the exec tools may contact; empty
        # means every host in the inventory is allowed
        self.exec_hosts = [str(entry) for entry in
                           mcp_config.get('exec_hosts', []) or []]

        options = mcp_config.get('execute_options')
        if options is None:
            options = EXECUTE_OPTIONS
        self.execute_options = []
        for option in options:
            canonical = _canonical_execute_option(option)
            if canonical is None:
                logger.warning('Unknown project.mcp.execute_options entry '
                               '%r, ignoring', option)
                continue
            if canonical not in self.execute_options:
                self.execute_options.append(canonical)
        if set(self.execute_options) != set(EXECUTE_OPTIONS):
            logger.notice('Execution options restricted by policy: %s',
                          ', '.join(self.execute_options) or '(none)')

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

    def is_read_denied(self, relative_path):
        """Check if a project-relative path is denied to the read tools.

        The built-in deny list (secrets, key material, virtual
        environments) is extended by the 'project.mcp.read_deny'
        configuration list.
        """
        relative_path = relative_path.strip('/')
        for part in relative_path.split('/'):
            if part in READ_DENY_COMPONENTS:
                return True
        for denied in self.read_deny:
            if (relative_path == denied
                    or relative_path.startswith(denied + '/')):
                return True
        return False
