# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the subscription streams.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.server import McpServer, PROTOCOL_VERSION
from debops.mcp.transport import UnixSocketTransport
from debops.mcp import rpc
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}

SUBSCRIPTION_ID_KEY = 'io.modelcontextprotocol/subscriptionId'


class SubscriptionStreamTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-subs-')
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

    def _open(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(self.path)
        return connection

    def _listen(self, connection, notifications, request_id=1):
        connection.sendall(json.dumps({
                'jsonrpc': '2.0', '_meta': REQUEST_META, 'id': request_id,
                'method': 'subscriptions/listen',
                'params': {'notifications': notifications}}).encode()
                + b'\n')

    def test_ack_comes_first_and_notifications_follow(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {'resourcesListChanged': True,
                                  'promptsListChanged': True,
                                  'toolsListChanged': True})

        # The acknowledgment is the first message on the stream, and it
        # reflects the honored subset (toolsListChanged is not emitted
        # by this server)
        ack = json.loads(rpc.read_message(reader))
        self.assertEqual(ack['method'],
                         'notifications/subscriptions/acknowledged')
        self.assertEqual(ack['params']['_meta'][SUBSCRIPTION_ID_KEY], 1)
        self.assertEqual(ack['params']['notifications'],
                         {'resourcesListChanged': True,
                          'promptsListChanged': True})

        self.server.emit_list_changed('prompts')
        notification = json.loads(rpc.read_message(reader))
        self.assertEqual(notification['method'],
                         'notifications/prompts/list_changed')
        self.assertEqual(notification['params']['_meta']
                         [SUBSCRIPTION_ID_KEY], 1)

    def test_unsubscribed_types_are_not_delivered(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {'resourcesListChanged': True})
        ack = json.loads(rpc.read_message(reader))
        self.assertEqual(ack['params']['notifications'],
                         {'resourcesListChanged': True})

        self.server.emit_list_changed('prompts')
        # Nothing arrives for the unsubscribed type; the next message
        # is the completion after cancellation
        connection.sendall(json.dumps({
                'jsonrpc': '2.0',
                'method': 'notifications/cancelled',
                'params': {'requestId': 1}}).encode() + b'\n')
        completion = json.loads(rpc.read_message(reader))
        self.assertEqual(completion['id'], 1)
        self.assertEqual(completion['result']['resultType'], 'complete')
        self.assertEqual(completion['result']['_meta']
                         [SUBSCRIPTION_ID_KEY], 1)

    def test_resource_update_notifications(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {
                'resourceSubscriptions': ['skill://debops-cli']})
        json.loads(rpc.read_message(reader))  # the ack

        self.server.emit_resource_updated('skill://debops-cli')
        notification = json.loads(rpc.read_message(reader))
        self.assertEqual(notification['method'],
                         'notifications/resources/updated')
        self.assertEqual(notification['params']['uri'],
                         'skill://debops-cli')

        # A URI we did not subscribe to is not delivered
        self.server.emit_resource_updated('skill://other')
        connection.sendall(json.dumps({
                'jsonrpc': '2.0',
                'method': 'notifications/cancelled',
                'params': {'requestId': 1}}).encode() + b'\n')
        completion = json.loads(rpc.read_message(reader))
        self.assertEqual(completion['id'], 1)

    def test_eof_ends_the_stream(self):
        connection = self._open()
        self._listen(connection, {'resourcesListChanged': True})
        reader = connection.makefile('rb')
        json.loads(rpc.read_message(reader))  # the ack
        # EOF: the stream ends with the channel. The makefile holds its
        # own file descriptor, so both are closed
        reader.close()
        connection.close()
        for _ in range(500):
            with self.server._subscriptions_lock:
                if not self.server._subscriptions:
                    return
            time.sleep(0.01)
        self.fail('the subscription outlived its channel')


class ContentAnnounceTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-announce-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.server = McpServer()
        self.events = []
        self.server.emit_list_changed = self.events.append

    def test_unchanged_content_announces_nothing(self):
        before = self.server.content_fingerprint()
        self.server.announce_content_changes(before)
        self.assertEqual(self.events, [])

    def test_new_content_announces_the_list_change(self):
        # No skills/prompts libraries here; simulate with stubs
        before = {'skills': {}, 'prompts': {}}
        self.server.skills = type('Skills', (), {
                'project_fingerprint': lambda self: {'new-skill': 1},
        })()
        self.server.announce_content_changes(before)
        self.assertEqual(self.events, ['resources'])


if __name__ == '__main__':
    unittest.main()
