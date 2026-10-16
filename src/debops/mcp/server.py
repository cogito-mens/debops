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


class Tool(object):
    """A single tool exposed by the server."""

    def __init__(self, name, description, input_schema, handler,
                 output_schema=None):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.output_schema = output_schema
        self.handler = handler

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

    def __init__(self, name='debops', version=None, project=None):
        self.name = name
        self.version = version or __version__
        # The project directory the server is confined to; tools resolve
        # all paths against it
        self.project = project
        self.tools = {}
        self._methods = {
            'server/discover': self._discover,
            'tools/list': self._tools_list,
            'tools/call': self._tools_call,
            'notifications/cancelled': self._cancelled,
        }

    def register_tool(self, name, description, input_schema, handler,
                      output_schema=None):
        """Register a tool exposed via tools/list and tools/call."""
        self.tools[name] = Tool(name, description, input_schema, handler,
                                output_schema)

    # -- transport entry point ---------------------------------------------

    def handle_bytes(self, data):
        """Handle one raw message; return the response or None."""
        try:
            message = rpc.parse_message(data)
        except rpc.JsonRpcError as errmsg:
            return errmsg.to_response()
        return self.handle(message)

    def handle(self, message):
        """Handle one parsed message; return the response or None."""
        try:
            return self._dispatch(message)
        except rpc.JsonRpcError as errmsg:
            return errmsg.to_response()
        except Exception:
            logger.exception('Internal error while handling a request')
            return rpc.make_error(message.get('id'), rpc.INTERNAL_ERROR,
                                  'Internal error')

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

        handler = self._methods.get(method)
        if handler is None:
            if is_notification:
                logger.debug('Ignoring unknown notification: %s', method)
                return None
            raise rpc.JsonRpcError(rpc.METHOD_NOT_FOUND, 'Method not found',
                                   data={'method': method},
                                   request_id=request_id)

        params = message.get('params')
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise rpc.JsonRpcError(rpc.INVALID_PARAMS, 'Invalid params',
                                   request_id=request_id)

        result = handler(params)
        if is_notification:
            return None
        if result is None:
            result = {}
        result.setdefault('resultType', 'complete')
        response = rpc.make_result(request_id, result)
        if method == 'server/discover':
            response['_meta'] = {SERVER_INFO_KEY: {
                'name': self.name, 'version': self.version}}
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
            raise rpc.JsonRpcError(
                    rpc.UNSUPPORTED_PROTOCOL_VERSION,
                    'Unsupported protocol version',
                    data={'supported': PROTOCOL_VERSION,
                          'requested': version},
                    request_id=request_id)

    def _discover(self, params):
        return {
            'protocolVersion': PROTOCOL_VERSION,
            'serverInfo': {'name': self.name, 'version': self.version},
            'capabilities': {'tools': {}},
            'authorization': None,
        }

    def _tools_list(self, params):
        return {'tools': [tool.describe() for tool in self.tools.values()],
                'nextCursor': None}

    def _tools_call(self, params):
        name = params.get('name')
        tool = self.tools.get(name)
        if tool is None:
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
        return tool.handler(arguments)

    def _cancelled(self, params):
        # There are no long-running tools yet, so there is nothing to
        # cancel; the notification is accepted and ignored. Cooperative
        # cancellation arrives together with the tools.
        logger.debug('Received notifications/cancelled: %s', params)
        return None
