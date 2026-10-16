# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server dispatch.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer, PROTOCOL_VERSION
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


if __name__ == '__main__':
    unittest.main()
