# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The DebOps MCP server.

Transport-agnostic request dispatch for the 2026-07-28 ("stateless")
revision of the Model Context Protocol. Every request is self-contained;
there is no 'initialize' handshake and no session state, so a transport
can hand any message to handle_bytes() independently of any other.
"""

import base64
import difflib
import json
import logging
import os
import queue
import secrets
import threading
import time

from . import rpc
from . import runs
from . import trust
from .present import make_path_presenter
from .skills import SKILL_URI_PREFIX, SKILLS_EXTENSION
from .tasks import TaskRegistry

from ..constants import DEBOPS_PACKAGE_DATA, DEBOPS_USER_HOME_DIR

logger = logging.getLogger(__name__)

# Detect the DebOps version from the Python module; fall back to a stub in
# a development environment where the version has not been generated
try:
    from ..__version__ import __version__
except ModuleNotFoundError:
    __version__ = '0.0.0'

# The revision of the Model Context Protocol implemented by this server
PROTOCOL_VERSION = '2026-07-28'

# The stateful (2025-era) revisions the legacy mode answers
LEGACY_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26',
                            '2024-11-05')

PROTOCOL_VERSION_KEY = 'io.modelcontextprotocol/protocolVersion'
CLIENT_CAPABILITIES_KEY = 'io.modelcontextprotocol/clientCapabilities'

# The bearer token of a trusted client rides the request '_meta'
TRUST_TOKEN_KEY = 'org.debops/token'
CLIENT_INFO_KEY = 'io.modelcontextprotocol/clientInfo'
SERVER_INFO_KEY = 'io.modelcontextprotocol/serverInfo'

# The Tasks extension identifier
TASKS_EXTENSION = 'io.modelcontextprotocol/tasks'

# A tribute carried in the overhead of every result; '_meta' is
# protocol metadata consumed by clients and never shown to the model,
# and unknown namespaced keys are ignored by design
CLACKS_OVERHEAD = {'org.debops/clacksOverhead': 'GNU Terry Pratchett'}

# The project the server is confined to, so that clients can tell
# multiple instances apart
PROJECT_META_KEY = 'org.debops/project'
VIEW_META_KEY = 'org.debops/view'

SUBSCRIPTION_ID_KEY = 'io.modelcontextprotocol/subscriptionId'


def _reject_cursor(params):
    """Reject a pagination cursor on a list method: the lists are
    never paginated, so any cursor received is an invalid one."""
    if params.get('cursor') is not None:
        raise rpc.JsonRpcError(
                rpc.INVALID_PARAMS, 'Invalid params',
                data={'details': 'invalid cursor: the list is not '
                      'paginated; repeat the request without it'})


# The page size of the unbounded catalogs (the skills and the directory
# listings); the bounded lists stay complete and cursor-free
LIST_PAGE_SIZE = 100


def _encode_cursor(offset):
    """The opaque cursor for a catalog page: an offset, wrapped."""
    return base64.urlsafe_b64encode(
            json.dumps({'offset': offset}).encode('utf-8')
            ).decode('ascii')


def _decode_cursor(cursor):
    """The offset of a cursor; ValueError for anything we never wrote."""
    try:
        data = json.loads(base64.urlsafe_b64decode(
                cursor.encode('ascii')).decode('utf-8'))
        offset = data['offset']
    except (ValueError, UnicodeDecodeError, AttributeError, KeyError,
            TypeError, UnicodeEncodeError):
        raise ValueError('invalid cursor')
    if not isinstance(offset, int) or offset < 0:
        raise ValueError('invalid cursor')
    return offset


def _supports_form_elicitation(capabilities):
    """Whether the client declared form-mode elicitation support."""
    elicitation = capabilities.get('elicitation')
    if not isinstance(elicitation, dict):
        return False
    # An empty capability object declares form mode only
    return 'form' in elicitation or not elicitation


def _confirmation_message(tool_name, arguments):
    """A human-readable summary of the operation to confirm."""
    if tool_name == 'run':
        message = ('Run playbook "{}" and change the managed hosts?'
                   .format(arguments.get('playbook', '?')))
        if arguments.get('limit'):
            message += ' Limited to: {}.'.format(arguments['limit'])
        message += (' The run continues to completion even if this '
                    'session closes; its output lands in the run log.')
        return message
    if tool_name == 'project.push':
        return ("Push the project's committed history to its git origin?")
    if tool_name == 'collection.require':
        message = 'Install the Ansible Collection "{}"'.format(
                arguments.get('name', '?'))
        if arguments.get('version'):
            message += ' (version {})'.format(arguments['version'])
        if arguments.get('source'):
            message += ' from {}'.format(arguments['source'])
        return message + ' into the project?'
    if tool_name == 'collection.remove':
        return 'Remove the Ansible Collection "{}" from the ' \
            'project?'.format(arguments.get('name', '?'))
    if tool_name in ('environment.set', 'environment.unset'):
        verb = 'Set' if tool_name == 'environment.set' else 'Unset'
        message = '{} the environment variable "{}"'.format(
                verb, arguments.get('name', '?'))
        if arguments.get('secret'):
            return message + (' in the untracked tokens file (never '
                              'committed to git)?')
        if tool_name == 'environment.set' and arguments.get('value'):
            return message + ' to "{}" in the tracked environment ' \
                'file?'.format(arguments['value'])
        return message + ' in the tracked environment file?'
    return 'Run the "{}" operation?'.format(tool_name)


class RequestCancelled(Exception):
    """Raised by a tool when its request was cancelled by the client.

    The server sends no response for a cancelled request, per the
    specification.
    """


class RequestContext(object):
    """Context of the request being dispatched.

    Carries the request id and the transport-level notification channel
    (for server-initiated messages such as progress notifications), plus
    the client's progress token, capabilities, identity and process id
    from the request '_meta' and the transport. 'trusted' and
    'elevated' resolve from the bearer token of the request.
    """

    def __init__(self, request_id, notify=None, progress_token=None,
                 client_capabilities=None, client_info=None,
                 peer_pid=None, dead=None):
        self.request_id = request_id
        self.notify = notify
        self.progress_token = progress_token
        self.client_capabilities = (client_capabilities
                                    if isinstance(client_capabilities, dict)
                                    else {})
        self.client_info = (client_info if isinstance(client_info, dict)
                            else {})
        self.peer_pid = peer_pid
        # The trust of the bearer token the request carried, resolved
        # by the dispatcher
        self.trusted = False
        self.elevated = False
        self._dead = dead

    def is_dead(self):
        """Whether the transport channel is gone (EOF, teardown)."""
        return self._dead is not None and self._dead.is_set()


def _client_tag(client_info):
    """The client identity for an audit line, when it identified
    itself — e.g. ' [opencode 2.1.2]'."""
    name = client_info.get('name')
    if not name:
        return ''
    version = client_info.get('version') or ''
    return ' [{}]'.format(' '.join([name, version]).strip())


def _audit_tag(context):
    """The client identity plus the trust state, for an audit line —
    ' [opencode 2.1.2, elevated]'."""
    tag = _client_tag(context.client_info)
    if getattr(context, 'elevated', False):
        state = 'elevated'
    elif getattr(context, 'trusted', False):
        state = 'trusted'
    else:
        return tag
    if tag:
        return tag[:-1] + ', ' + state + ']'
    return ' [{}]'.format(state)


class _ActivityHandler(logging.Handler):
    """Feed the notice-level audit lines to the activity
    subscribers."""

    def __init__(self, server):
        super().__init__(level=logging.NOTICE)
        self.setFormatter(logging.Formatter('%(message)s'))
        self._server = server

    def emit(self, record):
        try:
            self._server._emit_activity(self.format(record))
        except Exception:
            # A notification feed must never break the logged event
            self.handleError(record)


# How often the project configuration files are checked for changes
CONFIG_WATCH_INTERVAL = 10.0  # seconds


def _config_signature(project):
    """A fingerprint of the project's configuration files.

    The 'conf.d' directory is fingerprinted as a listing, so that
    added, renamed and deleted files are noticed as well as edits;
    the format does not matter (the loader accepts TOML, YAML and
    JSON). The legacy '.debops.cfg' is a single file. The ambient
    configuration is left out on purpose: administrator-level changes
    come with a daemon restart, and the kill switch re-reads its own
    files already.
    """
    config_dir = os.path.join(project.path, '.debops', 'conf.d')
    try:
        entries = tuple(
                (name, os.stat(os.path.join(config_dir, name))
                 .st_mtime_ns)
                for name in sorted(os.listdir(config_dir))
                if os.path.isfile(os.path.join(config_dir, name)))
    except OSError:
        entries = None
    try:
        legacy = os.stat(os.path.join(project.path, '.debops.cfg')) \
            .st_mtime_ns
    except OSError:
        legacy = None
    return (entries, legacy)


class ToolContext(object):
    """Request-scoped context passed to tools which need it.

    'cancelled' is an event which is set when the client cancels the
    request via 'notifications/cancelled'; long-running tools should
    poll it and stop. 'progress()' sends a progress notification when
    the client asked for one with a 'progressToken' in the request
    '_meta'. 'peer_pid' identifies the client process on this host,
    'client_info' its self-reported name and version — both for the
    audit log.
    """

    def __init__(self, request_id, cancelled, progress_token=None,
                 notify=None, peer_pid=None, client_capabilities=None,
                 client_info=None, input_responses=None, task=None,
                 trusted=False, elevated=False):
        self.request_id = request_id
        self.cancelled = cancelled
        self.progress_token = progress_token
        self.peer_pid = peer_pid
        self.client_capabilities = (client_capabilities
                                    if isinstance(client_capabilities, dict)
                                    else {})
        self.client_info = (client_info if isinstance(client_info, dict)
                            else {})
        self.input_responses = (input_responses
                                if isinstance(input_responses, dict)
                                else None)
        self.task = task
        self.trusted = trusted
        self.elevated = elevated
        self._notify = notify

    def progress(self, message, progress=None, total=None):
        """Report progress: the task status message, or the
        notification when the client asked with a progress token."""
        if self.task is not None:
            self.task.set_status_message(message)
            return
        if self.progress_token is None or self._notify is None:
            return
        params = {'progressToken': self.progress_token,
                  'message': message}
        if progress is not None:
            params['progress'] = progress
        if total is not None:
            params['total'] = total
        try:
            self._notify({'jsonrpc': '2.0',
                          'method': 'notifications/progress',
                          'params': params})
        except Exception:
            # Progress reporting must never break the request it
            # reports on
            logger.debug('Could not send a progress notification',
                         exc_info=True)


class Tool(object):
    """A single tool exposed by the server."""

    def __init__(self, name, description, input_schema, handler,
                 output_schema=None, needs_context=False,
                 annotations=None, meta=None, self_confirms=False,
                 task_eligible=False):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.handler = handler
        self.needs_context = needs_context
        self.annotations = annotations
        self.meta = meta
        # A tool which runs its own elicitation mid-call (to show
        # computed data in the confirmation) is skipped by the generic
        # confirmation gate
        self.self_confirms = self_confirms
        # A long-running tool may return a task handle instead of
        # blocking, when the client declared the tasks extension
        self.task_eligible = task_eligible

    def describe(self):
        description = {
            'name': self.name,
            'description': self.description,
            'inputSchema': self.input_schema,
        }
        if self.output_schema is not None:
            description['outputSchema'] = self.output_schema
        if self.annotations is not None:
            description['annotations'] = self.annotations
        if self.meta is not None:
            description['_meta'] = self.meta
        return description


class McpServer(object):
    """Dispatch MCP requests to their handlers."""

    def __init__(self, name='debops', version=None, project=None,
                 policy=None, skills=None, prompts=None, killswitch=None,
                 transport=None, legacy=False):
        self.name = name
        self.version = version or __version__
        # The project directory the server is confined to; tools resolve
        # all paths against it
        self.project = project
        self.policy = policy
        # Which transport serves the clients; the discover instructions
        # read it ('stdio' means the harness opened the project
        # directory, 'unix' means the project may live elsewhere)
        self.transport = transport
        # The legacy mode: answer the 2025-era (stateful) revision of
        # the protocol — the initialize handshake, no per-request
        # '_meta' envelope, the old result shape. The era is pinned by
        # the operator, so the handshake needs no negotiation state;
        # requests stay self-contained
        self.legacy = legacy
        # Legacy clients which completed the handshake, by peer pid:
        # their capabilities and identity from 'initialize'
        self._legacy_clients = {}
        # The client-facing presentation of the server-local paths:
        # the account's home is masked to '~' everywhere; a socket
        # server — which may live outside the harness' reach —
        # replaces its content roots with monikers
        self._present = make_path_presenter(
                self._path_monikers(project, transport))
        # A trusted client gets the human form: the home masking
        # stays, the monikers go
        self._present_trusted = make_path_presenter([])
        # The activity feed handler, attached while a trusted client
        # subscribes
        self._activity_handler = None
        # The skill library served as resources; None when absent
        self.skills = skills
        # The prompt library; None when absent
        self.prompts = prompts
        # The kill switch; None means always enabled
        self.killswitch = killswitch
        self.tools = {}
        # Hidden aliases, answered by their canonical tools and never
        # listed; the registration entry points fill them in
        self.aliases = {}
        # Cancellation registry: request id -> cancel event, for the
        # requests currently being handled
        self._active_requests = {}
        self._active_lock = threading.Lock()
        self._methods = {
            'server/discover': self._discover,
            'server/authenticate': self._server_authenticate,
            'server/elevate': self._server_elevate,
            'server/drop': self._server_drop,
            'tools/list': self._tools_list,
            'tools/call': self._tools_call,
            'notifications/cancelled': self._cancelled,
            'subscriptions/listen': self._subscriptions_listen,
            'tasks/get': self._tasks_get,
            'tasks/update': self._tasks_update,
            'tasks/cancel': self._tasks_cancel,
        }
        if self.legacy:
            # The 2025-era handshake and keepalive
            self._methods['initialize'] = self._legacy_initialize
            self._methods['ping'] = self._legacy_ping
            self._methods['notifications/initialized'] = \
                self._legacy_initialized
        # Open subscription streams: request id → filter and event queue
        self._subscriptions = {}
        self._subscriptions_lock = threading.Lock()
        # The task registry: long-running work the client polls for
        self._tasks = TaskRegistry()
        self._tasks.on_task_change = self._emit_task_change
        # The trust: bearer tokens of the authenticated clients
        # (sliding lifetimes), and the rate-limit of failed
        # authentications, per calling process
        self._bearers = {}
        self._bearers_lock = threading.Lock()
        self._auth_failures = {}
        # The reload recipe, provided by the caller which knows how the
        # server was assembled ('debops mcp serve' sets it)
        self._rebuild = None
        # The configuration watcher's stop event and thread, while it
        # runs
        self._config_watch = None
        self._config_watch_thread = None
        if self.skills is not None:
            self._methods['resources/list'] = self._resources_list
            self._methods['resources/read'] = self._resources_read
            self._methods['skills/list'] = self._skills_list
            self._methods['skills/get'] = self._skills_get
            self._methods['resources/directory/read'] = \
                self._resources_directory_read
        if self.prompts is not None:
            self._methods['prompts/list'] = self._prompts_list
            self._methods['prompts/get'] = self._prompts_get

    def register_tool(self, name, description, input_schema, handler,
                      output_schema=None, needs_context=False,
                      annotations=None, meta=None, self_confirms=False,
                      task_eligible=False):
        """Register a tool exposed via tools/list and tools/call."""
        self.tools[name] = Tool(name, description, input_schema, handler,
                                output_schema, needs_context, annotations,
                                meta, self_confirms, task_eligible)

    def cancel_all(self):
        """Cancel every in-flight request (transport teardown)."""
        with self._active_lock:
            events = list(self._active_requests.values())
        for event in events:
            event.set()

    def drain(self, timeout=60):
        """Wait for the in-flight requests to finish.

        A shutdown is a lifecycle event, not an abort: the work in
        flight is given the chance to finish. Subscription streams do
        not hold the shutdown back — they are long-lived by design and
        are cancelled afterwards like any other leftover. Returns
        whether the server went idle within the timeout.
        """
        deadline = time.monotonic() + timeout
        while True:
            with self._active_lock:
                active = set(self._active_requests)
            with self._subscriptions_lock:
                active -= set(self._subscriptions)
            if not active:
                return True
            if time.monotonic() >= deadline:
                logger.warning('The in-flight requests did not finish '
                               'within %s seconds', timeout)
                return False
            time.sleep(0.1)

    def switch_view(self, view):
        """Switch the active infrastructure view.

        The rebuild closure (set by 'debops mcp serve') validates the
        view by constructing the project with it, rebuilds the policy
        and swaps the tool surface in whole. Called by the view.switch
        tool with the exclusive project lock held.
        """
        if self._rebuild is None:
            raise RuntimeError('the server cannot switch views')
        self._rebuild(view=view)

    def reload(self):
        """Re-read the project configuration and rebuild the policy and
        the tool surface; announce a change to the subscribers.

        Called with a project lock held (the caller is inside one:
        a pull holds the exclusive lock, the configuration watcher
        holds the shared one). In-flight requests keep the tool
        objects they started with; the registry and the sync throttle
        survive. A rebuild failure (a broken configuration file) is
        logged and the current policy stays in effect. Returns whether
        the reload happened.
        """
        if self._rebuild is None:
            return False
        before = [tool.describe() for tool in self.tools.values()]
        try:
            self._rebuild()
        except Exception:
            logger.warning('The policy reload failed; keeping the '
                           'current configuration', exc_info=True)
            return False
        after = [tool.describe() for tool in self.tools.values()]
        if before != after:
            logger.notice('The tool surface changed on reload')
            self.emit_list_changed('tools')
        else:
            logger.debug('Reloaded the policy; the tool surface is '
                         'unchanged')
        return True

    def watch_config(self, interval=CONFIG_WATCH_INTERVAL):
        """Start watching the project configuration for changes.

        A changed configuration reloads the policy and the tool
        surface in place, announced to the subscribers when the
        surface changed. Does nothing when the server has no reload
        recipe or is watched already.
        """
        if self._rebuild is None or self._config_watch is not None:
            return
        stop = threading.Event()

        def watch():
            signature = _config_signature(self.project)
            while not stop.wait(interval):
                current = _config_signature(self.project)
                if current == signature:
                    continue
                # The signature advances even when the reload fails
                # (a file being edited can be invalid for a while);
                # the next change is another chance
                signature = current
                # Imported here: the tools package imports this module
                from .tools.common import shared_project
                try:
                    with shared_project(self.project, self.policy):
                        self.reload()
                except Exception:
                    logger.warning('The configuration reload failed',
                                   exc_info=True)

        thread = threading.Thread(
                target=watch, name='debops-mcp-config-watch',
                daemon=True)
        self._config_watch = stop
        self._config_watch_thread = thread
        thread.start()

    def stop_config_watch(self):
        """Stop the configuration watcher."""
        if self._config_watch is not None:
            self._config_watch.set()
            self._config_watch = None
            # Do not stall a shutdown on a watcher which is blocked on
            # the project lock
            self._config_watch_thread.join(timeout=5.0)
            self._config_watch_thread = None

    def accepting(self):
        """Whether the server currently accepts work (kill switch)."""
        if self.killswitch is None:
            return True
        return self.killswitch.is_enabled()

    @staticmethod
    def _path_monikers(project, transport):
        """The (prefix, moniker) table for the client-facing output.

        stdio needs none — the server runs where the harness is. A
        socket server masks the run-log cache and the bundled content,
        plus the collection roots which sit outside both the project
        and the account's home (the home masking covers those). The
        import is deferred: the tools package imports this module.
        """
        if transport != 'unix':
            return []
        pairs = [(runs.cache_root(), 'run-logs:'),
                 (DEBOPS_PACKAGE_DATA, 'bundled:')]
        from .tools.collections import (ACCOUNT_COLLECTION_ROOTS,
                                        documentation_collection_roots)
        if project is None:
            roots = documentation_collection_roots()
        else:
            roots = []
            defined = os.environ.get('ANSIBLE_COLLECTIONS_PATH')
            if defined:
                roots.extend(entry for entry in
                             defined.split(os.pathsep) if entry)
            roots.extend(os.path.expanduser(root)
                         for root in ACCOUNT_COLLECTION_ROOTS)
        home = os.path.normpath(DEBOPS_USER_HOME_DIR)
        project_path = getattr(project, 'path', None)
        project_root = (os.path.realpath(project_path)
                        if project_path is not None else None)
        for root in roots:
            if not root:
                continue
            normed = os.path.normpath(root)
            if normed == home or normed.startswith(home + os.sep):
                continue
            if project_root is not None and (
                    normed == project_root
                    or normed.startswith(project_root + os.sep)):
                continue
            pairs.append((root, 'collections:'))
        return pairs

    # -- transport entry point ---------------------------------------------

    def _base_meta(self):
        """The '_meta' carried by every result.

        Besides the clacks overhead it identifies the server, the
        project and the infrastructure view the server is confined to,
        so that clients can tell multiple instances apart.
        """
        meta = dict(CLACKS_OVERHEAD)
        meta[SERVER_INFO_KEY] = {'name': self.name, 'version': self.version}
        if self.project is not None:
            meta[PROJECT_META_KEY] = self.project.name
            meta[VIEW_META_KEY] = self.project.view
        return meta

    def handle_bytes(self, data, notify=None, peer_pid=None, dead=None):
        """Handle one raw message; return the response or None."""
        try:
            message = rpc.parse_message(data)
        except rpc.JsonRpcError as errmsg:
            logger.warning('Rejecting an unparseable message: %s',
                           errmsg.message)
            return errmsg.to_response()
        return self.handle(message, notify, peer_pid=peer_pid, dead=dead)

    def handle(self, message, notify=None, peer_pid=None, dead=None):
        """Handle one parsed message; return the response or None."""
        try:
            return self._dispatch(message, notify, peer_pid=peer_pid,
                                  dead=dead)
        except rpc.JsonRpcError as errmsg:
            # Error responses have no '_meta' slot; the clacks overhead
            # and the server identity ride results only
            return errmsg.to_response()
        except Exception as errmsg:
            logger.exception('Internal error while handling a request')
            # A single-user local server serves the same person who
            # reads the journal; naming the exception makes a tool
            # failure actionable for both
            return rpc.make_error(message.get('id'),
                                  rpc.INTERNAL_ERROR,
                                  'Internal error: {}: {}'.format(
                                      type(errmsg).__name__, errmsg))

    def _dispatch(self, message, notify=None, peer_pid=None, dead=None):
        """Route one parsed message to its method handler.

        Raises JsonRpcError for protocol-level errors; the caller turns
        them into error responses.
        """
        request_id = message.get('id')
        # Notifications never get a response
        is_notification = 'id' not in message
        method = message.get('method')
        if not isinstance(method, str):
            raise rpc.JsonRpcError(rpc.INVALID_REQUEST, 'Invalid Request',
                                   request_id=request_id)

        if method == 'initialize' and not is_notification \
                and not self.legacy:
            # A legacy client has no fall-forward mechanism; name the
            # supported versions, since this error may be the only
            # diagnostic it can surface
            raise rpc.JsonRpcError(
                    rpc.METHOD_NOT_FOUND,
                    'Unsupported protocol version: this server speaks '
                    + PROTOCOL_VERSION
                    + ' (start it with --legacy for the 2025-era '
                      'protocol)',
                    data={'supported': [PROTOCOL_VERSION]},
                    request_id=request_id)

        # The request metadata rides in 'params._meta', per the spec;
        # a legacy client carries it only in the handshake
        params = message.get('params')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   request_id=request_id)

        # Notifications carry no '_meta' requirement; requests do —
        # except in the legacy mode, where the handshake carried them
        if not is_notification and not self.legacy:
            self._check_request_meta(params, request_id)

        meta = params.get('_meta')
        progress_token = (meta.get('progressToken')
                          if isinstance(meta, dict) else None)
        capabilities = (meta.get(CLIENT_CAPABILITIES_KEY)
                        if isinstance(meta, dict) else None)
        client_info = (meta.get(CLIENT_INFO_KEY)
                       if isinstance(meta, dict) else None)
        if self.legacy and peer_pid is not None:
            legacy_client = self._legacy_clients.get(peer_pid, {})
            if capabilities is None:
                capabilities = legacy_client.get('capabilities')
            if client_info is None:
                client_info = legacy_client.get('clientInfo')
        context = RequestContext(request_id, notify=notify,
                                 progress_token=progress_token,
                                 client_capabilities=capabilities,
                                 client_info=client_info,
                                 peer_pid=peer_pid, dead=dead)
        self._resolve_trust(context, meta)

        if is_notification:
            logger.info('Notification: %s', method)
        else:
            logger.info('Request: %s (id=%s, client=%s %s)', method,
                        request_id, (client_info or {}).get('name', '?'),
                        (client_info or {}).get('version', ''))

        handler = self._methods.get(method)
        if handler is None:
            if is_notification:
                logger.debug('Ignoring unknown notification: %s', method)
                return None
            logger.warning('Unknown method requested: %s', method)
            raise rpc.JsonRpcError(rpc.METHOD_NOT_FOUND, 'Method not found',
                                   data={'method': method},
                                   request_id=request_id)

        try:
            result = handler(params, context)
        except RequestCancelled:
            # A cancelled request gets no response
            logger.debug('Request %s was cancelled', request_id)
            return None
        except rpc.JsonRpcError as errmsg:
            # Handler errors must echo the request id
            if errmsg.request_id is None:
                errmsg.request_id = request_id
            raise
        if is_notification:
            return None
        if result is None:
            result = {}
        if method in ('tools/call', 'tasks/get'):
            # Client-facing output masks the server-local paths; the
            # audit log and the files keep the real ones. A trusted
            # client gets the human form: the home masking stays, the
            # monikers go
            presenter = (self._present_trusted if context.trusted
                         else self._present)
            result = presenter(result)
        if self.legacy:
            # The 2025-era result shape: no resultType, no caching
            # metadata
            result.pop('resultType', None)
            result.pop('ttlMs', None)
            result.pop('cacheScope', None)
        else:
            result.setdefault('resultType', 'complete')
            # The handler's own '_meta' keys (a subscription id, ...)
            # ride alongside the server's identity
            meta = self._base_meta()
            meta.update(result.get('_meta', {}))
            result['_meta'] = meta
        return rpc.make_result(request_id, result)

    # -- protocol methods ---------------------------------------------------

    def _legacy_initialize(self, params, context):
        """Answer the 2025-era handshake.

        The era is pinned by the operator ('serve --legacy'), so there
        is no negotiation state: the answer is the same for every
        client. The client's capabilities and identity are remembered
        per calling process, since legacy requests carry no '_meta'
        envelope.
        """
        requested = params.get('protocolVersion')
        version = (requested if requested in LEGACY_PROTOCOL_VERSIONS
                   else LEGACY_PROTOCOL_VERSIONS[0])
        if context.peer_pid is not None:
            self._legacy_clients[context.peer_pid] = {
                    'capabilities': params.get('capabilities') or {},
                    'clientInfo': params.get('clientInfo') or {}}
        client = params.get('clientInfo') or {}
        logger.notice('Legacy client initialized: %s %s (pid %s, '
                      'protocol %s)', client.get('name', '?'),
                      client.get('version', ''), context.peer_pid,
                      version)
        return {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': True}},
            'serverInfo': {'name': self.name, 'version': self.version},
            'instructions': self._instructions(),
        }

    def _legacy_ping(self, params, context):
        """The keepalive of the 2025-era protocol."""
        return {}

    def _legacy_initialized(self, params, context):
        """The client finished the handshake; nothing to do."""
        return None

    # -- the trust model: passphrase, bearer tokens, elevation -------

    def _auth_rate_limited(self, peer):
        """The seconds a peer must wait before authenticating again."""
        with self._bearers_lock:
            count, last = self._auth_failures.get(peer, (0, 0.0))
            if not count:
                return 0.0
            wait = min(2.0 ** count, 60.0) - (time.monotonic() - last)
            return max(0.0, wait)

    def _auth_failed(self, peer):
        with self._bearers_lock:
            count, _last = self._auth_failures.get(peer, (0, 0.0))
            self._auth_failures[peer] = (count + 1, time.monotonic())

    def _resolve_trust(self, context, meta):
        """Set the trust flags of a request context from the bearer
        token it carried; the lifetimes slide with use."""
        token = meta.get(TRUST_TOKEN_KEY) if isinstance(meta, dict) \
            else None
        if not token:
            return
        now = time.monotonic()
        with self._bearers_lock:
            bearer = self._bearers.get(str(token))
            if bearer is None or now >= bearer['expires']:
                return
            context.trusted = True
            bearer['expires'] = now + self.policy.trust_ttl
            if now < bearer['elevated_until']:
                context.elevated = True
                bearer['elevated_until'] = now + self.policy.elevate_ttl

    def _server_authenticate(self, params, context):
        """Exchange the project's trust passphrase for a bearer token.

        The token rides the '_meta' of the requests which follow. A
        legacy client carries no '_meta', so trust stays unavailable
        to it.
        """
        if self.project is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Invalid Request',
                    data={'details': 'trust is bound to a project; the '
                          'documentation mode has none'},
                    request_id=context.request_id)
        if self.legacy:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Invalid Request',
                    data={'details': 'trust needs the per-request '
                          "'_meta' envelope, which the 2025-era "
                          'protocol does not carry; the client must '
                          'speak the {} revision'.format(
                                  PROTOCOL_VERSION)},
                    request_id=context.request_id)
        peer = context.peer_pid or 0
        wait = self._auth_rate_limited(peer)
        if wait > 0:
            logger.warning('Authentication rate-limited for pid %s '
                           '(%.0f s)', context.peer_pid, wait)
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Rate limited',
                    data={'details': 'too many failed attempts; retry '
                          'in {:.0f} seconds'.format(wait)},
                    request_id=context.request_id)
        presented = params.get('passphrase')
        stored = trust.read_passphrase(self.project)
        if stored is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Invalid Request',
                    data={'details': 'this daemon has no trust '
                          'passphrase; a restart generates one'},
                    request_id=context.request_id)
        if not isinstance(presented, str) \
                or not trust.passphrase_matches(presented, stored):
            self._auth_failed(peer)
            logger.notice('Authentication failed for pid %s',
                          context.peer_pid)
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'the passphrase did not match'},
                    request_id=context.request_id)
        now = time.monotonic()
        with self._bearers_lock:
            self._auth_failures.pop(peer, None)
            token = secrets.token_urlsafe(24)
            self._bearers[token] = {'expires': now
                                    + self.policy.trust_ttl,
                                    'elevated_until': 0.0}
        logger.notice('A client authenticated%s',
                      _audit_tag(context))
        return {'token': token, 'tokenType': 'bearer',
                'expiresIn': self.policy.trust_ttl}

    def _server_elevate(self, params, context):
        """Raise a trusted session to elevated: the passphrase is
        re-checked, sudo-style; the flag outlives nothing and slides
        with use."""
        meta = params.get('_meta')
        token = meta.get(TRUST_TOKEN_KEY) if isinstance(meta, dict) \
            else None
        if not context.trusted or token is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Invalid Request',
                    data={'details': 'elevation needs an authenticated '
                          'session first (server/authenticate)'},
                    request_id=context.request_id)
        peer = context.peer_pid or 0
        wait = self._auth_rate_limited(peer)
        if wait > 0:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Rate limited',
                    data={'details': 'too many failed attempts; retry '
                          'in {:.0f} seconds'.format(wait)},
                    request_id=context.request_id)
        presented = params.get('passphrase')
        stored = trust.read_passphrase(self.project)
        if not isinstance(presented, str) or stored is None \
                or not trust.passphrase_matches(presented, stored):
            self._auth_failed(peer)
            logger.notice('Elevation failed for pid %s',
                          context.peer_pid)
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'the passphrase did not match'},
                    request_id=context.request_id)
        now = time.monotonic()
        with self._bearers_lock:
            self._auth_failures.pop(peer, None)
            bearer = self._bearers.get(str(token))
            if bearer is not None:
                bearer['elevated_until'] = (now
                                            + self.policy.elevate_ttl)
                context.elevated = True
        logger.notice('A trusted client elevated%s',
                      _audit_tag(context))
        return {'elevated': True, 'expiresIn': self.policy.elevate_ttl}

    def _server_drop(self, params, context):
        """Drop the elevation of the session's bearer, ahead of its
        TTL."""
        meta = params.get('_meta')
        token = meta.get(TRUST_TOKEN_KEY) if isinstance(meta, dict) \
            else None
        with self._bearers_lock:
            bearer = self._bearers.get(str(token))
            if bearer is not None:
                bearer['elevated_until'] = 0.0
        if context.elevated:
            context.elevated = False
            logger.notice('A trusted client dropped the elevation%s',
                          _audit_tag(context))
        return {'elevated': False}

    def _check_request_meta(self, params, request_id):
        """Enforce the per-request protocol fields.

        Every request must carry its protocol version and client
        capabilities in 'params._meta'; a missing one is malformed. A
        request asking for another protocol revision is rejected.
        """
        meta = params.get('_meta')
        if not isinstance(meta, dict):
            logger.warning('Request without the required _meta')
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "missing required '_meta' fields "
                          "(they belong in 'params._meta'): "
                          '{} and {}'.format(PROTOCOL_VERSION_KEY,
                                             CLIENT_CAPABILITIES_KEY)},
                    request_id=request_id)
        missing = [key for key in (PROTOCOL_VERSION_KEY,
                                   CLIENT_CAPABILITIES_KEY)
                   if meta.get(key) is None]
        if missing:
            logger.warning('Request without required _meta fields: %s',
                           ', '.join(missing))
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'missing required _meta fields: '
                          + ', '.join(missing)},
                    request_id=request_id)
        version = meta[PROTOCOL_VERSION_KEY]
        if version != PROTOCOL_VERSION:
            logger.warning('Unsupported protocol version requested: %s',
                           version)
            raise rpc.JsonRpcError(
                    rpc.UNSUPPORTED_PROTOCOL_VERSION,
                    'Unsupported protocol version',
                    data={'supported': [PROTOCOL_VERSION],
                          'requested': version},
                    request_id=request_id)

    def _instructions(self):
        """The guidance of the discover result, per the server mode
        and transport.

        The harness decides whether the model ever sees this (the spec
        suggests folding it into the system prompt); either way the
        text must be true — a documentation server is not a project,
        and a daemon's project is not necessarily readable from the
        client's side.
        """
        batching = ('Calls in one batch run concurrently, without '
                    'ordering; when a call depends on the effect of an '
                    'earlier one (write, then list or check), wait for '
                    'its response before sending the next.')
        if self.project is None:
            return (
                'This server answers with the bundled DebOps '
                'documentation, read-only; no project directory is '
                'served. Explore roles with the role.* tools, playbook '
                'targets with playbook.list and playbook.info, and the '
                'visible collections with collection.roles and '
                'collection.playbooks; ANSIBLE_COLLECTIONS_PATH decides '
                'which external collections are visible, and without '
                'it the account\'s own collections are served next to '
                'the bundled ones. Reusable '
                'DebOps workflows are available as skill:// resources. '
                + batching)
        if self.transport == 'unix':
            return (
                'This server is a DebOps MCP daemon for the project '
                "'{}' (view '{}'), reached over a socket: the project "
                'directory is not necessarily readable from here, so '
                'the debops tools are the access path — browse with '
                'project.files and project.read, inspect roles and '
                'playbooks with the role.* and playbook.* tools, run '
                "'check' before 'run'. Writes are "
                'confined and committed to git automatically; secrets '
                'and key material are never served. Server-local paths '
                "in the output are masked: 'run-logs:/' is the "
                "server's run-log cache, 'bundled:/' its bundled "
                "content, 'collections:/' a collections root — read "
                'them through the tools. Reusable DebOps '
                'workflows are available as skill:// resources. '
                .format(self.project.name, self.project.view)
                + batching)
        return (
            'This server exposes the DebOps project '
            "'{}' (view '{}', which a view.switch call may change "
            'mid-session; the _meta stamp of every result carries the '
            'current one). Prefer the debops tools over reading the '
            'project files directly: they enforce the confinement '
            'policy (secrets and key material are never served) and '
            'commit writes to git automatically. Browse with '
            'project.files and project.read, inspect roles and '
            'playbooks with the role.* and playbook.* tools, run '
            "'check' before 'run'. Reusable DebOps "
            'workflows are available as skill:// resources. '
            .format(self.project.name, self.project.view)
            + batching)

    def _discover(self, params, context):
        capabilities = {'tools': {}}
        extensions = {TASKS_EXTENSION: {}}
        if self.skills is not None:
            capabilities['resources'] = {'listChanged': True}
            # The skills extension (SEP-2640): skills/list, skills/get
            # and the directory listing
            extensions[SKILLS_EXTENSION] = {'directoryRead': True}
        # The prompts are project content, so the capability follows the
        # directory at call time
        if (self.prompts is not None and self.prompts.available()
                and self.prompts.list()):
            capabilities['prompts'] = {'listChanged': True}
        return {
            'supportedVersions': [PROTOCOL_VERSION],
            'capabilities': dict(capabilities, extensions=extensions),
            'instructions': self._instructions(),
            'ttlMs': 60000, 'cacheScope': 'private',
        }

    def _tools_list(self, params, context):
        _reject_cursor(params)
        # The tool set is fixed at startup; a short TTL lets clients
        # pick up a restarted daemon's changed policy reasonably fast
        return {'tools': [tool.describe() for tool in self.tools.values()],
                'ttlMs': 60000, 'cacheScope': 'private'}

    def _tools_call(self, params, context):
        name = params.get('name')
        tool = self.tools.get(name)
        if tool is None and name in self.aliases:
            # A hidden alias resolves to its canonical tool, quietly;
            # the alias never appears in the tools list
            canonical = self.aliases[name]
            tool = self.tools.get(canonical)
            if tool is not None:
                logger.notice('Tool alias resolved: %s -> %s',
                              name, canonical)
        if tool is None:
            logger.warning('Unknown tool requested: %s', name)
            details = 'unknown tool: {}'.format(name)
            # A near-miss name (a renamed tool, a harness-normalized
            # form) gets the pointer instead of a retry loop; harnesses
            # which keep the error from the model usually have their
            # own suggestion, so this costs them nothing
            closest = difflib.get_close_matches(
                    name, sorted(self.tools), n=3, cutoff=0.6)
            if not closest and '_' in name:
                closest = difflib.get_close_matches(
                        name.replace('_', '.'), sorted(self.tools),
                        n=3, cutoff=0.6)
            if closest:
                details += ('; did you mean {}?'
                            .format(', '.join(repr(item)
                                              for item in closest)))
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': details})
        arguments = params.get('arguments')
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'arguments' must be an object"})
        # Log the argument keys only; values may contain secrets
        logger.debug('Calling tool %s with arguments: %s',
                     name, sorted(arguments))
        gated = self._confirmation_gate(tool, arguments, params, context)
        if gated is not None:
            logger.notice('Tool %s(%s) -> gated%s', name,
                          ','.join(sorted(arguments)),
                          _audit_tag(context))
            return gated
        if (tool.task_eligible
                and self._client_supports(context, TASKS_EXTENSION)):
            logger.notice('Tool %s(%s) -> task%s', name,
                          ','.join(sorted(arguments)),
                          _audit_tag(context))
            return self._start_task(tool, arguments, params, context)
        if not tool.needs_context:
            result = tool.handler(arguments)
            logger.notice('Tool %s(%s) -> %s%s', name,
                          ','.join(sorted(arguments)),
                          'error' if result.get('isError') else 'ok',
                          _audit_tag(context))
            return result

        request_id = context.request_id
        cancel_event = threading.Event()
        if request_id is not None:
            with self._active_lock:
                self._active_requests[request_id] = cancel_event
        try:
            tool_context = ToolContext(
                    request_id, cancel_event,
                    progress_token=context.progress_token,
                    notify=context.notify,
                    peer_pid=context.peer_pid,
                    client_capabilities=context.client_capabilities,
                    client_info=context.client_info,
                    input_responses=params.get('inputResponses'),
                    trusted=context.trusted, elevated=context.elevated)
            result = tool.handler(arguments, tool_context)
            logger.notice('Tool %s(%s) -> %s%s', name,
                          ','.join(sorted(arguments)),
                          'error' if result.get('isError') else 'ok',
                          _audit_tag(context))
            return result
        finally:
            if request_id is not None:
                with self._active_lock:
                    self._active_requests.pop(request_id, None)

    def _confirmation_gate(self, tool, arguments, params, context):
        """Gate a tool on explicit user confirmation, via elicitation.

        Returns None when the call may proceed, otherwise the result to
        send back: an elicitation request ('input_required') when the
        user still has to answer, or the refusal as a tool error. The
        confirmation is a courtesy for the user, not a security
        boundary — the client answers it; the 'project.mcp.execute'
        policy remains the authoritative control.
        """
        if (self.policy is None
                or tool.name not in self.policy.confirm
                or tool.self_confirms):
            return None

        if context.elevated:
            # The elevation re-proved the human; the gates answer
            # themselves for it. The audit line says so
            logger.notice('Tool %s runs without the confirmation '
                          'gate: the client is elevated%s',
                          tool.name, _audit_tag(context))
            return None

        # The retry of a confirmed call carries the user's answer
        responses = params.get('inputResponses')
        if isinstance(responses, dict) and 'confirmation' in responses:
            answer = responses.get('confirmation') or {}
            content = answer.get('content') or {}
            if (answer.get('action') == 'accept'
                    and content.get('confirmed') is True):
                logger.notice('Tool %s confirmed by the user', tool.name)
                return None
            logger.notice('Tool %s declined by the user', tool.name)
            return {'resultType': 'complete',
                    'content': [{'type': 'text',
                                 'text': 'The operation was declined '
                                         'by the user.'}],
                    'isError': True}

        if not _supports_form_elicitation(context.client_capabilities) \
                or self.legacy:
            # A legacy client's elicitation is the in-band kind
            # (a server-initiated request mid-call), which this server
            # does not drive; it is treated as unable to ask
            if self.policy.confirm_required:
                logger.notice('Tool %s refused: the client cannot '
                              'elicit and the policy requires '
                              'confirmation', tool.name)
                return {'resultType': 'complete',
                        'content': [{'type': 'text',
                                     'text': 'The operation requires '
                                             'user confirmation, which '
                                             'this client cannot '
                                             'provide (no form '
                                             'elicitation); refused by '
                                             'the project policy '
                                             '(project.mcp.'
                                             'confirm_required).'}],
                        'isError': True}
            # A client which cannot ask may not block the work; the
            # skip is written to the audit log
            logger.notice('Tool %s runs without user confirmation: '
                          'the client cannot elicit', tool.name)
            return None

        return {'resultType': 'input_required',
                'inputRequests': {
                    'confirmation': {
                        'method': 'elicitation/create',
                        'params': {
                            'mode': 'form',
                            'message': _confirmation_message(tool.name,
                                                             arguments),
                            'requestedSchema': {
                                'type': 'object',
                                'properties': {
                                    'confirmed': {
                                        'type': 'boolean',
                                        'title': 'Confirm',
                                        'description':
                                            'I understand and want to '
                                            'proceed',
                                        'default': False,
                                    },
                                },
                                'required': ['confirmed'],
                            },
                        },
                    },
                }}

    def _prompts_list(self, params, context):
        _reject_cursor(params)
        return {'prompts': self.prompts.list(),
                'ttlMs': 60000, 'cacheScope': 'private'}

    def _prompts_get(self, params, context):
        name = params.get('name')
        if not isinstance(name, str) or not name:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "missing prompt 'name'"})
        arguments = params.get('arguments', {})
        if not isinstance(arguments, dict):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'arguments' must be an object"})
        try:
            rendered = self.prompts.render(name, arguments)
        except ValueError as errmsg:
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   data={'details': str(errmsg)})
        return {'description': rendered['description'],
                'messages': [{'role': 'user',
                              'content': {'type': 'text',
                                          'text': rendered['text']}}]}

    def _resources_list(self, params, context):
        _reject_cursor(params)
        # The skills are static package content; they change only with
        # a package upgrade
        return {'resources': self.skills.list(),
                'ttlMs': 3600000, 'cacheScope': 'public'}

    def _paginate(self, items, key, params):
        """One page of a catalog result, per the pagination utility.

        The catalogs (the skills, the directory listings) outgrow a
        page with the generated corpora; the cursor is an offset over
        the sorted order. A cursor we never issued is invalid.
        """
        offset = 0
        if params.get('cursor') is not None:
            try:
                offset = _decode_cursor(params['cursor'])
            except ValueError:
                raise rpc.JsonRpcError(
                        rpc.INVALID_PARAMS, 'Invalid params',
                        data={'details': 'invalid cursor'})
            if offset >= max(len(items), 1):
                raise rpc.JsonRpcError(
                        rpc.INVALID_PARAMS, 'Invalid params',
                        data={'details': 'invalid cursor'})
        page = items[offset:offset + LIST_PAGE_SIZE]
        result = {key: page, 'ttlMs': 60000, 'cacheScope': 'private'}
        if offset + LIST_PAGE_SIZE < len(items):
            result['nextCursor'] = _encode_cursor(
                    offset + LIST_PAGE_SIZE)
        return result

    def _skills_list(self, params, context):
        return self._paginate(self.skills.entries(), 'skills', params)

    def _skills_get(self, params, context):
        uri = params.get('uri')
        if not isinstance(uri, str):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'uri' must be a string"})
        try:
            entry = self.skills.entry_for(uri)
        except ValueError as errmsg:
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   data={'details': str(errmsg)})
        if entry is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unknown skill: {}'.format(uri)})
        return {'skill': entry, 'ttlMs': 60000, 'cacheScope': 'private'}

    def _resources_directory_read(self, params, context):
        uri = params.get('uri')
        if not isinstance(uri, str):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'uri' must be a string"})
        try:
            children = self.skills.directory(uri)
        except ValueError as errmsg:
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   data={'details': str(errmsg)})
        if children is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'not a directory resource: {}'
                          .format(uri)})
        return self._paginate(children, 'resources', params)

    def _resources_read(self, params, context):
        uri = params.get('uri')
        if not isinstance(uri, str) or not uri.startswith(SKILL_URI_PREFIX):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unsupported URI scheme; '
                          'expected {}...'.format(SKILL_URI_PREFIX)})
        try:
            content = self.skills.read(uri)
        except ValueError as errmsg:
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   data={'details': str(errmsg)})
        if content is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'resource not found: {}'.format(uri)})
        return {'contents': [{'uri': uri, 'mimeType': 'text/markdown',
                              'text': content}],
                'ttlMs': 3600000, 'cacheScope': 'public'}

    # -- tasks (the io.modelcontextprotocol/tasks extension) ----------------

    def _client_supports(self, context, extension):
        """Whether the client declared an extension on this request."""
        capabilities = context.client_capabilities
        return extension in (capabilities.get('extensions') or {})

    def _require_tasks_capability(self, context):
        if not self._client_supports(context, TASKS_EXTENSION):
            raise rpc.JsonRpcError(
                    -32021, 'Missing required client capability',
                    data={'requiredCapabilities':
                          {'extensions': {TASKS_EXTENSION: {}}}},
                    request_id=context.request_id)

    def _start_task(self, tool, arguments, params, context):
        """Run a task-eligible tool in the background; return the handle."""
        timeout = (self.policy.execute_timeout
                   if self.policy is not None else 3600)
        # An unlimited execution timeout (0) still needs a task bound
        task = self._tasks.create(
                ttl_ms=max(3600000,
                           (timeout if timeout > 0 else 43200) * 2000))
        thread = threading.Thread(
                target=self._run_task,
                args=(task, tool, arguments, params, context),
                daemon=True)
        thread.start()
        logger.notice('Task %s started: %s', task.task_id, tool.name)
        return task.creation_result()

    def _run_task(self, task, tool, arguments, params, context):
        """Drive one task to its terminal state."""
        # The task is cancelled by tasks/cancel via its event, and by
        # the server-wide cancel (the kill switch, a transport shutdown)
        with self._active_lock:
            self._active_requests['task:' + task.task_id] = \
                task.cancel_event
        try:
            tool_context = ToolContext(
                    task.task_id, task.cancel_event,
                    progress_token=None, notify=context.notify,
                    peer_pid=context.peer_pid,
                    client_capabilities=context.client_capabilities,
                    client_info=context.client_info,
                    input_responses=params.get('inputResponses'),
                    # The trust of the requesting moment rides with
                    # the task
                    trusted=context.trusted, elevated=context.elevated,
                    task=task)
            if tool.needs_context:
                result = tool.handler(arguments, tool_context)
            else:
                result = tool.handler(arguments)
            task.complete(result)
            logger.notice('Task %s completed: %s', task.task_id,
                          tool.name)
        except RequestCancelled:
            task.mark_cancelled()
            logger.notice('Task %s cancelled: %s', task.task_id,
                          tool.name)
        except Exception as errmsg:
            logger.exception('Task %s failed: %s', task.task_id,
                             tool.name)
            task.fail({'code': rpc.INTERNAL_ERROR,
                       'message': 'Internal error: {}: {}'.format(
                               type(errmsg).__name__, errmsg)})
        finally:
            with self._active_lock:
                self._active_requests.pop('task:' + task.task_id, None)

    def _tasks_get(self, params, context):
        self._require_tasks_capability(context)
        task = self._tasks.get(params.get('taskId'))
        if task is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unknown or expired task: {}'
                          .format(params.get('taskId'))},
                    request_id=context.request_id)
        return task.describe()

    def _tasks_update(self, params, context):
        self._require_tasks_capability(context)
        task = self._tasks.get(params.get('taskId'))
        if task is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unknown or expired task: {}'
                          .format(params.get('taskId'))},
                    request_id=context.request_id)
        responses = params.get('inputResponses', {})
        if not isinstance(responses, dict):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'inputResponses' must be an "
                          'object'},
                    request_id=context.request_id)
        for key, response in responses.items():
            task.answer(key, response)
        return {}

    def _tasks_cancel(self, params, context):
        self._require_tasks_capability(context)
        if not self._tasks.cancel(params.get('taskId')):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unknown or expired task: {}'
                          .format(params.get('taskId'))},
                    request_id=context.request_id)
        return {}

    # -- subscriptions -----------------------------------------------------

    def _subscribe(self, request_id, honored):
        event_queue = queue.Queue()
        with self._subscriptions_lock:
            self._subscriptions[request_id] = {
                    'filter': honored, 'queue': event_queue}
            if honored.get('activity') and self._activity_handler \
                    is None:
                # The audit feed attaches while someone listens
                self._activity_handler = _ActivityHandler(self)
                logging.getLogger('debops').addHandler(
                        self._activity_handler)
        return event_queue

    def _unsubscribe(self, request_id):
        with self._subscriptions_lock:
            self._subscriptions.pop(request_id, None)
            if self._activity_handler is not None and not any(
                    subscription['filter'].get('activity')
                    for subscription in self._subscriptions.values()):
                logging.getLogger('debops').removeHandler(
                        self._activity_handler)
                self._activity_handler = None

    def _emit_activity(self, line):
        """Push an audit line to the activity subscribers."""
        message = {'jsonrpc': '2.0',
                   'method': 'notifications/activity',
                   'params': {'line': line}}
        with self._subscriptions_lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            if subscription['filter'].get('activity'):
                subscription['queue'].put(message)

    def emit_list_changed(self, kind):
        """Notify subscribers that a list ('resources', 'prompts') changed."""
        key = kind + 'ListChanged'
        message = {'jsonrpc': '2.0',
                   'method': 'notifications/{}/list_changed'.format(kind),
                   'params': {}}
        with self._subscriptions_lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            if subscription['filter'].get(key):
                subscription['queue'].put(message)

    def emit_resource_updated(self, uri):
        """Notify subscribers of a resource URI that its content changed."""
        message = {'jsonrpc': '2.0',
                   'method': 'notifications/resources/updated',
                   'params': {'uri': uri}}
        with self._subscriptions_lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            if uri in subscription['filter'].get('resourceSubscriptions',
                                                 []):
                subscription['queue'].put(message)

    def _emit_task_change(self, task):
        """Push a task status notification to its subscribers."""
        message = {'jsonrpc': '2.0', 'method': 'notifications/tasks',
                   'params': task.describe()}
        with self._subscriptions_lock:
            subscriptions = list(self._subscriptions.values())
        for subscription in subscriptions:
            if task.task_id in subscription['filter'].get('taskIds', []):
                subscription['queue'].put(message)

    def content_fingerprint(self):
        """A fingerprint of the project content served (skills, prompts)."""
        return {
            'skills': (self.skills.project_fingerprint()
                       if self.skills is not None else {}),
            'prompts': (self.prompts.project_fingerprint()
                        if self.prompts is not None else {}),
        }

    def announce_content_changes(self, before):
        """Emit the notifications for content changed since 'before'."""
        after = self.content_fingerprint()
        if before['skills'] != after['skills']:
            self.emit_list_changed('resources')
            for path, mtime in after['skills'].items():
                if before['skills'].get(path) != mtime:
                    self.emit_resource_updated(
                            SKILL_URI_PREFIX + path + '/SKILL.md')
        if before['prompts'] != after['prompts']:
            self.emit_list_changed('prompts')

    def _subscriptions_listen(self, params, context):
        """Open a notification stream until cancelled or disconnected.

        The acknowledgment goes first, per the specification; the
        request stays open and the stream ends with the completion
        result.
        """
        request_id = context.request_id
        notify = context.notify
        if request_id is None or notify is None:
            raise rpc.JsonRpcError(
                    rpc.INVALID_REQUEST, 'Invalid Request',
                    data={'details': 'a subscription needs a request id '
                          'and a notification channel'},
                    request_id=request_id)
        requested = params.get('notifications', {})
        if not isinstance(requested, dict):
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "'notifications' must be an object"},
                    request_id=request_id)

        # The honored subset: unsupported types are omitted from the
        # acknowledgment, per the specification
        honored = {}
        for key in ('resourcesListChanged', 'promptsListChanged',
                    'toolsListChanged'):
            if requested.get(key):
                honored[key] = True
        uris = [str(uri) for uri in
                requested.get('resourceSubscriptions', []) or []
                if str(uri).startswith(SKILL_URI_PREFIX)]
        if uris:
            honored['resourceSubscriptions'] = uris
        if requested.get('activity'):
            # The audit stream is the operator's view of the other
            # clients; it needs trust
            if not context.trusted:
                raise rpc.JsonRpcError(
                        rpc.INVALID_PARAMS, 'Invalid params',
                        data={'details': 'the activity topic needs a '
                              'trusted client (server/authenticate)'},
                        request_id=request_id)
            honored['activity'] = True
        task_ids = requested.get('taskIds')
        if task_ids:
            # Task notifications require the client to have declared
            # the tasks extension
            if not self._client_supports(context, TASKS_EXTENSION):
                raise rpc.JsonRpcError(
                        -32021, 'Missing required client capability',
                        data={'requiredCapabilities':
                              {'extensions': {TASKS_EXTENSION: {}}}},
                        request_id=request_id)
            honored['taskIds'] = [str(task_id) for task_id in task_ids]

        event_queue = self._subscribe(request_id, honored)
        cancel_event = threading.Event()
        with self._active_lock:
            self._active_requests[request_id] = cancel_event
        notify({'jsonrpc': '2.0',
                'method': 'notifications/subscriptions/acknowledged',
                'params': {'_meta': {SUBSCRIPTION_ID_KEY: request_id},
                           'notifications': honored}})
        logger.info('Subscription %s opened: %s', request_id,
                    ', '.join(sorted(honored)) or '(nothing honored)')
        try:
            while not cancel_event.is_set() and not context.is_dead():
                try:
                    message = event_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                message['params'].setdefault('_meta', {})[
                        SUBSCRIPTION_ID_KEY] = request_id
                notify(message)
        finally:
            self._unsubscribe(request_id)
            with self._active_lock:
                self._active_requests.pop(request_id, None)
        logger.info('Subscription %s closed', request_id)
        # The graceful end: the completion result closes the stream,
        # tagged with the subscription id
        return {'_meta': {SUBSCRIPTION_ID_KEY: request_id}}

    def _cancelled(self, params, context):
        target = params.get('requestId')
        with self._active_lock:
            event = self._active_requests.get(target)
        if event is not None:
            logger.notice('Request %s cancelled by the client', target)
            event.set()
        else:
            logger.debug('Cancellation for an unknown or finished '
                         'request: %s', target)
        return None
