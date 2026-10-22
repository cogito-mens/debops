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
from tests.unit._helpers import wait_for_socket
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
        wait_for_socket(self.path)
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.transport.shutdown()
        self.thread.join(timeout=5)

    def _open(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(connection.close)
        connection.connect(self.path)
        return connection

    def _listen(self, connection, notifications, request_id=1,
                meta=None):
        connection.sendall(json.dumps({
                'jsonrpc': '2.0',
                'id': request_id,
                'method': 'subscriptions/listen',
                'params': {'_meta': meta or REQUEST_META,
                           'notifications': notifications}
                }).encode() + b'\n')

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
                          'promptsListChanged': True,
                          'toolsListChanged': True})

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

    def test_the_activity_topic_needs_a_trusted_client(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {'activity': True})
        response = json.loads(rpc.read_message(reader))
        self.assertIn('error', response)
        self.assertIn('trusted',
                      response['error']['data']['details'])

    def test_resource_update_notifications(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {
                'resourceSubscriptions': ['skill://debops-cli/SKILL.md']})
        json.loads(rpc.read_message(reader))  # the ack

        self.server.emit_resource_updated('skill://debops-cli/SKILL.md')
        notification = json.loads(rpc.read_message(reader))
        self.assertEqual(notification['method'],
                         'notifications/resources/updated')
        self.assertEqual(notification['params']['uri'],
                         'skill://debops-cli/SKILL.md')

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

    def test_task_notifications_on_the_stream(self):
        from debops.mcp.server import TASKS_EXTENSION
        tasks_meta = dict(REQUEST_META, **{
                'io.modelcontextprotocol/clientCapabilities':
                {'extensions': {TASKS_EXTENSION: {}}}})

        def slowish(arguments, context):
            context.cancelled.wait(1)
            return {'content': [{'type': 'text', 'text': 'done'}],
                    'isError': False}

        self.server.register_tool('slowish', 'A second of work',
                                  {'type': 'object'}, slowish,
                                  needs_context=True, task_eligible=True)
        # Start the task, then subscribe to it
        connection = self._open()
        reader = connection.makefile('rb')
        connection.sendall(json.dumps({
                'jsonrpc': '2.0', 'id': 10,
                'method': 'tools/call',
                'params': {'_meta': tasks_meta,
                           'name': 'slowish', 'arguments': {}},
                }).encode() + b'\n')
        created = json.loads(rpc.read_message(reader))
        task_id = created['result']['taskId']

        self._listen(connection, {'taskIds': [task_id]}, request_id=11,
                     meta=tasks_meta)
        ack = json.loads(rpc.read_message(reader))
        self.assertEqual(ack['params']['notifications'],
                         {'taskIds': [task_id]})
        # The completion arrives on the stream with the full task
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = json.loads(rpc.read_message(reader))
            if message.get('method') == 'notifications/tasks':
                break
        self.assertEqual(message['method'], 'notifications/tasks')
        self.assertEqual(message['params']['taskId'], task_id)
        self.assertEqual(message['params']['status'], 'completed')
        self.assertEqual(message['params']['result']['content'][0]
                         ['text'], 'done')

    def test_task_notifications_require_the_capability(self):
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {'taskIds': ['nosuch']})
        response = json.loads(rpc.read_message(reader))
        self.assertEqual(response['error']['code'], -32021)

    def test_unsubscribed_tasks_do_not_leak(self):
        from debops.mcp.server import TASKS_EXTENSION
        tasks_meta = dict(REQUEST_META, **{
                'io.modelcontextprotocol/clientCapabilities':
                {'extensions': {TASKS_EXTENSION: {}}}})
        connection = self._open()
        reader = connection.makefile('rb')
        self._listen(connection, {'taskIds': ['some-other-task']},
                     meta=tasks_meta)
        json.loads(rpc.read_message(reader))  # the ack
        task = self.server._tasks.create(60000)
        task.complete({'content': [], 'isError': False})
        # Nothing arrives for an unsubscribed task
        connection.sendall(json.dumps({
                'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                'params': {'requestId': 1}}).encode() + b'\n')
        completion = json.loads(rpc.read_message(reader))
        self.assertEqual(completion['id'], 1)
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
