# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server dispatch.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer, PROTOCOL_VERSION, RequestCancelled
import json
import threading
import unittest


def make_request(method, params=None, request_id=1, meta=None):
    message = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        message['params'] = params
    if meta is not None:
        message['_meta'] = meta
    return message


class DiscoverTestCase(unittest.TestCase):

    def test_discover(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        self.assertEqual(response['jsonrpc'], '2.0')
        self.assertEqual(response['id'], 1)

        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['protocolVersion'], PROTOCOL_VERSION)
        self.assertEqual(result['serverInfo']['name'], 'debops')
        self.assertTrue(result['serverInfo']['version'])
        self.assertIn('tools', result['capabilities'])
        self.assertIsNone(result['authorization'])

        meta = response['_meta']['io.modelcontextprotocol/serverInfo']
        self.assertEqual(meta['name'], 'debops')

    def test_discover_with_protocol_version(self):
        server = McpServer()
        response = server.handle(make_request(
                'server/discover',
                meta={'io.modelcontextprotocol/protocolVersion':
                      PROTOCOL_VERSION}))
        self.assertIn('result', response)

    def test_unsupported_protocol_version(self):
        server = McpServer()
        response = server.handle(make_request(
                'server/discover',
                meta={'io.modelcontextprotocol/protocolVersion':
                      '2025-11-25'}))
        error = response['error']
        self.assertEqual(error['code'], rpc.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error['data']['supported'], PROTOCOL_VERSION)
        self.assertEqual(error['data']['requested'], '2025-11-25')


class EnvelopeTestCase(unittest.TestCase):

    def test_notification_gets_no_response(self):
        server = McpServer()
        request = make_request('server/discover')
        del request['id']
        self.assertIsNone(server.handle(request))

    def test_unknown_method(self):
        server = McpServer()
        response = server.handle(make_request('resources/list'))
        self.assertEqual(response['error']['code'], rpc.METHOD_NOT_FOUND)
        self.assertEqual(response['error']['data'],
                         {'method': 'resources/list'})

    def test_unknown_notification_ignored(self):
        server = McpServer()
        request = make_request('notifications/unknown')
        del request['id']
        self.assertIsNone(server.handle(request))

    def test_missing_method(self):
        server = McpServer()
        response = server.handle({'jsonrpc': '2.0', 'id': 1})
        self.assertEqual(response['error']['code'], rpc.INVALID_REQUEST)

    def test_params_must_be_an_object(self):
        server = McpServer()
        response = server.handle(make_request('server/discover',
                                              params=['not', 'a', 'dict']))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)

    def test_string_request_id_preserved(self):
        server = McpServer()
        response = server.handle(make_request('server/discover',
                                              request_id='req-42'))
        self.assertEqual(response['id'], 'req-42')


class HandleBytesTestCase(unittest.TestCase):

    def test_garbage_bytes(self):
        server = McpServer()
        response = server.handle_bytes(b'not json')
        self.assertEqual(response['error']['code'], rpc.PARSE_ERROR)
        self.assertIsNone(response['id'])

    def test_valid_bytes(self):
        server = McpServer()
        response = server.handle_bytes(
                b'{"jsonrpc": "2.0", "id": 9, "method": "tools/list"}')
        self.assertEqual(response['id'], 9)
        self.assertIn('tools', response['result'])


class ToolsTestCase(unittest.TestCase):

    def test_tools_list_empty(self):
        server = McpServer()
        response = server.handle(make_request('tools/list'))
        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertEqual(result['tools'], [])
        self.assertIsNone(result['nextCursor'])

    def test_tools_call_unknown_tool(self):
        server = McpServer()
        response = server.handle(make_request(
                'tools/call', params={'name': 'nope', 'arguments': {}}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertIn('nope', response['error']['data']['details'])

    def test_registered_tool_listed_and_called(self):
        server = McpServer()

        def echo(arguments):
            return {'content': [{'type': 'text',
                                 'text': arguments.get('text', '')}],
                    'isError': False}

        server.register_tool(
                'echo', 'Echo the given text',
                {'type': 'object',
                 'properties': {'text': {'type': 'string'}}},
                echo)

        response = server.handle(make_request('tools/list'))
        tools = response['result']['tools']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['name'], 'echo')
        self.assertEqual(tools[0]['description'], 'Echo the given text')
        self.assertNotIn('outputSchema', tools[0])

        response = server.handle(make_request(
                'tools/call',
                params={'name': 'echo', 'arguments': {'text': 'hello'}}))
        result = response['result']
        self.assertEqual(result['resultType'], 'complete')
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'],
                         [{'type': 'text', 'text': 'hello'}])

    def test_tools_call_arguments_must_be_an_object(self):
        server = McpServer()
        server.register_tool('echo', 'Echo', {'type': 'object'},
                             lambda arguments: {'content': []})
        response = server.handle(make_request(
                'tools/call', params={'name': 'echo', 'arguments': ['x']}))
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)


