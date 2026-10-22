# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the client-facing path presentation."""

import os
import unittest

from unittest import mock

from debops.mcp.present import make_path_presenter
from debops.mcp.server import McpServer, PROTOCOL_VERSION

HOME = os.path.expanduser('~')

REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


class PresenterTestCase(unittest.TestCase):

    def test_home_is_masked_in_text(self):
        present = make_path_presenter([], home=HOME)
        self.assertEqual(present('log: {}/x.log'.format(HOME)),
                         'log: ~/x.log')

    def test_monikers_mask_the_known_roots(self):
        present = make_path_presenter(
                [('/cache-root/debops/mcp/runs', 'run-logs:'),
                 ('/srv/collections', 'collections:')], home=HOME)
        text = present('log: /cache-root/debops/mcp/runs/x/'
                       'execution-1.log and '
                       '/srv/collections/ansible_collections/x')
        self.assertIn('run-logs:/x/execution-1.log', text)
        self.assertIn('collections:/ansible_collections/x', text)

    def test_a_longer_prefix_wins(self):
        present = make_path_presenter(
                [('/srv', 'a:'), ('/srv/deep', 'b:')], home=HOME)
        self.assertEqual(present('/srv/deep/x'), 'b:/x')

    def test_a_prefix_needs_a_separator(self):
        # '/srv/collections2' never trips the '/srv/collections' entry
        present = make_path_presenter(
                [('/srv/collections', 'collections:')], home=HOME)
        self.assertEqual(present('/srv/collections2/x'),
                         '/srv/collections2/x')

    def test_unrelated_paths_pass_through(self):
        # Paths inside the ansible output or on the managed hosts are
        # data, not the server's layout
        present = make_path_presenter([], home=HOME)
        self.assertEqual(present('see /etc/ansible/ansible.cfg'),
                         'see /etc/ansible/ansible.cfg')

    def test_structured_content_is_walked(self):
        present = make_path_presenter(
                [('/srv/collections', 'collections:')], home=HOME)
        result = present({
            'content': [{'type': 'text',
                         'text': 'log: {}/x.log'.format(HOME)}],
            'structuredContent': {
                'log': '/srv/collections/x/execution-1.log',
                '/srv/collections/keyed': True,
                'list': ['{}/y'.format(HOME)],
                'count': 3,
            },
        })
        self.assertEqual(result['content'][0]['text'], 'log: ~/x.log')
        structured = result['structuredContent']
        self.assertEqual(structured['log'],
                         'collections:/x/execution-1.log')
        self.assertIn('collections:/keyed', structured)
        self.assertEqual(structured['list'], ['~/y'])
        self.assertEqual(structured['count'], 3)


class ServerPresentationTestCase(unittest.TestCase):
    """The presentation applies at the tools/call boundary."""

    def _server(self, transport, text):
        server = McpServer(project=None, transport=transport)
        server.register_tool(
                'probe', 'report a path', {'type': 'object'},
                lambda arguments: {
                    'content': [{'type': 'text', 'text': text}],
                    'isError': False})
        return server

    def call_probe(self, server):
        return server.handle({
            'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call',
            'params': {'_meta': REQUEST_META, 'name': 'probe',
                       'arguments': {}},
        })

    def test_stdio_masks_the_home_directory(self):
        response = self.call_probe(self._server(
                'stdio', 'log: {}/x.log'.format(HOME)))
        text = response['result']['content'][0]['text']
        self.assertEqual(text, 'log: ~/x.log')

    def test_unix_masks_the_known_roots_and_home(self):
        from debops.mcp import runs
        log_path = os.path.join(runs.cache_root(), 'x',
                                'execution-1.log')
        with mock.patch('debops.mcp.tools.collections.'
                        'documentation_collection_roots',
                        return_value=['/srv/collections']):
            server = self._server(
                    'unix', '{} over {}/y and {}/secret'.format(
                            log_path, '/srv/collections', HOME))
        response = self.call_probe(server)
        text = response['result']['content'][0]['text']
        self.assertEqual(text, 'run-logs:/x/execution-1.log over '
                               'collections:/y and ~/secret')


if __name__ == '__main__':
    unittest.main()
