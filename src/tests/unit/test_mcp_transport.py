# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the DebOps MCP server transports.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import rpc
from debops.mcp.server import McpServer
from debops.mcp.transport import (StdioTransport, UnixSocketTransport,
                                  ping_unix)
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': '2026-07-28',
    'io.modelcontextprotocol/clientCapabilities': {},
}


def make_discover_request(request_id=1):
    return json.dumps({
        'jsonrpc': '2.0', 'id': request_id, 'method': 'server/discover',
        'params': {
            '_meta': {'io.modelcontextprotocol/protocolVersion':
                      '2026-07-28',
                      'io.modelcontextprotocol/clientCapabilities': {}},
        },
    })


class TransportLifecycleTestCase(unittest.TestCase):
    """The on_ready/on_stop callbacks drive the serve hooks."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-lifecycle-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_socket_ready_and_stop(self):
        events = []
        path = os.path.join(self.tmpdir, 'cb.sock')
        transport = UnixSocketTransport(path)
        thread = threading.Thread(
                target=transport.serve, args=(McpServer(),),
                kwargs={'on_ready': lambda: events.append('ready'),
                        'on_stop': lambda: events.append('stop')},
                daemon=True)
        thread.start()
        # Wait for the readiness itself: the socket file appears at
        # bind(2) time, which precedes the callback, and under load the
        # thread may not get there before the next poll tick
        for _ in range(500):
            if events:
                break
            time.sleep(0.01)
        self.assertEqual(events, ['ready'])
        transport.shutdown()
        thread.join(timeout=5)
        self.assertEqual(events, ['ready', 'stop'])

    def test_stdio_ready_and_stop(self):
        events = []
        # An immediate EOF ends the stdio serving
        import io
        reader = io.BytesIO(b'')
        writer = io.BytesIO()
        import sys
        old_stdin, old_stdout = sys.stdin, sys.stdout
        sys.stdin = type('FakeStdin', (), {'buffer': reader})()
        sys.stdout = type('FakeStdout', (), {'buffer': writer})()
        try:
            transport = StdioTransport()
            transport.serve(McpServer(),
                            on_ready=lambda: events.append('ready'),
                            on_stop=lambda: events.append('stop'))
        finally:
            sys.stdin, sys.stdout = old_stdin, old_stdout
        self.assertEqual(events, ['ready', 'stop'])


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
        else:
            self.fail('the server socket did not appear within 5 '
                      'seconds: ' + self.path)
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
        self.assertEqual(response['result']['supportedVersions'],
                         ['2026-07-28'])

    def test_multiple_messages_on_one_connection(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(self.path)
        connection.sendall(make_discover_request(1).encode('utf-8') + b'\n')
        connection.sendall(json.dumps({
                'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
                'params': {'_meta': REQUEST_META}}).encode() + b'\n')
        reader = connection.makefile('rb')
        # Requests run concurrently; responses may arrive out of order
        responses = {}
        for _ in range(2):
            response = json.loads(rpc.read_message(reader))
            responses[response['id']] = response
        self.assertEqual(responses[1]['result']['supportedVersions'],
                         ['2026-07-28'])
        self.assertEqual(responses[2]['result']['tools'], [])

    def test_progress_notifications_on_the_wire(self):
        server = McpServer()

        def noisy(arguments, context):
            context.progress('working', progress=1)
            return {'content': [{'type': 'text', 'text': 'done'}]}

        server.register_tool('noisy', 'A noisy tool', {'type': 'object'},
                             noisy, needs_context=True)
        path = os.path.join(self.tmpdir, 'progress.sock')
        transport = UnixSocketTransport(path)
        thread = threading.Thread(target=transport.serve, args=(server,),
                                  daemon=True)
        thread.start()
        try:
            for _ in range(500):
                if os.path.exists(path):
                    break
                time.sleep(0.01)

            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(path)
            connection.sendall(json.dumps({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'noisy', 'arguments': {},
                           '_meta': {'progressToken': 'pt-9',
                                     'io.modelcontextprotocol/'
                                     'protocolVersion': '2026-07-28',
                                     'io.modelcontextprotocol/'
                                     'clientCapabilities': {}}}}
                ).encode() + b'\n')
            reader = connection.makefile('rb')
            # The progress notification precedes the response
            first = json.loads(rpc.read_message(reader))
            second = json.loads(rpc.read_message(reader))
            self.assertEqual(first.get('method'), 'notifications/progress')
            self.assertEqual(first['params']['progressToken'], 'pt-9')
            self.assertEqual(first['params']['message'], 'working')
            self.assertEqual(second['id'], 1)
            connection.close()
        finally:
            transport.shutdown()
            thread.join(timeout=5)

    def test_requests_run_concurrently(self):
        # A slow request must not block a later one on the same connection
        gate = threading.Event()
        server = McpServer()

        def slow(arguments, context):
            gate.wait(10)
            return {'content': [{'type': 'text', 'text': 'slow'}]}

        server.register_tool('slow', 'A slow tool', {'type': 'object'},
                             slow, needs_context=True)
        path = os.path.join(self.tmpdir, 'concurrent.sock')
        transport = UnixSocketTransport(path)
        thread = threading.Thread(target=transport.serve, args=(server,),
                                  daemon=True)
        thread.start()
        try:
            for _ in range(500):
                if os.path.exists(path):
                    break
                time.sleep(0.01)

            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(path)
            connection.sendall(json.dumps({
                    'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                    'params': {'_meta': REQUEST_META, 'name': 'slow',
                               'arguments': {}},
                    }).encode() + b'\n')
            connection.sendall(json.dumps({
                    'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list',
                    'params': {'_meta': REQUEST_META},
                    }).encode() + b'\n')
            reader = connection.makefile('rb')
            first = json.loads(rpc.read_message(reader))
            self.assertEqual(first['id'], 2)
            gate.set()
            second = json.loads(rpc.read_message(reader))
            self.assertEqual(second['id'], 1)
            connection.close()
        finally:
            gate.set()
            transport.shutdown()
            thread.join(timeout=5)

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

    def test_ping_unix_round_trip(self):
        response = json.loads(ping_unix(self.path))
        meta = response['result']['_meta']
        self.assertEqual(meta['io.modelcontextprotocol/serverInfo']
                         ['name'], 'debops')
        self.assertEqual(response['result']['supportedVersions'],
                         ['2026-07-28'])
        # The clacks overhead rides along
        self.assertEqual(meta['org.debops/clacksOverhead'],
                         'GNU Terry Pratchett')

    def test_ping_unix_missing_socket(self):
        with self.assertRaises(ConnectionError):
            ping_unix(os.path.join(self.tmpdir, 'nosuch.sock'))

    def test_ping_unix_refused_by_kill_switch(self):
        disabled = type('Switch', (),
                        {'is_enabled': lambda self: False})()
        server = McpServer(killswitch=disabled)
        path = os.path.join(self.tmpdir, 'killed-ping.sock')
        transport = UnixSocketTransport(path)
        thread = threading.Thread(target=transport.serve, args=(server,),
                                  daemon=True)
        thread.start()
        try:
            for _ in range(500):
                if os.path.exists(path):
                    break
                time.sleep(0.01)
            with self.assertRaises(EOFError):
                ping_unix(path)
        finally:
            transport.shutdown()
            thread.join(timeout=5)

    def test_ping_unix_timeout(self):
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        silent_path = os.path.join(self.tmpdir, 'silent.sock')
        silent.bind(silent_path)
        silent.listen()
        self.addCleanup(silent.close)

        def hold():
            connection, _ = silent.accept()
            time.sleep(3)
            connection.close()

        threading.Thread(target=hold, daemon=True).start()
        with self.assertRaises(TimeoutError):
            ping_unix(silent_path, timeout=1)

    def test_kill_switch_refuses_connections(self):
        disabled = type('Switch', (),
                        {'is_enabled': lambda self: False})()
        server = McpServer(killswitch=disabled)
        path = os.path.join(self.tmpdir, 'killed.sock')
        transport = UnixSocketTransport(path)
        thread = threading.Thread(target=transport.serve, args=(server,),
                                  daemon=True)
        thread.start()
        try:
            for _ in range(500):
                if os.path.exists(path):
                    break
                time.sleep(0.01)

            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(path)
            connection.sendall(make_discover_request(1).encode('utf-8')
                               + b'\n')
            reader = connection.makefile('rb')
            # The connection was closed without a response; pending
            # unread data can turn that into a reset instead of an EOF
            try:
                self.assertIsNone(rpc.read_message(reader))
            except ConnectionResetError:
                pass
            connection.close()
        finally:
            transport.shutdown()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