class CancellationTestCase(unittest.TestCase):

    def test_cancellation_stops_and_suppresses_the_request(self):
        server = McpServer()
        started = threading.Event()

        def slow(arguments, context):
            started.set()
            context.cancelled.wait(10)
            raise RequestCancelled()

        server.register_tool('slow', 'A slow tool', {'type': 'object'},
                             slow, needs_context=True)

        outcome = []

        def call():
            outcome.append(server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'slow', 'arguments': {}}}))

        thread = threading.Thread(target=call)
        thread.start()
        self.assertTrue(started.wait(5))

        # The cancellation notification itself gets no response
        self.assertIsNone(server.handle({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 1}}))
        thread.join(timeout=5)
        # ... and neither does the cancelled request
        self.assertEqual(outcome, [None])

    def test_cancellation_of_unknown_request_is_ignored(self):
        server = McpServer()
        self.assertIsNone(server.handle({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 'nosuch'}}))

    def test_tool_without_context_needs_no_registry(self):
        server = McpServer()
        server.register_tool('plain', 'Plain tool', {'type': 'object'},
                             lambda arguments: {'content': [],
                                                'isError': False})
        response = server.handle(make_request(
                'tools/call', params={'name': 'plain', 'arguments': {}}))
        self.assertEqual(response['result']['content'], [])
        self.assertEqual(server._active_requests, {})


class ProgressTestCase(unittest.TestCase):
    """Progress notifications travel the transport's notify channel."""

    def _noisy_server(self):
        server = McpServer()

        def noisy(arguments, context):
            context.progress('halfway', progress=1)
            return {'content': []}

        server.register_tool('noisy', 'Noisy tool', {'type': 'object'},
                             noisy, needs_context=True)
        return server

    def test_progress_with_a_token(self):
        server = self._noisy_server()
        sent = []
        request = make_request('tools/call', params={'name': 'noisy',
                                                     'arguments': {}},
                               meta={'progressToken': 'pt-42'})
        response = server.handle_bytes(json.dumps(request).encode(),
                                       notify=sent.append)
        self.assertIsNotNone(response)
        notifications = [message for message in sent
                         if message.get('method')
                         == 'notifications/progress']
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]['params']['progressToken'],
                         'pt-42')
        self.assertEqual(notifications[0]['params']['message'], 'halfway')

    def test_no_token_no_progress(self):
        server = self._noisy_server()
        sent = []
        request = make_request('tools/call', params={'name': 'noisy',
                                                     'arguments': {}})
        response = server.handle_bytes(json.dumps(request).encode(),
                                       notify=sent.append)
        self.assertIsNotNone(response)
        self.assertEqual(sent, [])


class ClacksOverheadTestCase(unittest.TestCase):
    """Every response carries the tribute in its '_meta' overhead."""

    def test_result_responses_carry_the_clacks_overhead(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        self.assertEqual(response['_meta']['org.debops/clacksOverhead'],
                         'GNU Terry Pratchett')
        # The discover serverInfo is intact alongside it
        self.assertIn('io.modelcontextprotocol/serverInfo',
                      response['_meta'])

    def test_error_responses_carry_the_clacks_overhead(self):
        server = McpServer()
        response = server.handle(make_request('nosuch/method'))
        self.assertEqual(response['_meta']['org.debops/clacksOverhead'],
                         'GNU Terry Pratchett')

    def test_parse_errors_carry_the_clacks_overhead(self):
        server = McpServer()
        response = server.handle_bytes(b'not json')
        self.assertEqual(response['_meta']['org.debops/clacksOverhead'],
                         'GNU Terry Pratchett')

    def test_project_and_view_in_meta_when_present(self):
        project = type('Project', (), {'name': 'containers',
                                       'view': 'system'})()
        server = McpServer(project=project)
        response = server.handle(make_request('server/discover'))
        self.assertEqual(response['_meta']['org.debops/project'],
                         'containers')
        self.assertEqual(response['_meta']['org.debops/view'], 'system')

    def test_no_project_no_project_meta(self):
        server = McpServer()
        response = server.handle(make_request('server/discover'))
        self.assertNotIn('org.debops/project', response['_meta'])
        self.assertNotIn('org.debops/view', response['_meta'])


if __name__ == '__main__':
    unittest.main()
