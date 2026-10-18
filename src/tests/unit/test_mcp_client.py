# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the human-facing MCP client.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import client
from debops.mcp.server import McpServer
from debops.mcp.transport import UnixSocketTransport
import os
import shutil
import tempfile
import threading
import time
import unittest


class CallArgumentsTestCase(unittest.TestCase):

    def test_scalars(self):
        self.assertEqual(client.parse_call_arguments(
                ['playbook=site', 'become=true', 'count=3',
                 'name=a=b']),
                {'playbook': 'site', 'become': True, 'count': 3,
                 'name': 'a=b'})

    def test_bad_pairs_rejected(self):
        with self.assertRaises(ValueError):
            client.parse_call_arguments(['no-separator'])
        with self.assertRaises(ValueError):
            client.parse_call_arguments(['=no-key'])


class FormatterTestCase(unittest.TestCase):

    def test_first_sentence(self):
        self.assertEqual(client._first_sentence('One. Two.'), 'One.')
        self.assertEqual(client._first_sentence('Single'), 'Single')
        self.assertEqual(client._first_sentence('Spaced.\n  Out.'), 'Spaced.')

    def test_first_sentence_survives_abbreviations(self):
        self.assertEqual(
                client._first_sentence('Playbook target, e.g. '
                                       '"service/nginx" or "site"'),
                'Playbook target, e.g. "service/nginx" or "site"')
        self.assertEqual(
                client._first_sentence('Paused, i.e. stopped. Next.'),
                'Paused, i.e. stopped.')

    def test_shorten(self):
        self.assertEqual(client._shorten('short'), 'short')
        self.assertEqual(client._shorten('x' * 100), 'x' * 61 + '...')
        self.assertEqual(client._shorten('First. ' + 'x' * 100), 'First.')

    def test_annotation_flags(self):
        self.assertEqual(client.annotation_flags(
                {'readOnlyHint': True, 'openWorldHint': False}),
                ['read-only', 'local'])
        self.assertEqual(client.annotation_flags(
                {'destructiveHint': True, 'idempotentHint': True}),
                ['destructive', 'idempotent'])
        self.assertEqual(client.annotation_flags(
                {'destructiveHint': False, 'idempotentHint': True}),
                ['non-destructive', 'idempotent'])
        # The spec defaults: not read-only, destructive
        self.assertEqual(client.annotation_flags({}), ['destructive'])

    def test_format_discover(self):
        lines = client.format_discover({
            'supportedVersions': ['2026-07-28'],
            'capabilities': {'tools': {}, 'resources': {}},
            'instructions': 'Do the thing.  And the other thing.',
            'ttlMs': 60000, 'cacheScope': 'private',
            '_meta': {'io.modelcontextprotocol/serverInfo':
                      {'name': 'debops', 'version': '1.0'},
                      'org.debops/project': 'containers',
                      'org.debops/view': 'system'},
        })
        text = '\n'.join(lines)
        self.assertIn('name: debops', text)
        self.assertIn('project: containers', text)
        self.assertIn('protocol: 2026-07-28', text)
        self.assertIn('tools: yes', text)
        self.assertIn('instructions:\n    Do the thing.', text)
        self.assertIn('cache: 60000 ms, private', text)

    def test_format_discover_without_project(self):
        lines = client.format_discover({
            'supportedVersions': ['2026-07-28'],
            'capabilities': {},
            '_meta': {'io.modelcontextprotocol/serverInfo':
                      {'name': 'debops', 'version': '1.0'}},
        })
        text = '\n'.join(lines)
        self.assertNotIn('project:', text)
        self.assertIn('capabilities: (none)', text)

    def test_format_tools(self):
        lines = client.format_tools([
            {'name': 'run',
             'description': 'Run things. Carefully.',
             'annotations': {'destructiveHint': True}},
            {'name': 'status', 'description': 'Show the status.',
             'annotations': {'readOnlyHint': True}},
        ])
        self.assertEqual(lines[0], 'tools (2):')
        self.assertIn('run', lines[1])
        self.assertIn('[destructive]', lines[1])
        self.assertIn('Show the status.', lines[2])
        self.assertIn('[read-only]', lines[2])

    def test_format_tool(self):
        lines = client.format_tool({
            'name': 'run',
            'description': 'Run things.',
            'annotations': {'destructiveHint': True},
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'playbook': {'type': 'string',
                                 'description': 'The playbook.'},
                    'limit': {'type': 'string',
                              'description': 'The limit.'},
                },
                'required': ['playbook'],
            },
        })
        text = '\n'.join(lines)
        self.assertIn('run — Run things.', text)
        self.assertIn('annotations: destructive', text)
        self.assertIn('playbook, required', text)
        self.assertIn('(string): The playbook.', text)
        self.assertIn('limit', text)

    def test_format_tool_without_parameters(self):
        lines = client.format_tool({'name': 'status',
                                    'description': 'Show the status.',
                                    'inputSchema': {'type': 'object'}})
        self.assertIn('parameters: (none)', lines)

    def test_format_tool_wraps_the_description(self):
        description = (' '.join(['word'] * 40)) + '.'
        lines = client.format_tool({'name': 'run',
                                    'description': description,
                                    'inputSchema': {'type': 'object'}})
        # Wrapped to the width, continuation aligned under the text
        self.assertTrue(all(len(line) <= 76 for line in lines))
        indent = len('run — ')
        self.assertEqual(lines[1][:indent], ' ' * indent)
        # Nothing is truncated in the detail view
        joined = ' '.join(' '.join(lines).split())
        self.assertEqual(joined.count('word'), 40)
        self.assertIn('word word word.', joined)

    def test_format_tool_wraps_parameter_descriptions(self):
        lines = client.format_tool({
            'name': 'run',
            'description': 'Run things.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'playbook': {'type': 'string',
                                 'description': ' '.join(['word'] * 40)},
                },
                'required': ['playbook'],
            },
        })
        index = lines.index('parameters:')
        first, second = lines[index + 1], lines[index + 2]
        # The continuation aligns under the description column
        column = first.index('word')
        self.assertEqual(second[:column], ' ' * column)

    def test_format_resources(self):
        lines = client.format_resources([
            {'name': 'debops-cli',
             'uri': 'skill://debops-cli',
             'description': 'The CLI skill.'},
        ])
        self.assertEqual(lines[0], 'resources (1):')
        self.assertIn('debops-cli', lines[1])
        self.assertIn('The CLI skill.', lines[1])


class UnixClientTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-client-')
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

    def test_requests_share_one_connection(self):
        with client.UnixClient(self.path) as connection:
            first = connection.request('server/discover')
            second = connection.request('tools/list')
        self.assertEqual(first['id'], 1)
        self.assertEqual(second['id'], 2)
        self.assertIn('supportedVersions', first['result'])
        self.assertEqual(second['result']['tools'], [])

    def test_request_unix_one_shot(self):
        response = client.request_unix(self.path, 'server/discover')
        self.assertIn(b'supportedVersions', response)

    def test_connection_error(self):
        with self.assertRaises(ConnectionError):
            client.request_unix(os.path.join(self.tmpdir, 'absent.sock'),
                                'server/discover')


if __name__ == '__main__':
    unittest.main()
