# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server transports.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer
from debops.mcp.transport import UnixSocketTransport
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest


def make_discover_request(request_id=1):
    return json.dumps({
        'jsonrpc': '2.0', 'id': request_id, 'method': 'server/discover',
        'params': {},
        '_meta': {'io.modelcontextprotocol/protocolVersion': '2026-07-28'},
    })


class UnixSocketTransportTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-test-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, 'test.sock')
        self.server = McpServer()
        self.transport = UnixSocketTransport(self.path)
        self.thread = threading.Thread(
                target=self.transport.serve, args=(self.server,),
                daemon=True)
        self.thread.start()
        for _ in range(500):
            if os.path.exists(self.path):
                break
            time.sleep(0.01)
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.transport.shutdown()
        self.thread.join(timeout=5)

    def _round_trip(self, request):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(self.path)
        connection.sendall(request.encode('utf-8') + b'\n')
        reader = connection.makefile('rb')
        return rpc.read_message(reader)

    def test_socket_created_with_mode(self):
        socket_stat = os.stat(self.path)
        self.assertTrue(stat.S_ISSOCK(socket_stat.st_mode))
        self.assertEqual(stat.S_IMODE(socket_stat.st_mode), 0o660)

    def test_discover_round_trip(self):
        response = json.loads(self._round_trip(make_discover_request()))
        self.assertEqual(response['id'], 1)
        self.assertEqual(response['result']['protocolVersion'],
                         '2026-07-28')

    def test_multiple_messages_on_one_connection(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(self.path)
        connection.sendall(make_discover_request(1).encode('utf-8') + b'\n')
        connection.sendall(b'{"jsonrpc": "2.0", "id": 2, '
                           b'"method": "tools/list", "params": {}}\n')
        reader = connection.makefile('rb')
        first = json.loads(rpc.read_message(reader))
        second = json.loads(rpc.read_message(reader))
        self.assertEqual(first['id'], 1)
        self.assertEqual(second['id'], 2)
        self.assertEqual(second['result']['tools'], [])

    def test_concurrent_connections(self):
        results = []
        errors = []

        def client(request_id):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                connection.connect(self.path)
                connection.sendall(
                        make_discover_request(request_id).encode('utf-8')
                        + b'\n')
                reader = connection.makefile('rb')
                response = json.loads(rpc.read_message(reader))
                results.append(response['id'])
            except Exception as errmsg:
                errors.append(errmsg)
            finally:
                connection.close()

        threads = [threading.Thread(target=client, args=(index,))
                   for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), list(range(8)))

    def test_parent_directory_created(self):
        path = os.path.join(self.tmpdir, 'subdir', 'other.sock')
        transport = UnixSocketTransport(path)
        transport._prepare_socket_path()
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_symlink_path_refused(self):
        target = os.path.join(self.tmpdir, 'target')
        open(target, 'w').close()
        link = os.path.join(self.tmpdir, 'link.sock')
        os.symlink(target, link)
        transport = UnixSocketTransport(link)
        with self.assertRaises(ValueError):
            transport._prepare_socket_path()

    def test_non_socket_file_refused(self):
        path = os.path.join(self.tmpdir, 'not-a-socket')
        open(path, 'w').close()
        transport = UnixSocketTransport(path)
        with self.assertRaises(FileExistsError):
            transport._prepare_socket_path()

    def test_stale_socket_replaced(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale_path = os.path.join(self.tmpdir, 'stale.sock')
        stale.bind(stale_path)
        stale.close()
        transport = UnixSocketTransport(stale_path)
        transport._prepare_socket_path()
        self.assertFalse(os.path.exists(stale_path))

    def test_live_server_detected(self):
        transport = UnixSocketTransport(self.path)
        with self.assertRaises(FileExistsError):
            transport._prepare_socket_path()


if __name__ == '__main__':
    unittest.main()
