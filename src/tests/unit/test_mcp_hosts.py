# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the host query tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The ansible subprocesses are mocked.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, PROTOCOL_VERSION, RequestCancelled, ToolContext
from debops.mcp.tools import register_project_tools
from unittest import mock
import io
import json
import os
import shutil
import tempfile
import threading
import unittest


PING_OK = 'web1.example.com | SUCCESS => {"ping": "pong"}\n'

# The real setup result wraps the facts in the 'ansible_facts'
# envelope key
FACTS_OK = ('web1.example.com | SUCCESS => {"ansible_facts": '
            '{"ansible_distribution": "Debian", '
            '"ansible_distribution_version": "12"}, "changed": false}\n')


def make_process(returncode=0, output='', poll=None):
    process = mock.Mock()
    process.poll.side_effect = poll if poll is not None else [None, 0]
    process.stdout = io.StringIO(output)
    process.wait.return_value = None
    process.returncode = returncode
    return process


def _auto_commit_noop(self, message=None):
    pass


# The per-request '_meta' fields the specification requires
REQUEST_META = {
    'io.modelcontextprotocol/protocolVersion': PROTOCOL_VERSION,
    'io.modelcontextprotocol/clientCapabilities': {},
}


class HostsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-hosts-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir,
            'name': 'project',
            'project_type': 'legacy',
            'view': 'system',
            'auto_commit': _auto_commit_noop,
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
            'jsonrpc': '2.0',
            'id': request_id, 'method': 'tools/call',
            'params': {'_meta': REQUEST_META,
                       'name': name, 'arguments': arguments}})

    def _run(self, name, arguments, module_output=PING_OK, module_rc=0,
             resolved=('web1',), poll=None):
        """Call a tool with both ansible subprocesses mocked."""
        list_result = mock.Mock()
        list_result.returncode = 0
        list_result.stdout = ('  hosts ({}):\n'.format(len(resolved))
                              + ''.join('    {}\n'.format(host)
                                        for host in resolved))
        list_result.stderr = ''
        with mock.patch('debops.mcp.tools.execute.subprocess.Popen') \
                as popen, \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=list_result), \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            popen.return_value = make_process(returncode=module_rc,
                                              output=module_output,
                                              poll=poll)
            return popen, self.call_tool(name, arguments)

    def test_ping(self):
        popen, response = self._run('ansible_ping', {'hosts': 'web1'})
        command = popen.call_args[0][0]
        self.assertEqual(command[:4],
                         ['ansible', 'web1', '-m', 'ansible.builtin.ping'])
        self.assertNotIn('--become', command)
        text = response['result']['content'][0]['text']
        self.assertIn('pong', text)
        self.assertIn('Hosts: web1', text)

    def test_ping_structured(self):
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        _popen, response = self._run('ansible_ping',
                                     {'hosts': 'web1'})
        structured = response['result']['structuredContent']
        self.assertEqual(structured, {'web1.example.com': 'pong'})
        self.assertEqual(check_schema(structured,
                                      TOOL_OUTPUT_SCHEMAS
                                      ['ansible_ping']), [])

    def test_become_flag(self):
        popen, _response = self._run('ansible_ping',
                                     {'hosts': 'web1', 'become': True})
        self.assertIn('--become', popen.call_args[0][0])

    def test_facts_are_rendered_as_yaml(self):
        _popen, response = self._run('ansible_facts', {'hosts': 'web1'},
                                     module_output=FACTS_OK)
        text = response['result']['content'][0]['text']
        self.assertIn('## web1.example.com', text)
        self.assertIn('ansible_distribution: Debian', text)

    def test_facts_filter(self):
        popen, _response = self._run('ansible_facts',
                                     {'hosts': 'web1',
                                      'filter': 'ansible_local*'})
        self.assertEqual(popen.call_args[0][0][-2:],
                         ['-a', 'filter=ansible_local*'])

    def test_facts_dotted_filter_narrows_into_the_branch(self):
        # Ansible's own filter stops at the top level; the dotted path
        # is walked server-side
        local_facts = ('web1.example.com | SUCCESS => {"ansible_facts": '
                       '{"ansible_local": '
                       '{"postfix": {"installed": true}, '
                       '"telegraf": {"installed": false}, '
                       '"nginx": {"installed": true}}}, '
                       '"changed": false}\n')
        popen, response = self._run(
                'ansible_facts',
                {'hosts': 'web1', 'filter': 'ansible_local.post*'},
                module_output=local_facts)
        # Ansible gets the top-level prefix; the branch narrows after
        self.assertEqual(popen.call_args[0][0][-2:],
                         ['-a', 'filter=ansible_local'])
        text = response['result']['content'][0]['text']
        self.assertIn('postfix', text)
        self.assertNotIn('telegraf', text)
        self.assertNotIn('nginx', text)

    def test_facts_dotted_filter_without_a_match_is_an_empty_branch(
            self):
        _popen, response = self._run(
                'ansible_facts',
                {'hosts': 'web1', 'filter': 'ansible_local.nosuch*'},
                module_output=FACTS_OK)
        text = response['result']['content'][0]['text']
        self.assertFalse(response['result']['isError'])
        # The host is present with an empty branch, not silently absent
        self.assertIn('web1.example.com', text)
        self.assertIn('{}', text)

    def test_facts_format_json(self):
        _popen, response = self._run(
                'ansible_facts',
                {'hosts': 'web1', 'format': 'json'},
                module_output=FACTS_OK)
        document = json.loads(response['result']['content'][0]['text'])
        self.assertEqual(document,
                         {'web1.example.com': {
                                 'ansible_distribution': 'Debian',
                                 'ansible_distribution_version': '12'}})

    def test_facts_invalid_format_rejected(self):
        response = self.call_tool('ansible_facts',
                                  {'hosts': 'web1', 'format': 'yaml'})
        self.assertTrue(response['result']['isError'])

    def test_invalid_host_pattern(self):
        response = self.call_tool('ansible_ping', {'hosts': 'a b'})
        self.assertTrue(response['result']['isError'])
        response = self.call_tool('ansible_ping', {'hosts': ''})
        self.assertTrue(response['result']['isError'])

    def test_invalid_facts_filter(self):
        response = self.call_tool('ansible_facts',
                                  {'hosts': 'web1',
                                   'filter': 'a b; rm -rf /'})
        self.assertTrue(response['result']['isError'])

    def test_non_boolean_become_rejected(self):
        response = self.call_tool('ansible_ping',
                                  {'hosts': 'web1', 'become': 'yes'})
        self.assertTrue(response['result']['isError'])

    def test_unreachable_host_is_an_error_result(self):
        _popen, response = self._run(
                'ansible_ping', {'hosts': 'web1'}, module_rc=4,
                module_output='web1 | UNREACHABLE! => {...}\n')
        self.assertTrue(response['result']['isError'])
        self.assertIn('UNREACHABLE',
                      response['result']['content'][0]['text'])

    def test_pattern_matching_nothing_is_an_error(self):
        _popen, response = self._run('ansible_ping', {'hosts': 'nosuch'},
                                     resolved=())
        self.assertTrue(response['result']['isError'])
        self.assertIn('matches no hosts',
                      response['result']['content'][0]['text'])

    def test_ansible_hosts_allowlist(self):
        self.project.config.raw['project'] = {'mcp': {
                'ansible_hosts': ['db*']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        list_result = mock.Mock()
        list_result.returncode = 0
        list_result.stdout = '  hosts (1):\n    web1\n'
        list_result.stderr = ''
        with mock.patch('debops.mcp.tools.execute.subprocess.run',
                        return_value=list_result), \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            response = server.handle({
                'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'_meta': REQUEST_META, 'name': 'ansible_ping',
                           'arguments': {'hosts': 'web1'}}})
        self.assertTrue(response['result']['isError'])
        self.assertIn('ansible_hosts',
                      response['result']['content'][0]['text'])

    def test_cancellation(self):
        from debops.mcp.tools.hosts import tool_ansible_ping
        self.context.cancelled.set()
        with mock.patch('debops.mcp.tools.execute.subprocess.Popen') \
                as popen, \
                mock.patch('debops.mcp.tools.execute.subprocess.run') \
                as resolve, \
                mock.patch('debops.mcp.tools.hosts.AnsibleInventory'):
            resolve.return_value = mock.Mock(
                    returncode=0, stdout='  hosts (1):\n    web1\n',
                    stderr='')
            popen.return_value = make_process(returncode=-15,
                                              poll=[None, None])
            with self.assertRaises(RequestCancelled):
                tool_ansible_ping(self.project, self.policy,
                                  {'hosts': 'web1'}, self.context)
        popen.return_value.terminate.assert_called_once()

    def test_registered_when_execution_allowed(self):
        self.assertIn('ansible_ping', self.server.tools)
        self.assertIn('ansible_facts', self.server.tools)

    def test_not_registered_when_execute_none(self):
        self.project.config.raw['project'] = {'mcp': {'execute': 'none'}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        self.assertNotIn('ansible_ping', server.tools)
        self.assertNotIn('ansible_facts', server.tools)

    def test_deny_list_removes_them(self):
        self.project.config.raw['project'] = {'mcp': {'deny':
                                                      ['ansible_ping']}}
        server = McpServer(project=self.project,
                           policy=McpPolicy(self.project))
        register_project_tools(server, roles_path=self.tmpdir,
                               playbooks_path=self.tmpdir,
                               collection_index_provider=lambda: {})
        self.assertNotIn('ansible_ping', server.tools)
        self.assertIn('ansible_facts', server.tools)


if __name__ == '__main__':
    unittest.main()
