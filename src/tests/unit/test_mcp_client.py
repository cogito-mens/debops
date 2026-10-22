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
from tests.unit._helpers import wait_for_socket
import os
import shutil
import tempfile
import threading
import time
import unittest

from unittest import mock


class CallArgumentsTestCase(unittest.TestCase):

    def test_scalars(self):
        self.assertEqual(client.parse_call_arguments(
                ['playbook=site', 'become=true', 'count=3',
                 'name=a=b']),
                {'playbook': 'site', 'become': True, 'count': 3,
                 'name': 'a=b'})

    def test_leading_zero_stays_a_string(self):
        # An octal file mode is not an integer
        self.assertEqual(client.parse_call_arguments(
                ['mode=0755', 'count=0', 'offset=-3']),
                {'mode': '0755', 'count': 0, 'offset': -3})

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

    def test_confirm_marker_rendered(self):
        gated = {'name': 'run', 'description': 'Run things.',
                 'annotations': {'destructiveHint': True},
                 '_meta': {'org.debops/confirm': True}}
        plain = {'name': 'check', 'description': 'Check things.',
                 'annotations': {'destructiveHint': False}}
        lines = client.format_tools([gated, plain])
        by_name = {line.split()[0]: line for line in lines[1:]}
        self.assertIn('[destructive, confirm]', by_name['run'])
        self.assertNotIn('confirm', by_name['check'])

        detail = client.format_tool(gated)
        self.assertIn('annotations: destructive, confirm', detail)
        self.assertIn('confirmation: asked before the operation runs',
                      detail)

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
        # Deliberately unsorted: the listing comes out alphabetical
        lines = client.format_tools([
            {'name': 'status', 'description': 'Show the status.',
             'annotations': {'readOnlyHint': True}},
            {'name': 'run',
             'description': 'Run things. Carefully.',
             'annotations': {'destructiveHint': True}},
        ])
        self.assertEqual(lines[0], 'tools (2):')
        self.assertIn('run', lines[1])
        self.assertIn('[destructive]', lines[1])
        self.assertIn('Show the status.', lines[2])
        self.assertIn('[read-only]', lines[2])

    def test_format_prompts(self):
        # Deliberately unsorted: the listing comes out alphabetical
        lines = client.format_prompts([
            {'name': 'review-a-role',
             'description': 'Review a role against the conventions.'},
            {'name': 'bootstrap', 'description': 'Bootstrap a host.'},
        ])
        self.assertEqual(lines[0], 'prompts (2):')
        self.assertEqual(lines[1].split(),
                         ['bootstrap', 'Bootstrap', 'a', 'host.'])
        self.assertEqual(lines[2].split()[:2],
                         ['review-a-role', 'Review'])
        # The descriptions line up in a column
        self.assertEqual(lines[1].index('Bootstrap'),
                         lines[2].index('Review'))

    def test_format_resources(self):
        lines = client.format_resources([
            {'name': 'skill://zfs', 'description': 'Manage ZFS.'},
            {'name': 'skill://apt', 'description': 'Manage APT.'},
        ])
        self.assertEqual(lines[0], 'resources (2):')
        self.assertIn('skill://apt', lines[1])
        self.assertIn('skill://zfs', lines[2])
        self.assertEqual(lines[1].index('Manage'),
                         lines[2].index('Manage'))

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
        # (the trailing sections — annotations, parameters, examples —
        # are not description lines)
        cutoff = lines.index('annotations: destructive')
        self.assertTrue(all(len(line) <= 76 for line in lines[:cutoff]))
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

    def test_format_examples(self):
        lines = client.format_examples('run')
        self.assertEqual(lines[0], 'examples:')
        self.assertIn('debops mcp connect <socket> --call run '
                      'playbook=service/nginx', lines[1])
        # A tool without curated examples gets no section
        self.assertEqual(client.format_examples('nosuch'), [])

    def test_format_tool_shows_examples(self):
        lines = client.format_tool({'name': 'run',
                                    'description': 'Run things.',
                                    'inputSchema': {'type': 'object'}})
        self.assertIn('examples:', lines)

    def test_all_registered_tools_have_examples(self):
        # The curated examples must not drift from the tool surface
        from debops.mcp.tools import TOOL_ANNOTATIONS
        for name in client.TOOL_CALL_EXAMPLES:
            self.assertIn(name, TOOL_ANNOTATIONS,
                          'examples for an unknown tool: ' + name)
        for name in TOOL_ANNOTATIONS:
            self.assertIn(name, client.TOOL_CALL_EXAMPLES,
                          'no examples for the tool: ' + name)

    def test_format_resources(self):
        lines = client.format_resources([
            {'name': 'debops-cli',
             'uri': 'skill://debops-cli/SKILL.md',
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
        wait_for_socket(self.path)
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

    def test_notify_does_not_disturb_requests(self):
        with client.UnixClient(self.path) as connection:
            connection.notify('notifications/cancelled',
                              {'requestId': 'nosuch'})
            response = connection.request('tools/list')
        # The notification consumed no request id and no response slot
        self.assertEqual(response['id'], 1)
        self.assertEqual(response['result']['tools'], [])

    def test_connection_error(self):
        with self.assertRaises(ConnectionError):
            client.request_unix(os.path.join(self.tmpdir, 'absent.sock'),
                                'server/discover')

    def test_request_streaming_round_trip(self):
        # The stream: the ack, then the notifications, then the
        # completion ends it
        with client.UnixClient(self.path) as connection:
            stream = connection.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'toolsListChanged': True}})
            ack = next(stream)
            self.assertEqual(ack['method'],
                             'notifications/subscriptions/acknowledged')
            connection.notify('notifications/cancelled',
                              {'requestId': connection.inflight_id})
            with self.assertRaises(StopIteration):
                next(stream)

    def test_request_streaming_idles_past_the_request_timeout(self):
        # A quiet stream must not die on the per-request timeout: the
        # read timeout is off for the stream and restored after
        with client.UnixClient(self.path, timeout=1) as connection:
            stream = connection.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'toolsListChanged': True}})
            next(stream)  # the ack
            self.assertIsNone(connection._connection.gettimeout())
            # Longer than the client timeout, no event
            time.sleep(1.5)
            self.server.emit_list_changed('tools')
            for message in stream:
                self.assertEqual(message['method'],
                                 'notifications/tools/list_changed')
                break
            connection.notify('notifications/cancelled',
                              {'requestId': connection.inflight_id})
            with self.assertRaises(StopIteration):
                next(stream)
            self.assertEqual(connection._connection.gettimeout(), 1)


