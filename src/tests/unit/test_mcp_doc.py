# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the ansible.doc tool."""

import os
import subprocess
import unittest

from unittest import mock

from debops.mcp.tools.collections import BUNDLED_COLLECTIONS_ROOT
from debops.mcp.tools.doc import tool_ansible_doc

DOC_TEXT = '> ANSIBLE.BUILTIN.SERVICE\n\nManage services.\n'


class AnsibleDocTestCase(unittest.TestCase):

    def call(self, arguments, project=None, policy=None,
             roles_paths=None):
        return tool_ansible_doc(project, policy, arguments,
                                roles_paths=roles_paths)

    def run_doc(self, arguments, stdout=DOC_TEXT, returncode=0,
                stderr='', **kwargs):
        with mock.patch('debops.mcp.tools.doc.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess(
                    args=[], returncode=returncode, stdout=stdout,
                    stderr=stderr)
            result = self.call(arguments, **kwargs)
        return result, run

    def test_module_documentation(self):
        result, run = self.run_doc({'plugin': 'ansible.builtin.service'})
        self.assertFalse(result.get('isError', False))
        self.assertIn('Manage services.', result['content'][0]['text'])
        command = run.call_args[0][0]
        self.assertEqual(command[:2], ['ansible-doc',
                                       'ansible.builtin.service'])
        self.assertNotIn('--json', command)

    def test_plugin_type_and_json(self):
        result, run = self.run_doc(
                {'plugin': 'debops.debops.lists', 'type': 'lookup',
                 'format': 'json'},
                stdout='{"debops.debops.lists": {"doc": {}}}')
        self.assertFalse(result.get('isError', False))
        command = run.call_args[0][0]
        self.assertIn('--type', command)
        self.assertIn('lookup', command)
        self.assertIn('--json', command)
        self.assertIn('debops.debops.lists',
                      result['content'][0]['text'])

    def test_yaml_format_renders(self):
        result, run = self.run_doc(
                {'plugin': 'service', 'format': 'application/yaml'},
                stdout='{"service": {"doc": {"short": "Manage it"}}}')
        self.assertFalse(result.get('isError', False))
        self.assertIn('short: Manage it', result['content'][0]['text'])

    def test_list_mode(self):
        result, run = self.run_doc({'list': True, 'type': 'filter',
                                    'plugin': 'debops.debops'},
                                   stdout='debops.debops.globmatch ...\n')
        self.assertFalse(result.get('isError', False))
        command = run.call_args[0][0]
        self.assertIn('--list', command)
        self.assertIn('filter', command)
        self.assertIn('debops.debops', command)

    def test_list_mode_needs_no_plugin_name(self):
        result, _run = self.run_doc({'list': True})
        self.assertFalse(result.get('isError', False))

    def test_snippet_mode(self):
        result, run = self.run_doc({'plugin': 'ansible.builtin.user',
                                    'snippet': True},
                                   stdout='- name: ...\n')
        self.assertFalse(result.get('isError', False))
        self.assertIn('--snippet', run.call_args[0][0])

    def test_snippet_is_refused_for_filters(self):
        result = self.call({'plugin': 'x', 'type': 'filter',
                            'snippet': True})
        self.assertTrue(result['isError'])
        self.assertIn('snippets exist only',
                      result['content'][0]['text'])

    def test_entry_point_only_with_roles(self):
        result = self.call({'plugin': 'x', 'entry_point': 'main'})
        self.assertTrue(result['isError'])
        self.assertIn('"role"', result['content'][0]['text'])
        result, run = self.run_doc(
                {'plugin': 'debops.debops.nginx', 'type': 'role',
                 'entry_point': 'main'})
        self.assertFalse(result.get('isError', False))
        command = run.call_args[0][0]
        self.assertIn('--entry-point', command)

    def test_a_plugin_name_is_required(self):
        result = self.call({})
        self.assertTrue(result['isError'])
        self.assertIn('"plugin" argument is required',
                      result['content'][0]['text'])

    def test_invalid_type_and_name(self):
        result = self.call({'plugin': 'x', 'type': 'bogus'})
        self.assertTrue(result['isError'])
        self.assertIn('invalid plugin type',
                      result['content'][0]['text'])
        result = self.call({'plugin': '-Rbogus'})
        self.assertTrue(result['isError'])
        self.assertIn('invalid plugin name',
                      result['content'][0]['text'])

    def test_failure_answers_with_the_stderr_tail(self):
        result, _run = self.run_doc(
                {'plugin': 'nosuch.module'}, returncode=1,
                stdout='', stderr='Unable to find nosuch.module\n')
        self.assertTrue(result['isError'])
        self.assertIn('Unable to find', result['content'][0]['text'])

    def test_missing_plugin_is_an_error_despite_exit_zero(self):
        # ansible-doc exits 0 for a missing plugin; the warning rides
        # stderr and stdout stays empty
        result, _run = self.run_doc({'plugin': 'nosuch.module'},
                                    stdout='',
                                    stderr='[WARNING]: nosuch.module '
                                           'was not found\n')
        self.assertTrue(result['isError'])
        self.assertIn('no documentation', result['content'][0]['text'])
        # In list mode the same shape is a valid empty answer
        result, _run = self.run_doc({'list': True, 'type': 'filter',
                                     'plugin': 'nosuch'},
                                    stdout='', stderr='')
        self.assertFalse(result.get('isError', False))
        self.assertIn('no matching plugins',
                      result['content'][0]['text'])

    def test_missing_binary_and_timeout(self):
        with mock.patch('debops.mcp.tools.doc.subprocess.run',
                        side_effect=FileNotFoundError):
            result = self.call({'plugin': 'x'})
        self.assertTrue(result['isError'])
        self.assertIn('was not found', result['content'][0]['text'])
        with mock.patch('debops.mcp.tools.doc.subprocess.run',
                        side_effect=subprocess.TimeoutExpired('x', 1)):
            result = self.call({'plugin': 'x'})
        self.assertTrue(result['isError'])
        self.assertIn('timed out', result['content'][0]['text'])

    def test_garbage_json_is_a_clean_error(self):
        result, _run = self.run_doc({'plugin': 'x', 'format': 'json'},
                                    stdout='[WARNING]: nah\n')
        self.assertTrue(result['isError'])
        self.assertIn('did not return JSON',
                      result['content'][0]['text'])

    def test_roles_paths_become_search_paths(self):
        _result, run = self.run_doc(
                {'plugin': 'debops.debops.nginx', 'type': 'role'},
                roles_paths=['/srv/roles'])
        command = run.call_args[0][0]
        self.assertIn('--roles-path', command)
        self.assertIn('/srv/roles', command)

    def test_docs_mode_environment_is_hermetic(self):
        _result, run = self.run_doc({'plugin': 'x'})
        env = run.call_args[1]['env']
        # The packaged neutral config wins over any stray ansible.cfg
        # (an env path must EXIST to win; os.devnull fails Ansible's
        # extension check)
        self.assertTrue(env['ANSIBLE_CONFIG'].endswith(
                os.path.join('mcp', 'ansible.cfg')))
        self.assertTrue(os.path.isfile(env['ANSIBLE_CONFIG']))
        self.assertEqual(env['ANSIBLE_HOME'],
                         os.path.dirname(env['ANSIBLE_CONFIG']))

    def test_a_restricted_policy_scopes_the_collections(self):
        policy = type('Policy', (), {'bundled_content': True,
                                     'external_content': False})()
        _result, run = self.run_doc({'plugin': 'x'}, policy=policy)
        env = run.call_args[1]['env']
        # Only the bundled collection is visible to the lookup
        self.assertEqual(env['ANSIBLE_COLLECTIONS_PATH'],
                         BUNDLED_COLLECTIONS_ROOT)


if __name__ == '__main__':
    unittest.main()
