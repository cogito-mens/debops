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
SERVER_INFO_KEY = 'io.modelcontextprotocol/serverInfo'

# A tribute carried in the overhead of every response; '_meta' is
# protocol metadata consumed by clients and never shown to the model,
# and unknown namespaced keys are ignored by design
CLACKS_OVERHEAD = {'org.debops/clacksOverhead': 'GNU Terry Pratchett'}


class RequestCancelled(Exception):
    """Raised by a tool when its request was cancelled by the client.

    The server sends no response for a cancelled request, per the
    specification.
    """


class ToolContext(object):
    """Request-scoped context passed to tools which need it.

    'cancelled' is an event which is set when the client cancels the
    request via 'notifications/cancelled'; long-running tools should
    poll it and stop.
    """

    def __init__(self, request_id, cancelled):
        self.request_id = request_id
        self.cancelled = cancelled


class Tool(object):
    """A single tool exposed by the server."""

    def __init__(self, name, description, input_schema, handler,
                 output_schema=None, needs_context=False):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.handler = handler
        self.needs_context = needs_context

    def describe(self):
        description = {
            'name': self.name,
            'description': self.description,
            'inputSchema': self.input_schema,
        }
        if self.output_schema is not None:
            description['outputSchema'] = self.output_schema
        return description


class McpServer(object):
    """Dispatch MCP requests to their handlers."""

    def __init__(self, name='debops', version=None, project=None,
                 policy=None):
        self.name = name
        self.version = version or __version__
        # The project directory the server is confined to; tools resolve
        # all paths against it
        self.project = project
        self.policy = policy
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

    def register_tool(self, name, description, input_schema, handler,
                      output_schema=None, needs_context=False):
        """Register a tool exposed via tools/list and tools/call."""
        self.tools[name] = Tool(name, description, input_schema, handler,
                                output_schema, needs_context)

    def cancel_all(self):
        """Cancel every in-flight request (transport teardown)."""
        with self._active_lock:
            events = list(self._active_requests.values())
        for event in events:
            event.set()

    # -- transport entry point ---------------------------------------------

    def handle_bytes(self, data):
        """Handle one raw message; return the response or None."""
        try:
            message = rpc.parse_message(data)
        except rpc.JsonRpcError as errmsg:
            logger.warning('Rejecting an unparseable message: %s',
                           errmsg.message)
            response = errmsg.to_response()
            response['_meta'] = dict(CLACKS_OVERHEAD)
            return response
        return self.handle(message)

    def handle(self, message):
        """Handle one parsed message; return the response or None."""
        try:
            return self._dispatch(message)
        except rpc.JsonRpcError as errmsg:
            response = errmsg.to_response()
            response['_meta'] = dict(CLACKS_OVERHEAD)
            return response
        except Exception:
            logger.exception('Internal error while handling a request')
            response = rpc.make_error(message.get('id'),
                                      rpc.INTERNAL_ERROR, 'Internal error')
            response['_meta'] = dict(CLACKS_OVERHEAD)
            return response

    def _dispatch(self, message):
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

        self._check_protocol_version(message, request_id)

        if is_notification:
            logger.info('Notification: %s', method)
        else:
            logger.info('Request: %s (id=%s)', method, request_id)

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
            result = handler(params, request_id)
        except RequestCancelled:
            # A cancelled request gets no response
            logger.debug('Request %s was cancelled', request_id)
            return None
        if is_notification:
            return None
        if result is None:
            result = {}
        result.setdefault('resultType', 'complete')
        response = rpc.make_result(request_id, result)
        response['_meta'] = dict(CLACKS_OVERHEAD)
        if method == 'server/discover':
            response['_meta'][SERVER_INFO_KEY] = {
                'name': self.name, 'version': self.version}
        return response

    # -- protocol methods ---------------------------------------------------

    def _check_protocol_version(self, message, request_id):
        """Reject requests which explicitly ask for another revision.

        The field is mandatory in the specification, but a missing one is
        tolerated so that simple clients and manual testing keep working.
        """
        meta = message.get('_meta')
        if not isinstance(meta, dict):
            return
        version = meta.get(PROTOCOL_VERSION_KEY)
        if version is not None and version != PROTOCOL_VERSION:
            logger.warning('Unsupported protocol version requested: %s',
                           version)
            raise rpc.JsonRpcError(
                    rpc.UNSUPPORTED_PROTOCOL_VERSION,
                    'Unsupported protocol version',
                    data={'supported': PROTOCOL_VERSION,
                          'requested': version},
                    request_id=request_id)

    def _discover(self, params, request_id):
        return {
            'protocolVersion': PROTOCOL_VERSION,
            'serverInfo': {'name': self.name, 'version': self.version},
            'capabilities': {'tools': {}},
            'authorization': None,
        }

    def _tools_list(self, params, request_id):
        return {'tools': [tool.describe() for tool in self.tools.values()],
                'nextCursor': None}

    def _tools_call(self, params, request_id):
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

        cancel_event = threading.Event()
        if request_id is not None:
            with self._active_lock:
                self._active_requests[request_id] = cancel_event
        try:
            return tool.handler(arguments,
                                ToolContext(request_id, cancel_event))
        finally:
            if request_id is not None:
                with self._active_lock:
                    self._active_requests.pop(request_id, None)

    def _cancelled(self, params, request_id):
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
