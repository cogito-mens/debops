# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the read-only project file and inventory tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import PROTOCOL_VERSION, McpServer
from debops.mcp.tools import register_project_tools
from unittest import mock
import os
import shutil
import subprocess
import tempfile
import unittest


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


class FilesTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-files-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        project_dir = os.path.join(self.tmpdir, 'project')
        os.makedirs(os.path.join(project_dir, 'ansible', 'inventory',
                                 'group_vars', 'all'))
        os.makedirs(os.path.join(project_dir, 'ansible', 'playbooks'))
        os.makedirs(os.path.join(project_dir, 'ansible', 'secret'))
        os.makedirs(os.path.join(project_dir, '.git', 'git-crypt', 'keys'))
        os.makedirs(os.path.join(project_dir, '.debops', 'conf.d'))
        os.makedirs(os.path.join(project_dir, '.debops', 'venv', 'lib'))
        with open(os.path.join(project_dir, 'ansible', 'inventory',
                               'hosts'), 'w') as fp:
            fp.write('[testgroup]\ntesthost\n')
        with open(os.path.join(project_dir, 'ansible', 'inventory',
                               'group_vars', 'all', 'ferm.yml'), 'w') as fp:
            fp.write('---\nferm__enabled: True\n')
        with open(os.path.join(project_dir, 'ansible', 'playbooks',
                               'custom.yml'), 'w') as fp:
            fp.write('---\n[]\n')
        with open(os.path.join(project_dir, 'ansible', 'secret',
                               'passwords.yml'), 'w') as fp:
            fp.write('---\nsecret: true\n')
        with open(os.path.join(project_dir, '.git', 'git-crypt', 'keys',
                               'key.gpg'), 'w') as fp:
            fp.write('key material')
        with open(os.path.join(project_dir, '.debops', 'conf.d',
                               'project.yml'), 'w') as fp:
            fp.write('---\nproject: {}\n')
        with open(os.path.join(project_dir, '.debops', 'venv', 'lib',
                               'installed.py'), 'w') as fp:
            fp.write('# venv content\n')
        with open(os.path.join(project_dir, 'ansible', 'inventory',
                               'blob.bin'), 'wb') as fp:
            fp.write(b'\xff\xd8\xff\xe0')

        self.project = type('Project', (), {
            'path': project_dir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'config': type('Config', (), {
                '_env_vars': {},
                'raw': {
                    'syslog': {'level': 'warning'},
                    'project': {'name': 'project'},
                },
            })(),
        })()
        self.policy = McpPolicy(self.project)
        self.server = McpServer(project=self.project, policy=self.policy)
        register_project_tools(
                self.server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})

    def call_tool(self, name, arguments=None, request_id=1):
        return self.server.handle({
            'jsonrpc': '2.0',
            'id': request_id, 'method': 'tools/call',
            'params': {'_meta': REQUEST_META,
                       'name': name, 'arguments': arguments or {}},
        })

    def result_text(self, response):
        return response['result']['content'][0]['text']

    # -- project_files ---------------------------------------------------

    def test_project_files(self):
        text = self.result_text(self.call_tool('project_files'))
        self.assertIn('ansible/inventory/hosts', text)
        self.assertIn('ansible/inventory/group_vars/all/ferm.yml', text)
        self.assertIn('.debops/conf.d/project.yml', text)
        # Denied subtrees are pruned
        self.assertNotIn('secret', text)
        self.assertNotIn('.git', text)
        self.assertNotIn('venv', text)

    def test_project_files_subdir_and_filter(self):
        text = self.result_text(self.call_tool(
                'project_files', {'path': 'ansible/inventory',
                                  'filter': 'ferm'}))
        self.assertIn('group_vars/all/ferm.yml', text)
        self.assertNotIn('hosts', text)

    def test_project_files_rejects_traversal(self):
        response = self.call_tool('project_files', {'path': '../..'})
        self.assertTrue(response['result']['isError'])

    # -- project_read ------------------------------------------------------

    def test_project_read(self):
        text = self.result_text(self.call_tool(
                'project_read',
                {'path': 'ansible/inventory/group_vars/all/ferm.yml'}))
        self.assertIn('ferm__enabled: True', text)

    def test_project_read_rejects_traversal(self):
        response = self.call_tool('project_read', {'path': '../../../etc'})
        self.assertTrue(response['result']['isError'])

    def test_project_read_denies_secrets(self):
        response = self.call_tool('project_read',
                                  {'path': 'ansible/secret/passwords.yml'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('denied', self.result_text(response))

    def test_project_read_denies_git_metadata(self):
        response = self.call_tool('project_read',
                                  {'path': '.git/git-crypt/keys/key.gpg'})
        self.assertTrue(response['result']['isError'])

    def test_project_read_rejects_binary_files(self):
        response = self.call_tool('project_read',
                                  {'path': 'ansible/inventory/blob.bin'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('UTF-8', self.result_text(response))

    def test_project_read_missing_file(self):
        response = self.call_tool('project_read',
                                  {'path': 'nosuch.yml'})
        self.assertTrue(response['result']['isError'])

    def test_project_read_deny_list_from_configuration(self):
        self.project.config.raw['project']['mcp'] = {
                'read_deny': ['ansible/playbooks']}
        policy = McpPolicy(self.project)
        server = McpServer(project=self.project, policy=policy)
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'_meta': REQUEST_META, 'name': 'project_read',
                           'arguments': {'path': 'ansible/playbooks/'
                                         'custom.yml'}}})
        self.assertTrue(response['result']['isError'])
        self.assertIn('denied', response['result']['content'][0]['text'])

    # -- config_get ---------------------------------------------------------

    def test_config_get(self):
        text = self.result_text(self.call_tool('config_get',
                                               {'key': 'syslog.level'}))
        self.assertIn('warning', text)

    def test_config_get_subtree(self):
        text = self.result_text(self.call_tool('config_get',
                                               {'key': 'project'}))
        self.assertIn('name: project', text)

    def test_config_get_missing_key(self):
        response = self.call_tool('config_get', {'key': 'nosuch.key'})
        self.assertTrue(response['result']['isError'])

    # -- inventory_query ----------------------------------------------------

    def test_inventory_query_graph(self):
        with mock.patch('debops.mcp.tools.files.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout='@all:\n  |--@testgroup:\n  |  |--testhost\n',
                    stderr='')
            response = self.call_tool('inventory_query',
                                      {'action': 'graph'})
        command = run.call_args[0][0]
        self.assertEqual(command[:2], ['ansible-inventory', '--graph'])
        self.assertIn('@testgroup', self.result_text(response))

    def test_inventory_query_host_requires_a_host(self):
        response = self.call_tool('inventory_query', {'action': 'host'})
        self.assertTrue(response['result']['isError'])

    def test_inventory_query_host_rejects_option_injection(self):
        response = self.call_tool('inventory_query',
                                  {'action': 'host', 'host': '--list'})
        self.assertTrue(response['result']['isError'])

    def test_inventory_query_failure_reports_stderr(self):
        with mock.patch('debops.mcp.tools.files.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=1, stdout='',
                    stderr='ERROR! the inventory could not be parsed')
            response = self.call_tool('inventory_query',
                                      {'action': 'graph'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('could not be parsed', self.result_text(response))

    def test_inventory_query_truncates_large_output(self):
        with mock.patch('debops.mcp.tools.files.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=0,
                    stdout='x' * (128 * 1024), stderr='')
            response = self.call_tool('inventory_query',
                                      {'action': 'list'})
        self.assertIn('truncated', self.result_text(response))


if __name__ == '__main__':
    unittest.main()
