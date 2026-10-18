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
import threading

from . import rpc
from .skills import SKILL_URI_PREFIX

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

# A tribute carried in the overhead of every result; '_meta' is
# protocol metadata consumed by clients and never shown to the model,
# and unknown namespaced keys are ignored by design
CLACKS_OVERHEAD = {'org.debops/clacksOverhead': 'GNU Terry Pratchett'}

# The project the server is confined to, so that clients can tell
# multiple instances apart
PROJECT_META_KEY = 'org.debops/project'
VIEW_META_KEY = 'org.debops/view'


class RequestCancelled(Exception):
    """Raised by a tool when its request was cancelled by the client.

    The server sends no response for a cancelled request, per the
    specification.
    """


class RequestContext(object):
    """Context of the request being dispatched.

    Carries the request id and the transport-level notification channel
    (for server-initiated messages such as progress notifications), plus
    the client's progress token when it asked for progress reports.
    """

    def __init__(self, request_id, notify=None, progress_token=None):
        self.request_id = request_id
        self.notify = notify
        self.progress_token = progress_token


class ToolContext(object):
    """Request-scoped context passed to tools which need it.

    'cancelled' is an event which is set when the client cancels the
    request via 'notifications/cancelled'; long-running tools should
    poll it and stop. 'progress()' sends a progress notification when
    the client asked for one with a 'progressToken' in the request
    '_meta'.
    """

    def __init__(self, request_id, cancelled, progress_token=None,
                 notify=None):
        self.request_id = request_id
        self.cancelled = cancelled
        self.progress_token = progress_token
        self._notify = notify

    def progress(self, message, progress=None, total=None):
        """Send a progress notification, if the client asked for them."""
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
                 annotations=None):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.handler = handler
        self.needs_context = needs_context
        self.annotations = annotations

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
        return description


class McpServer(object):
    """Dispatch MCP requests to their handlers."""

    def __init__(self, name='debops', version=None, project=None,
                 policy=None, skills=None, killswitch=None):
        self.name = name
        self.version = version or __version__
        # The project directory the server is confined to; tools resolve
        # all paths against it
        self.project = project
        self.policy = policy
        # The skill library served as resources; None when absent
        self.skills = skills
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
        }
        if self.skills is not None:
            self._methods['resources/list'] = self._resources_list
            self._methods['resources/read'] = self._resources_read

    def register_tool(self, name, description, input_schema, handler,
                      output_schema=None, needs_context=False,
                      annotations=None):
        """Register a tool exposed via tools/list and tools/call."""
        self.tools[name] = Tool(name, description, input_schema, handler,
                                output_schema, needs_context, annotations)

    def cancel_all(self):
        """Cancel every in-flight request (transport teardown)."""
        with self._active_lock:
            events = list(self._active_requests.values())
        for event in events:
            event.set()

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

    def handle_bytes(self, data, notify=None):
        """Handle one raw message; return the response or None."""
        try:
            message = rpc.parse_message(data)
        except rpc.JsonRpcError as errmsg:
            logger.warning('Rejecting an unparseable message: %s',
                           errmsg.message)
            return errmsg.to_response()
        return self.handle(message, notify)

    def handle(self, message, notify=None):
        """Handle one parsed message; return the response or None."""
        try:
            return self._dispatch(message, notify)
        except rpc.JsonRpcError as errmsg:
            # Error responses have no '_meta' slot; the clacks overhead
            # and the server identity ride results only
            return errmsg.to_response()
        except Exception:
            logger.exception('Internal error while handling a request')
            return rpc.make_error(message.get('id'),
                                  rpc.INTERNAL_ERROR, 'Internal error')

    def _dispatch(self, message, notify=None):
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
        context = RequestContext(request_id, notify=notify,
                                 progress_token=progress_token)

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
        result['_meta'] = self._base_meta()
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
            capabilities['resources'] = {}
        return {
            'supportedVersions': [PROTOCOL_VERSION],
            'capabilities': capabilities,
            'instructions':
                'This server exposes one DebOps project directory: '
                'inspect its inventory, roles, playbooks and '
                'configuration, run playbooks within the project '
                'policy, and synchronize the project with its git '
                'origin. Reusable DebOps workflows are available as '
                'skill:// resources.',
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
                    notify=context.notify)
            return tool.handler(arguments, tool_context)
        finally:
            if request_id is not None:
                with self._active_lock:
                    self._active_requests.pop(request_id, None)

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