class SocketPathTestCase(unittest.TestCase):

    def test_the_default_socket_path(self):
        from debops.mcp.client import default_socket_path
        with mock.patch.dict(os.environ,
                             {'XDG_RUNTIME_DIR': '/run/user/1000'}):
            path = default_socket_path(
                    type('Project', (), {'path': '/srv/projects/example',
                                         'name': 'example'})())
            self.assertTrue(path.startswith('/run/user/1000/debops/'
                                            'mcp/'))
            self.assertTrue(path.endswith('-example.sock'))
            # The path hash keeps two same-named projects apart
            other = default_socket_path(
                    type('Project', (), {'path': '/srv/other/example',
                                         'name': 'example'})())
            self.assertNotEqual(path, other)
            # The documentation mode has its own
            self.assertTrue(default_socket_path().endswith(
                    'documentation.sock'))

    def test_a_socket_name_too_long_for_af_unix_is_truncated(self):
        from debops.mcp.client import default_socket_path
        deep = '/tmp/' + 'x' * 60
        with mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': deep}):
            path = default_socket_path(
                    type('Project', (), {'path': '/srv/projects/example',
                                         'name': 'example'})())
            # The AF_UNIX limit is honored, the hash keeps it unique
            self.assertLessEqual(len(path.encode('utf-8')), 100)
            self.assertIn('-example', path)


class LegacyUnixClientTestCase(unittest.TestCase):
    """The client speaks the 2025-era revision with 'legacy'."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-legacy-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.path = os.path.join(self.tmpdir, 'legacy.sock')
        self.transport = None
        self.thread = None

    def _serve(self, legacy):
        server = McpServer(legacy=legacy)
        self.transport = UnixSocketTransport(self.path)
        self.thread = threading.Thread(
                target=self.transport.serve, args=(server,),
                daemon=True)
        self.thread.start()
        wait_for_socket(self.path)

    def tearDown(self):
        if self.transport is not None:
            self.transport.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def test_the_handshake_then_plain_requests(self):
        self._serve(legacy=True)
        with client.UnixClient(self.path, legacy=True) as connection:
            response = connection.request('tools/list')
        # The handshake happened once: the ids went initialize (1),
        # then this request (2)
        self.assertEqual(response['id'], 2)
        self.assertEqual(response['result']['tools'], [])

    def test_legacy_requests_carry_no_meta_envelope(self):
        self._serve(legacy=True)
        with client.UnixClient(self.path, legacy=True) as connection:
            raw = connection.request_raw('tools/list')
        # The 2025-era answer: no resultType marker
        self.assertNotIn(b'resultType', raw)

    def test_the_handshake_is_refused_by_a_native_server(self):
        self._serve(legacy=False)
        with self.assertRaises(ConnectionError) as context:
            client.UnixClient(self.path, legacy=True).request(
                    'tools/list')
        self.assertIn('2026-07-28', str(context.exception))

    def test_the_handshake_learns_the_server_identity(self):
        self._serve(legacy=True)
        with client.UnixClient(self.path, legacy=True) as connection:
            connection.request('tools/list')
        self.assertEqual(connection.protocol_version, '2025-11-25')
        self.assertTrue(connection.server_version)
