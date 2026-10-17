# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the secrets discovery tool.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer
from debops.mcp.tools import register_project_tools
from debops.mcp.tools.execute import _EXECUTION_LOCK
from unittest import mock
import os
import shutil
import tempfile
import threading
import unittest


class SecretPathsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-secrets-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.project_dir = os.path.join(self.tmpdir, 'project')
        secret = os.path.join(self.project_dir, 'ansible', 'secret')
        os.makedirs(os.path.join(secret, 'pki', 'authorities', 'root',
                                 'private'))
        os.makedirs(os.path.join(secret, 'credentials', 'kometa',
                                 'root_account'))
        os.makedirs(os.path.join(secret, 'dhparam', 'params'))
        with open(os.path.join(secret, 'pki', 'authorities', 'root',
                               'private', 'key.pem'), 'w') as fp:
            fp.write('PRIVATE KEY MATERIAL')
        with open(os.path.join(secret, 'credentials', 'kometa',
                               'root_account', 'password'), 'w') as fp:
            fp.write('the password')
        with open(os.path.join(secret, 'dhparam', 'params', 'dh.pem'),
                  'w') as fp:
            fp.write('dh params')

        # A symlinked directory leading outside of the secrets directory
        outside = os.path.join(self.tmpdir, 'outside')
        os.makedirs(outside)
        with open(os.path.join(outside, 'hidden.txt'), 'w') as fp:
            fp.write('hidden')
        os.symlink(outside, os.path.join(secret, 'linked'))

        self.project = type('Project', (), {
            'path': self.project_dir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
        })()
        self.server = McpServer(project=self.project,
                                policy=McpPolicy(self.project))
        register_project_tools(
                self.server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})

    def call_tool(self, arguments=None, unlocked=False):
        with mock.patch('debops.mcp.tools.secrets.AnsibleInventory') as inv:
            inv.return_value.unlock.return_value = unlocked
            return self.server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'secret_paths',
                           'arguments': arguments or {}}}), inv

    def result_text(self, response):
        return response['result']['content'][0]['text']

    def test_lists_all_secret_paths(self):
        response, _inventory = self.call_tool()
        text = self.result_text(response)
        self.assertFalse(response['result']['isError'])
        self.assertIn('- pki/authorities/root/private/key.pem', text)
        self.assertIn('- credentials/kometa/root_account/password', text)
        self.assertIn('- dhparam/params/dh.pem', text)
        # Contents are never served
        self.assertNotIn('PRIVATE KEY MATERIAL', text)
        self.assertNotIn('the password', text)
        # The lookup hint is included
        self.assertIn('lookup("password", secret + "/<path>")', text)

    def test_glob_narrows(self):
        response, _inventory = self.call_tool({'pattern': 'pki/**'})
        text = self.result_text(response)
        self.assertIn('pki/authorities/root/private/key.pem', text)
        self.assertNotIn('credentials', text)

    def test_glob_star_star(self):
        response, _inventory = self.call_tool({'pattern': '**/password'})
        text = self.result_text(response)
        self.assertIn('credentials/kometa/root_account/password', text)
        self.assertNotIn('key.pem', text)

    def test_symlink_escape_matches_nothing(self):
        response, _inventory = self.call_tool({'pattern': 'linked/**'})
        self.assertIn('no secrets match', self.result_text(response))

    def test_rejects_escaping_patterns(self):
        for pattern in ('/etc/passwd', '../outside', 'pki/../../x'):
            response, _inventory = self.call_tool({'pattern': pattern})
            self.assertTrue(response['result']['isError'], pattern)

    def test_no_match(self):
        response, _inventory = self.call_tool({'pattern': 'nosuch/**'})
        self.assertIn('no secrets match', self.result_text(response))

    def test_lock_only_when_we_unlocked(self):
        _response, inv = self.call_tool(unlocked=True)
        inv.return_value.lock.assert_called_once()

        _response, inv = self.call_tool(unlocked=False)
        inv.return_value.lock.assert_not_called()

    def test_unlock_failure_is_an_error_result(self):
        with mock.patch('debops.mcp.tools.secrets.AnsibleInventory') as inv:
            inv.return_value.unlock.side_effect = ChildProcessError(
                    'git working directory not clean')
            response = self.server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'secret_paths', 'arguments': {}}})
            self.assertTrue(response['result']['isError'])
            self.assertIn('cannot unlock', self.result_text(response))

    def test_waits_for_the_execution_lock(self):
        finished = threading.Event()

        def call():
            self.call_tool()
            finished.set()

        _EXECUTION_LOCK.acquire()
        try:
            thread = threading.Thread(target=call)
            thread.start()
            # The tool must not run while a playbook execution holds
            # the lock
            self.assertFalse(finished.wait(0.5))
        finally:
            _EXECUTION_LOCK.release()
        self.assertTrue(finished.wait(5))

    def test_deny_list_removes_the_tool(self):
        self.project.config.raw['project'] = {'mcp': {'deny':
                                                      ['secret_paths']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(
                server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        self.assertNotIn('secret_paths', server.tools)


class NoSecretsTestCase(unittest.TestCase):

    def test_view_without_secrets(self):
        tmpdir = tempfile.mkdtemp(prefix='debops-mcp-nosecrets-')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        project = type('Project', (), {
            'path': tmpdir, 'name': 'project', 'project_type': 'legacy',
            'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
        })()
        server = McpServer(project=project, policy=McpPolicy(project))
        register_project_tools(server, roles_path=tmpdir,
                               playbooks_path=tmpdir,
                               collection_index_provider=lambda: {})
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': 'secret_paths', 'arguments': {}}})
        self.assertIn('no secrets directory',
                      response['result']['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
