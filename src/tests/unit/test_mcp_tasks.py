# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the MCP Tasks extension.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.server import McpServer, PROTOCOL_VERSION, TASKS_EXTENSION
import threading
import time
import unittest


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}

TASKS_META = dict(REQUEST_META, **{
        'io.modelcontextprotocol/clientCapabilities':
        {'extensions': {TASKS_EXTENSION: {}}}})


def _slow(arguments, context):
    if context.cancelled.wait(30):
        from debops.mcp.server import RequestCancelled
        raise RequestCancelled()
    return {'content': [{'type': 'text', 'text': 'done'}],
            'isError': False}


def _slowish(arguments, context):
    """A second of work: enough to see 'working' before 'completed'."""
    if context.cancelled.wait(1):
        from debops.mcp.server import RequestCancelled
        raise RequestCancelled()
    return {'content': [{'type': 'text', 'text': 'done'}],
            'isError': False}


def _quick(arguments, context):
    return {'content': [{'type': 'text',
                         'text': 'quick: ' + str(
                                 arguments.get('value', ''))}],
            'isError': False}


def _asking(arguments, context):
    if context.task is not None:
        answer = context.task.ask('confirmation', {
                'method': 'elicitation/create',
                'params': {'mode': 'form', 'message': 'Sure?',
                           'requestedSchema': {'type': 'object'}}})
        content = answer.get('content') or {}
        return {'content': [{'type': 'text',
                             'text': 'answer: {}'.format(
                                     content.get('confirmed'))}],
                'isError': False}
    return {'content': [{'type': 'text', 'text': 'no task'}],
            'isError': True}


