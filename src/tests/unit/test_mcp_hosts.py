# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the host query tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The ansible subprocess is mocked.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, RequestCancelled, ToolContext
from debops.mcp.tools import register_project_tools
from unittest import mock
import io
import os
import shutil
import tempfile
import threading
import unittest


PING_OK = 'web1.example.com | SUCCESS => {"ping": "pong"}\n'

FACTS_OK = ('web1.example.com | SUCCESS => {"ansible_distribution": '
            '"Debian", "ansible_distribution_version": "12"}\n')


def make_process(returncode=0, output='', poll=None):
    process = mock.Mock()
    process.poll.side_effect = poll if poll is not None else [None, 0]
    process.stdout = io.StringIO(output)
    process.wait.return_value = None
    process.returncode = returncode
    return process


class HostsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-hosts-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'config': type('Config', (), {'_env_vars': {},
                                          'raw': {}})(),
        })()
        self.policy = McpPolicy(self.project)
        self.server = McpServer(project=self.project, policy=self.policy)
        register_project_tools(
                self.server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        self.context = ToolContext(1, threading.Event())

    def call_tool(self, name, arguments, request_id=1):
        return self.server.handle({
            'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}})

    def _popen(self, **kwargs):
        return mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen', **kwargs)

    def test_ping(self):
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(output=PING_OK)
            response = self.call_tool('exec_ping', {'hosts': 'web1'})
        command = popen.call_args[0][0]
        self.assertEqual(command[:4],
                         ['ansible', 'web1', '-m', 'ansible.builtin.ping'])
        self.assertNotIn('--become', command)
        self.assertIn('pong', response['result']['content'][0]['text'])

    def test_become_flag(self):
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(output=PING_OK)
            self.call_tool('exec_ping',
                           {'hosts': 'web1', 'become': True})
        command = popen.call_args[0][0]
        self.assertIn('--become', command)

    def test_facts_are_rendered_as_yaml(self):
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(output=FACTS_OK)
            response = self.call_tool('exec_facts', {'hosts': 'web1'})
        command = popen.call_args[0][0]
        self.assertEqual(command[:4],
                         ['ansible', 'web1', '-m', 'ansible.builtin.setup'])
        text = response['result']['content'][0]['text']
        self.assertIn('## web1.example.com', text)
        self.assertIn('ansible_distribution: Debian', text)

    def test_facts_filter(self):
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(output=FACTS_OK)
            self.call_tool('exec_facts', {'hosts': 'web1',
                                          'filter': 'ansible_local*'})
        command = popen.call_args[0][0]
        self.assertEqual(command[-2:], ['-a', 'filter=ansible_local*'])

    def test_invalid_host_pattern(self):
        response = self.call_tool('exec_ping', {'hosts': 'a b'})
        self.assertTrue(response['result']['isError'])
        response = self.call_tool('exec_ping', {'hosts': ''})
        self.assertTrue(response['result']['isError'])

    def test_invalid_facts_filter(self):
        response = self.call_tool('exec_facts',
                                  {'hosts': 'web1',
                                   'filter': 'a b; rm -rf /'})
        self.assertTrue(response['result']['isError'])

    def test_non_boolean_become_rejected(self):
        response = self.call_tool('exec_ping',
                                  {'hosts': 'web1', 'become': 'yes'})
        self.assertTrue(response['result']['isError'])

    def test_unreachable_host_is_an_error_result(self):
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(
                    returncode=4,
                    output='web1 | UNREACHABLE! => {...}\n')
            response = self.call_tool('exec_ping', {'hosts': 'web1'})
        self.assertTrue(response['result']['isError'])
        self.assertIn('UNREACHABLE',
                      response['result']['content'][0]['text'])

    def test_cancellation(self):
        from debops.mcp.tools.hosts import tool_exec_ping
        self.context.cancelled.set()
        with self._popen() as popen, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(returncode=-15,
                                              poll=[None, None])
            with self.assertRaises(RequestCancelled):
                tool_exec_ping(self.project, self.policy,
                               {'hosts': 'web1'}, self.context)
        popen.return_value.terminate.assert_called_once()

    def test_registered_when_execution_allowed(self):
        self.assertIn('exec_ping', self.server.tools)
        self.assertIn('exec_facts', self.server.tools)

    def test_not_registered_when_execute_none(self):
        self.project.config.raw['project'] = {'mcp': {'execute': 'none'}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        self.assertNotIn('exec_ping', server.tools)
        self.assertNotIn('exec_facts', server.tools)

    def test_deny_list_removes_them(self):
        self.project.config.raw['project'] = {'mcp': {'deny':
                                                      ['exec_ping']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        self.assertNotIn('exec_ping', server.tools)
        self.assertIn('exec_facts', server.tools)


if __name__ == '__main__':
    unittest.main()
