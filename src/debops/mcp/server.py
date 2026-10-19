# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The DebOps MCP server.

Transport-agnostic request dispatch for the 2026-07-28 ("stateless")
revision of the Model Context Protocol. Every request is self-contained;
there is no 'initialize' handshake and no session state, so a transport
can hand any message to handle_bytes() independently of any other.
"""

import logging
import os
import queue
import threading

from . import rpc
from .skills import SKILL_URI_PREFIX
from .tasks import TaskRegistry

logger = logging.getLogger(__name__)

# Detect the DebOps version from the Python module; fall back to a stub in
# a development environment where the version has not been generated
try:
    from ..__version__ import __version__
except ModuleNotFoundError:
    __version__ = '0.0.0'

# The revision of the Model Context Protocol implemented by this server
PROTOCOL_VERSION = '2026-07-28'

PROTOCOL_VERSION_KEY = 'io.modelcontextprotocol/protocolVersion'
CLIENT_CAPABILITIES_KEY = 'io.modelcontextprotocol/clientCapabilities'
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
        return message
    if tool_name == 'project_push':
        return ("Push the project's committed history to its git origin?")
    if tool_name == 'collection_require':
        message = 'Install the Ansible Collection "{}"'.format(
                arguments.get('name', '?'))
        if arguments.get('version'):
            message += ' (version {})'.format(arguments['version'])
        if arguments.get('source'):
            message += ' from {}'.format(arguments['source'])
        return message + ' into the project?'
    if tool_name == 'collection_remove':
        return 'Remove the Ansible Collection "{}" from the ' \
            'project?'.format(arguments.get('name', '?'))
    if tool_name in ('environment_set', 'environment_unset'):
        verb = 'Set' if tool_name == 'environment_set' else 'Unset'
        message = '{} the environment variable "{}"'.format(
                verb, arguments.get('name', '?'))
        if arguments.get('secret'):
            return message + (' in the untracked tokens file (never '
                              'committed to git)?')
        if tool_name == 'environment_set' and arguments.get('value'):
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
    the client's progress token, capabilities and process id from the
    request '_meta' and the transport.
    """

    def __init__(self, request_id, notify=None, progress_token=None,
                 client_capabilities=None, peer_pid=None, dead=None):
        self.request_id = request_id
        self.notify = notify
        self.progress_token = progress_token
        self.client_capabilities = (client_capabilities
                                    if isinstance(client_capabilities, dict)
                                    else {})
        self.peer_pid = peer_pid
        self._dead = dead

    def is_dead(self):
        """Whether the transport channel is gone (EOF, teardown)."""
        return self._dead is not None and self._dead.is_set()


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
    for the audit log.
    """

    def __init__(self, request_id, cancelled, progress_token=None,
                 notify=None, peer_pid=None, client_capabilities=None,
                 input_responses=None, task=None):
        self.request_id = request_id
        self.cancelled = cancelled
        self.progress_token = progress_token
        self.peer_pid = peer_pid
        self.client_capabilities = (client_capabilities
                                    if isinstance(client_capabilities, dict)
                                    else {})
        self.input_responses = (input_responses
                                if isinstance(input_responses, dict)
                                else None)
        self.task = task
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
                 policy=None, skills=None, prompts=None, killswitch=None):
        self.name = name
        self.version = version or __version__
        # The project directory the server is confined to; tools resolve
        # all paths against it
        self.project = project
        self.policy = policy
        # The skill library served as resources; None when absent
        self.skills = skills
        # The prompt library; None when absent
        self.prompts = prompts
        # The kill switch; None means always enabled
        self.killswitch = killswitch
        self.tools = {}
        # Cancellation registry: request id -> cancel event, for the
        # requests currently being handled
        self._active_requests = {}
        self._active_lock = threading.Lock()
        self._methods = {
            'server/discover': self._discover,
            'tools/list': self._tools_list,
            'tools/call': self._tools_call,
            'notifications/cancelled': self._cancelled,
            'subscriptions/listen': self._subscriptions_listen,
            'tasks/get': self._tasks_get,
            'tasks/update': self._tasks_update,
            'tasks/cancel': self._tasks_cancel,
        }
        # Open subscription streams: request id → filter and event queue
        self._subscriptions = {}
        self._subscriptions_lock = threading.Lock()
        # The task registry: long-running work the client polls for
        self._tasks = TaskRegistry()
        self._tasks.on_task_change = self._emit_task_change
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
        except Exception:
            logger.exception('Internal error while handling a request')
            return rpc.make_error(message.get('id'),
                                  rpc.INTERNAL_ERROR, 'Internal error')

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

        # Notifications carry no '_meta' requirement; requests do
        if not is_notification:
            self._check_request_meta(message, request_id)

        meta = message.get('_meta')
        progress_token = (meta.get('progressToken')
                          if isinstance(meta, dict) else None)
        capabilities = (meta.get(CLIENT_CAPABILITIES_KEY)
                        if isinstance(meta, dict) else None)
        context = RequestContext(request_id, notify=notify,
                                 progress_token=progress_token,
                                 client_capabilities=capabilities,
                                 peer_pid=peer_pid, dead=dead)

        if is_notification:
            logger.info('Notification: %s', method)
        else:
            client = meta.get(CLIENT_INFO_KEY, {})
            logger.info('Request: %s (id=%s, client=%s %s)', method,
                        request_id, client.get('name', '?'),
                        client.get('version', ''))

        handler = self._methods.get(method)
        if handler is None:
            if is_notification:
                logger.debug('Ignoring unknown notification: %s', method)
                return None
            logger.warning('Unknown method requested: %s', method)
            raise rpc.JsonRpcError(rpc.METHOD_NOT_FOUND, 'Method not found',
                                   data={'method': method},
                                   request_id=request_id)

        params = message.get('params')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
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
        result.setdefault('resultType', 'complete')
        # The handler's own '_meta' keys (a subscription id, ...) ride
        # alongside the server's identity
        meta = self._base_meta()
        meta.update(result.get('_meta', {}))
        result['_meta'] = meta
        return rpc.make_result(request_id, result)

    # -- protocol methods ---------------------------------------------------

    def _check_request_meta(self, message, request_id):
        """Enforce the per-request protocol fields.

        Every request must carry its protocol version and client
        capabilities in '_meta'; a missing one is malformed. A request
        asking for another protocol revision is rejected.
        """
        meta = message.get('_meta')
        if not isinstance(meta, dict):
            logger.warning('Request without the required _meta')
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': "missing required '_meta' fields: "
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

    def _discover(self, params, context):
        capabilities = {'tools': {}}
        if self.skills is not None:
            capabilities['resources'] = {'listChanged': True}
        # The prompts are project content, so the capability follows the
        # directory at call time
        if (self.prompts is not None and self.prompts.available()
                and self.prompts.list()):
            capabilities['prompts'] = {'listChanged': True}
        return {
            'supportedVersions': [PROTOCOL_VERSION],
            'capabilities': dict(capabilities,
                                 extensions={TASKS_EXTENSION: {}}),
            'instructions':
                'This server exposes one DebOps project directory: '
                'inspect its inventory, roles, playbooks and '
                'configuration, run playbooks within the project '
                'policy, and synchronize the project with its git '
                'origin. Reusable DebOps workflows are available as '
                'skill:// resources. Calls in one batch run '
                'concurrently, without ordering; when a call depends '
                'on the effect of an earlier one (write, then list or '
                'check), wait for its response before sending the '
                'next.',
            'ttlMs': 60000, 'cacheScope': 'private',
        }

    def _tools_list(self, params, context):
        # The tool set is fixed at startup; a short TTL lets clients
        # pick up a restarted daemon's changed policy reasonably fast
        return {'tools': [tool.describe() for tool in self.tools.values()],
                'ttlMs': 60000, 'cacheScope': 'private'}

    def _tools_call(self, params, context):
        name = params.get('name')
        tool = self.tools.get(name)
        if tool is None:
            logger.warning('Unknown tool requested: %s', name)
            raise rpc.JsonRpcError(
                    rpc.INVALID_PARAMS, 'Invalid params',
                    data={'details': 'unknown tool: {}'.format(name)})
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
            return gated
        if (tool.task_eligible
                and self._client_supports(context, TASKS_EXTENSION)):
            return self._start_task(tool, arguments, params, context)
        if not tool.needs_context:
            return tool.handler(arguments)

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
                    input_responses=params.get('inputResponses'))
            return tool.handler(arguments, tool_context)
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

        if not _supports_form_elicitation(context.client_capabilities):
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
        # The skills are static package content; they change only with
        # a package upgrade
        return {'resources': self.skills.list(),
                'ttlMs': 3600000, 'cacheScope': 'public'}

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
        task = self._tasks.create(ttl_ms=max(3600000, timeout * 2000))
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
                    input_responses=params.get('inputResponses'),
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
        except Exception:
            logger.exception('Task %s failed: %s', task.task_id,
                             tool.name)
            task.fail({'code': rpc.INTERNAL_ERROR,
                       'message': 'Internal error'})
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
        return event_queue

    def _unsubscribe(self, request_id):
        with self._subscriptions_lock:
            self._subscriptions.pop(request_id, None)

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
            for name, mtime in after['skills'].items():
                if before['skills'].get(name) != mtime:
                    self.emit_resource_updated(SKILL_URI_PREFIX + name)
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