class TaskTestCase(unittest.TestCase):

    def setUp(self):
        self.server = McpServer()
        self.server.register_tool('slow', 'A slow tool',
                                  {'type': 'object'}, _slow,
                                  needs_context=True, task_eligible=True)
        self.server.register_tool('quick', 'A quick tool',
                                  {'type': 'object'}, _quick,
                                  needs_context=True, task_eligible=True)
        self.server.register_tool('asking', 'A tool which asks',
                                  {'type': 'object'}, _asking,
                                  needs_context=True, task_eligible=True)

    def call(self, name, arguments=None, meta=None, request_id=1):
        return self.server.handle({
                'jsonrpc': '2.0',
                'id': request_id, 'method': 'tools/call',
                'params': {'_meta': meta or TASKS_META,
                           'name': name, 'arguments': arguments or {}}})

    def task_call(self, method, params, request_id=2):
        return self.server.handle({
                'jsonrpc': '2.0',
                'id': request_id, 'method': method,
                'params': dict(params or {}, _meta=TASKS_META)})

    def wait_task(self, task_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.task_call('tasks/get',
                                    {'taskId': task_id})['result']
            if result['status'] in ('completed', 'failed', 'cancelled',
                                    'input_required'):
                return result
            time.sleep(0.05)
        self.fail('the task did not reach a terminal or input state')

    def wait_done(self, task_id, timeout=10):
        """Wait for a terminal state only (past any input_required)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.task_call('tasks/get',
                                    {'taskId': task_id})['result']
            if result['status'] in ('completed', 'failed', 'cancelled'):
                return result
            time.sleep(0.05)
        self.fail('the task did not reach a terminal state')

    def test_discover_advertises_the_extension(self):
        result = self.server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'server/discover',
                'params': {'_meta': REQUEST_META}})['result']
        self.assertIn(TASKS_EXTENSION,
                      result['capabilities']['extensions'])

    def test_task_lifecycle(self):
        self.server.register_tool('slowish', 'A second of work',
                                  {'type': 'object'}, _slowish,
                                  needs_context=True, task_eligible=True)
        result = self.call('slowish')['result']
        self.assertEqual(result['resultType'], 'task')
        self.assertEqual(result['status'], 'working')
        self.assertTrue(result['taskId'])
        self.assertTrue(result['ttlMs'])
        self.assertEqual(result['pollIntervalMs'], 1000)

        done = self.wait_done(result['taskId'])
        self.assertEqual(done['resultType'], 'complete')
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['result']['content'][0]['text'], 'done')

    def test_without_the_capability_the_call_blocks(self):
        result = self.call('quick', {'value': 'x'}, meta=REQUEST_META)
        # No task: the plain result
        self.assertEqual(result['result']['content'][0]['text'],
                         'quick: x')

    def test_task_methods_require_the_capability(self):
        response = self.server.handle({
                'jsonrpc': '2.0', 'id': 1,
                'method': 'tasks/get',
                'params': {'_meta': REQUEST_META, 'taskId': 'nosuch'}})
        self.assertEqual(response['error']['code'], -32021)
        self.assertIn(TASKS_EXTENSION,
                      response['error']['data']['requiredCapabilities']
                      ['extensions'])

    def test_unknown_task(self):
        response = self.task_call('tasks/get', {'taskId': 'nosuch'})
        self.assertEqual(response['error']['code'], -32602)

    def test_cancel(self):
        result = self.call('slow')['result']
        task_id = result['taskId']
        ack = self.task_call('tasks/cancel', {'taskId': task_id})
        self.assertEqual(ack['result']['resultType'], 'complete')
        done = self.wait_task(task_id)
        self.assertEqual(done['status'], 'cancelled')

    def test_cancel_unknown_task(self):
        response = self.task_call('tasks/cancel', {'taskId': 'nosuch'})
        self.assertEqual(response['error']['code'], -32602)

    def test_task_for_a_tool_without_context(self):
        # A task-eligible tool which takes no context must still run
        self.server.register_tool('plain', 'A plain tool',
                                  {'type': 'object'},
                                  lambda arguments: {
                                      'content': [{'type': 'text',
                                                   'text': 'plain'}],
                                      'isError': False},
                                  task_eligible=True)
        result = self.call('plain')['result']
        self.assertEqual(result['resultType'], 'task')
        done = self.wait_done(result['taskId'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['result']['content'][0]['text'], 'plain')

    def test_input_required_and_update(self):
        result = self.call('asking')['result']
        task_id = result['taskId']
        task = self.wait_task(task_id)
        self.assertEqual(task['status'], 'input_required')
        self.assertIn('confirmation', task['inputRequests'])
        self.assertEqual(task['inputRequests']['confirmation']
                         ['params']['message'], 'Sure?')

        ack = self.task_call('tasks/update', {
                'taskId': task_id,
                'inputResponses': {'confirmation': {
                    'action': 'accept',
                    'content': {'confirmed': True}}}})
        self.assertEqual(ack['result']['resultType'], 'complete')
        done = self.wait_done(task_id)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['result']['content'][0]['text'],
                         'answer: True')

    def test_update_ignores_unknown_keys(self):
        result = self.call('asking')['result']
        task_id = result['taskId']
        self.wait_task(task_id)
        ack = self.task_call('tasks/update', {
                'taskId': task_id,
                'inputResponses': {'nosuch': {'action': 'accept'}}})
        self.assertEqual(ack['result']['resultType'], 'complete')
        self.task_call('tasks/cancel', {'taskId': task_id})
        self.wait_done(task_id)

    def test_expired_task_is_cancelled(self):
        result = self.call('slow')['result']
        task_id = result['taskId']
        with self.server._tasks._lock:
            task = self.server._tasks._tasks[task_id]
            task.ttl_ms = 0  # expire it
        outcome = self.wait_task(task_id)
        self.assertEqual(outcome['status'], 'cancelled')

    def test_finished_tasks_are_reaped(self):
        result = self.call('quick')['result']
        task_id = result['taskId']
        self.wait_task(task_id)
        with self.server._tasks._lock:
            task = self.server._tasks._tasks[task_id]
            # Age the finished task past the retention window
            from datetime import timedelta
            task.last_updated_at = (task.last_updated_at
                                    - timedelta(seconds=400))
        response = self.task_call('tasks/get', {'taskId': task_id})
        self.assertEqual(response['error']['code'], -32602)


if __name__ == '__main__':
    unittest.main()
