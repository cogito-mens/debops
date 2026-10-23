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
# includes git-crypt key material), encfs key material, Python virtual
# environments and dotenv files (which hold tokens)
READ_DENY_COMPONENTS = frozenset((
        'secret', '.git', '.encfs.secret', '.encfs6.keyfile',
        '.encfs6.xml', '__pycache__', 'venv', '.env'))


def is_denied_component(part):
    """Whether a path component is denied to the read and write tools."""
    return part in READ_DENY_COMPONENTS or part.startswith('.env.')


# ansible-playbook options the execution tools may pass through;
# 'project.mcp.execute_options' restricts the set
# The ansible-playbook options an execution may carry. The default
# set is the safe one: the pattern narrowers and the verification
# modes, which never touch the hosts beyond the check itself. The
# rest are opt-in — the project policy must name them explicitly.
EXECUTE_OPTIONS = ('limit', 'tags', 'skip_tags', 'syntax_check',
                   'list_hosts', 'list_tags', 'list_tasks',
                   'forks', 'force_handlers', 'flush_cache',
                   'start_at_task')

DEFAULT_EXECUTE_OPTIONS = ('limit', 'tags', 'skip_tags', 'syntax_check',
                           'list_hosts', 'list_tags', 'list_tasks')

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

    @classmethod
    def for_documentation(cls):
        """The policy of the documentation mode (no project directory).

        Everything project-bound is off; the bundled DebOps content and
        the collections the environment provides are served read-only.
        """
        policy = cls.__new__(cls)
        policy.project = None
        policy.view_root = None
        policy.confined = False
        policy.allow = []
        policy.deny = []
        policy.read_deny = []
        policy.write_allow = []
        policy.execute = 'none'
        policy.execute_timeout = 3600
        policy.sync_interval = 60
        policy.idle_timeout = 0
        policy.trust_ttl = 43200
        policy.elevate_ttl = 900
        policy.confirm = set()
        policy.keyring_sources = []
        policy.overrides = False
        policy.custom_tools = False
        policy.environment_deny = []
        policy.confirm_required = True
        policy.min_free_bytes = 64 * 1024 * 1024
        policy.bundled_content = True
        policy.external_content = True
        policy.project_content = False
        policy.local_playbooks = False
        policy.ansible_hosts = []
        policy.execute_hosts = []
        policy.become = False
        policy.execute_diff = False
        policy.execute_debug = False
        policy.view_lock = False
        policy.execute_options = []
        policy.execute_user_override = {}
        return policy

    def __init__(self, project):
        self.project = project
        # The confinement of the account (the ambient
        # 'mcp.login.confined') is layered over the project
        # configuration by the daemon at startup — a project must
        # never un-confine itself, so the knob is not read here
        self.confined = False
        # Mirror the view root computation in AnsibleInventory: in legacy
        # projects the inventory lives in '<project>/ansible', in modern
        # projects each view has its own root under 'ansible/views'
        if project.project_type == 'legacy':
            self.view_root = os.path.realpath(
                    os.path.join(project.path, 'ansible'))
        elif project.view and project.view in project.config.raw.get(
                'views', {}):
            self.view_root = os.path.realpath(os.path.join(
                    project.path, 'ansible', 'views', project.view))
        else:
            # No active view: the default view is unset (a deliberate
            # configuration — the command line requires --view then,
            # or a view directory as the working directory) or names a
            # view which is not registered. The server starts anyway;
            # the view-bound tools answer with guidance
            self.view_root = None

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

        # Paths the write tools may touch, as fnmatch globs. The default
        # covers the debops.resources drop-in directory and every
        # location the playbook runner resolves: the view's playbooks,
        # the shared 'ansible/playbooks' (including the loose roles in
        # its 'roles/'), and the project-root 'playbooks' of established
        # projects. The built-in deny list (secrets, key material, ...)
        # always wins
        write_allow = mcp_config.get(
                'write_allow',
                ['resources/**', 'playbooks/**', 'ansible/playbooks/**',
                 '/playbooks/**'])
        if isinstance(write_allow, str):
            write_allow = [write_allow]
        # A leading slash is the project-root anchor; keep it
        self.write_allow = [str(entry).rstrip('/')
                            for entry in write_allow or []]
        if not self.write_allow:
            logger.notice('All project writes disabled by policy')

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
            if self.execute_timeout < 0:
                raise ValueError()
        except (TypeError, ValueError):
            self.execute_timeout = 3600
        if self.execute != 'none':
            timeout = ('unlimited' if self.execute_timeout == 0
                       else '{}s'.format(self.execute_timeout))
            logger.notice('Playbook execution enabled: %s '
                          '(timeout %s)', self.execute, timeout)

        # The run logs of the executions, kept in the user's cache
        # directory: how many per project view, and for how long
        try:
            self.run_logs_keep = int(mcp_config.get('run_logs_keep', 10))
        except (TypeError, ValueError):
            self.run_logs_keep = 10
        try:
            self.run_logs_max_age = int(
                    mcp_config.get('run_logs_max_age', 7 * 24 * 60 * 60))
        except (TypeError, ValueError):
            self.run_logs_max_age = 7 * 24 * 60 * 60

        # Minimum interval between git syncs, so that the tools cannot
        # be used to hammer the origin
        try:
            self.sync_interval = int(mcp_config.get('sync_interval', 60))
        except (TypeError, ValueError):
            self.sync_interval = 60

        # How long a socket daemon idles without a client before it
        # shuts down, in seconds; 0 runs forever. A daemon spawned by
        # a client gets an hour when neither this nor the command
        # line answers
        try:
            self.idle_timeout = int(mcp_config.get('idle_timeout', 0))
        except (TypeError, ValueError):
            self.idle_timeout = 0

        # The trust lifetimes, in seconds, sliding with use: the
        # bearer token of an authenticated session, and the elevation
        # of a trusted one
        try:
            self.trust_ttl = int(mcp_config.get('trust_ttl', 43200))
        except (TypeError, ValueError):
            self.trust_ttl = 43200
        try:
            self.elevate_ttl = int(mcp_config.get('elevate_ttl', 900))
        except (TypeError, ValueError):
            self.elevate_ttl = 900

        # Tools which ask the user for confirmation before they run, via
        # an elicitation request. The confirmation is a courtesy for the
        # user, not a security boundary: the client answers it, so the
        # 'execute' gate above remains the authoritative control
        confirm = mcp_config.get('confirm',
                                 ['run', 'check', 'project.push',
                                  'keyring.fetch', 'environment.set',
                                  'environment.unset',
                                  'collection.require',
                                  'collection.remove'])
        if isinstance(confirm, str):
            confirm = [confirm]
        self.confirm = set(str(name) for name in confirm or [])

        # The hosts the keyring tool may fetch GPG keys from, as fnmatch
        # globs (matched against the URL or keyserver hostname); empty
        # means any HTTPS host — the fingerprint pin and the user
        # confirmation carry the trust
        self.keyring_sources = [str(entry) for entry in
                                mcp_config.get('keyring_sources', [])
                                or []]

        # The overrides write tool injects content into read-only
        # roles, run on the managed hosts at the next playbook
        # execution; it is not registered unless the project enables it
        overrides = mcp_config.get('overrides', False)
        if isinstance(overrides, str):
            overrides = strtobool(overrides)
        self.overrides = bool(overrides)
        if self.overrides:
            logger.notice('The overrides write tool enabled by policy')

        # Custom script tools ('.debops/mcp/tools/') run code on the
        # controller as the server's account; they are not registered
        # unless the project enables them, and every call is confirmed
        custom_tools = mcp_config.get('custom_tools', False)
        if isinstance(custom_tools, str):
            custom_tools = strtobool(custom_tools)
        self.custom_tools = bool(custom_tools)
        if self.custom_tools:
            logger.notice('Custom script tools enabled by policy')

        # Additional variable names the environment tools refuse to
        # set; the built-in list (the loader, interpreter and Ansible
        # code paths) always applies and cannot be lifted
        environment_deny = mcp_config.get('environment_deny', [])
        if isinstance(environment_deny, str):
            environment_deny = [environment_deny]
        self.environment_deny = [str(entry)
                                 for entry in environment_deny or []]

        # A client which cannot elicit is refused confirm-gated
        # operations: security is the default, not an opt-in. Setting
        # this false is the deliberate opt-out for a trusted client
        # which cannot ask (the skip is logged)
        confirm_required = mcp_config.get('confirm_required', True)
        if isinstance(confirm_required, str):
            confirm_required = strtobool(confirm_required)
        self.confirm_required = bool(confirm_required)
        if not self.confirm_required:
            logger.notice('Confirmation is optional by policy: clients '
                          'which cannot elicit proceed anyway')

        # Free disk space the write operations require on the project's
        # filesystem; below this they refuse, because a git operation
        # which dies mid-write can leave a broken repository. An
        # absolute floor: a percentage means nothing on its own
        try:
            self.min_free_bytes = (int(mcp_config.get('min_free_mb', 64))
                                   * 1024 * 1024)
        except (TypeError, ValueError):
            self.min_free_bytes = 64 * 1024 * 1024

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

        # The project's own MCP content ('.debops/mcp/skills/' and
        # '.debops/mcp/prompts/') is served when present; the directory
        # is the opt-in
        content = mcp_config.get('project_content', True)
        if isinstance(content, str):
            content = strtobool(content)
        self.project_content = bool(content)
        if not self.project_content:
            logger.notice('Project MCP content disabled by policy')

        # Whether playbooks living in the project itself may run. They
        # are agent-writable content: a playbook can execute commands on
        # the managed hosts even in check mode ('check_mode: no' on a
        # task), so untrusted environments should run only the
        # collection content
        local = mcp_config.get('local_playbooks', True)
        if isinstance(local, str):
            local = strtobool(local)
        self.local_playbooks = bool(local)
        if not self.local_playbooks:
            logger.notice('Project-local playbooks cannot run by policy')

        # A glob allowlist of hosts the exec tools may contact; empty
        # means every host in the inventory is allowed
        self.ansible_hosts = [str(entry) for entry in
                              mcp_config.get('ansible_hosts', []) or []]
        # Same, for hosts a playbook execution may touch
        self.execute_hosts = [str(entry) for entry in
                              mcp_config.get('execute_hosts', []) or []]

        # Whether the ad-hoc host tools escalate privileges by default.
        # The DebOps local facts report package and file state, which
        # reads as false negatives without root; the per-call 'become'
        # argument still wins. Playbook runs are not affected: roles
        # escalate in their own tasks
        become = mcp_config.get('become', False)
        if isinstance(become, str):
            become = strtobool(become)
        self.become = bool(become)
        if self.become:
            logger.notice('Privilege escalation by default for the '
                          'ad-hoc host tools (project.mcp.become)')

        # Whether check-mode runs pass '--diff'. The diff renders the
        # files a template task would write — with their secrets — into
        # the tool result, which goes to the model; the per-host recap
        # is the agent-usable answer without it
        execute_diff = mcp_config.get('execute_diff', False)
        if isinstance(execute_diff, str):
            execute_diff = strtobool(execute_diff)
        self.execute_diff = bool(execute_diff)
        if self.execute_diff:
            logger.notice('Check-mode diffs enabled by policy '
                          '(rendered file content, with its secrets, '
                          'reaches the model)')

        # The active infrastructure view cannot be switched mid-session;
        # the '--view-lock' serve option does the same from the command
        # line, and the two combine
        view_lock = mcp_config.get('view_lock', False)
        if isinstance(view_lock, str):
            view_lock = strtobool(view_lock)
        self.view_lock = bool(view_lock)
        if self.view_lock:
            logger.notice('The infrastructure view is locked by policy '
                          '(project.mcp.view_lock)')

        # Debug runs of playbooks: the verbosity prints task arguments
        # — secrets included — into the answer, which goes to the
        # model. Off by default, like the check-mode diffs
        debug = mcp_config.get('execute_debug', False)
        if isinstance(debug, str):
            debug = strtobool(debug)
        self.execute_debug = bool(debug)
        if self.execute_debug:
            logger.notice('Playbook debugging enabled by policy '
                          '(project.mcp.execute_debug: task arguments, '
                          'secrets included, reach the model)')

        options = mcp_config.get('execute_options')
        if options is None:
            options = DEFAULT_EXECUTE_OPTIONS
        self.execute_options = []
        for option in options:
            canonical = _canonical_execute_option(option)
            if canonical is None:
                logger.warning('Unknown project.mcp.execute_options entry '
                               '%r, ignoring', option)
                continue
            if canonical not in self.execute_options:
                self.execute_options.append(canonical)
        extra = [option for option in self.execute_options
                 if option not in DEFAULT_EXECUTE_OPTIONS]
        missing = [option for option in DEFAULT_EXECUTE_OPTIONS
                   if option not in self.execute_options]
        if extra or missing:
            logger.notice('Execution options set by policy: %s%s',
                          ', '.join(self.execute_options) or '(none)',
                          ' (opted in: {})'.format(', '.join(extra))
                          if extra else '')

        # The bootstrap escape hatch: glob playbook targets mapped to
        # the connection usernames a run may use instead of the
        # inventory default. The default is empty — no override is
        # honored. The tool layer validates the username charset
        override = mcp_config.get('execute_user_override', {})
        if not isinstance(override, dict):
            logger.warning('Ignoring project.mcp.execute_user_override, '
                           'it is not a mapping')
            override = {}
        self.execute_user_override = {}
        for pattern, users in override.items():
            if not isinstance(pattern, str):
                logger.warning('Ignoring the project.mcp.'
                               'execute_user_override entry %r: the '
                               'key must be a glob of a playbook target',
                               pattern)
                continue
            if pattern.endswith(('.yml', '.yaml')):
                logger.warning('The project.mcp.execute_user_override '
                               'entry %r may never match: the playbook '
                               'targets match without the extension',
                               pattern)
            if isinstance(users, str):
                users = [users]
            if not isinstance(users, (list, tuple)):
                logger.warning('Ignoring the project.mcp.'
                               'execute_user_override entry %r: the '
                               'value must be a username or a list of '
                               'them', pattern)
                continue
            names = [str(name) for name in users]
            if not names:
                logger.warning('Ignoring the project.mcp.'
                               'execute_user_override entry %r: the '
                               'username list is empty', pattern)
                continue
            self.execute_user_override[pattern] = names
        if self.execute_user_override:
            logger.notice('Connection user overrides allowed by policy '
                          '(project.mcp.execute_user_override): %s',
                          ', '.join(sorted(self.execute_user_override)))

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
            if is_denied_component(part):
                return True
        for denied in self.read_deny:
            if (relative_path == denied
                    or relative_path.startswith(denied + '/')):
                return True
        return False
