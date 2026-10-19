# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the JSON-RPC framing of the DebOps MCP server.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
import io
import json
import unittest


class ReadMessageTestCase(unittest.TestCase):

    def _stream(self, data):
        return io.BytesIO(data)

    def test_single_message(self):
        stream = self._stream(b'{"method": "ping"}\n')
        self.assertEqual(rpc.read_message(stream), b'{"method": "ping"}')
        self.assertIsNone(rpc.read_message(stream))

    def test_eof_returns_none(self):
        self.assertIsNone(rpc.read_message(self._stream(b'')))

    def test_blank_lines_and_headers_skipped(self):
        stream = self._stream(
                b'\n'
                b'Content-Length: 42\n'
                b'Mcp-Method: tools/list\n'
                b'Mcp-Name: debops\n'
                b'{"method": "tools/list"}\n')
        self.assertEqual(rpc.read_message(stream),
                         b'{"method": "tools/list"}')

    def test_surrounding_whitespace_stripped(self):
        stream = self._stream(b'  {"method": "ping"}  \n')
        self.assertEqual(rpc.read_message(stream), b'{"method": "ping"}')

    def test_oversized_message_rejected_and_stream_resynchronized(self):
        stream = self._stream(b'{"big": "' + b'x' * 64 + b'"}\n'
                              b'{"method": "ping"}\n')
        with self.assertRaises(rpc.JsonRpcError) as context:
            rpc.read_message(stream, max_size=32)
        self.assertEqual(context.exception.code, rpc.REQUEST_TOO_LARGE)

        # The remainder of the oversized message was discarded, so the
        # next message is read cleanly
        self.assertEqual(rpc.read_message(stream, max_size=32),
                         b'{"method": "ping"}')

    def test_custom_codes_stay_outside_the_reserved_range(self):
        # The MCP specification reserves -32768..-32000 (with the
        # legacy sub-range -32000..-32019 disused); implementation
        # codes live outside of it
        self.assertGreater(rpc.REQUEST_TOO_LARGE, -32000)


class ParseMessageTestCase(unittest.TestCase):

    def test_valid_message(self):
        message = rpc.parse_message(b'{"method": "ping", "id": 1}')
        self.assertEqual(message, {'method': 'ping', 'id': 1})

    def test_invalid_json(self):
        with self.assertRaises(rpc.JsonRpcError) as context:
            rpc.parse_message(b'not json')
        self.assertEqual(context.exception.code, rpc.PARSE_ERROR)

    def test_non_object_message(self):
        with self.assertRaises(rpc.JsonRpcError) as context:
            rpc.parse_message(b'[1, 2, 3]')
        self.assertEqual(context.exception.code, rpc.INVALID_REQUEST)


class ResponseTestCase(unittest.TestCase):

    def test_make_result(self):
        self.assertEqual(rpc.make_result(7, {'ok': True}),
                         {'jsonrpc': '2.0', 'id': 7, 'result': {'ok': True}})

    def test_make_error(self):
        response = rpc.make_error(7, rpc.INVALID_PARAMS, 'Invalid params',
                                  data={'details': 'missing name'})
        self.assertEqual(response['jsonrpc'], '2.0')
        self.assertEqual(response['id'], 7)
        self.assertEqual(response['error']['code'], rpc.INVALID_PARAMS)
        self.assertEqual(response['error']['data'],
                         {'details': 'missing name'})

    def test_make_error_without_data(self):
        response = rpc.make_error(None, rpc.PARSE_ERROR, 'Parse error')
        self.assertIsNone(response['id'])
        self.assertNotIn('data', response['error'])

    def test_error_to_response(self):
        error = rpc.JsonRpcError(rpc.METHOD_NOT_FOUND, 'Method not found',
                                 request_id=3)
        response = error.to_response()
        self.assertEqual(response['id'], 3)
        self.assertEqual(response['error']['code'], rpc.METHOD_NOT_FOUND)


class WriteMessageTestCase(unittest.TestCase):

    def test_write_message(self):
        stream = io.BytesIO()
        rpc.write_message(stream, {'jsonrpc': '2.0', 'id': 1,
                                   'result': {'ok': True}})
        line = stream.getvalue()
        self.assertTrue(line.endswith(b'\n'))
        self.assertEqual(json.loads(line.decode('utf-8')),
                         {'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True}})


if __name__ == '__main__':
    unittest.main()
