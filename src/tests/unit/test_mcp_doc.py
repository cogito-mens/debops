# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the ansible.doc tool."""

import os
import shutil
import subprocess
import tempfile
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
        # The documentation mode resolves the account's own
        # ansible-doc to an absolute path when one exists
        self.assertTrue(command[0].endswith('ansible-doc'), command[0])
        self.assertEqual(command[1], 'ansible.builtin.service')
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

    def test_docs_mode_serves_the_account_collections_by_default(self):
        # No operator-chosen collections path: the account's default
        # roots are served next to the bundled collection, so the
        # tool answers for the content the account actually has
        without = {key: value for key, value in os.environ.items()
                   if key != 'ANSIBLE_COLLECTIONS_PATH'}
        with mock.patch.dict(os.environ, without, clear=True):
            _result, run = self.run_doc({'plugin': 'x'})
        env = run.call_args[1]['env']
        roots = env['ANSIBLE_COLLECTIONS_PATH'].split(os.pathsep)
        self.assertEqual(roots[0], BUNDLED_COLLECTIONS_ROOT)
        self.assertIn(os.path.expanduser('~/.ansible/collections'),
                      roots)
        self.assertIn('/usr/share/ansible/collections', roots)

    def test_docs_mode_inherits_an_operator_chosen_path(self):
        # The variable, when defined, rules the lookup alone
        with mock.patch.dict(
                os.environ,
                {'ANSIBLE_COLLECTIONS_PATH': '/srv/collections'},
                clear=True):
            _result, run = self.run_doc({'plugin': 'x'})
        env = run.call_args[1]['env']
        self.assertEqual(env['ANSIBLE_COLLECTIONS_PATH'],
                         '/srv/collections')

    def test_docs_mode_prefers_the_accounts_ansible_doc(self):
        # The pipx or pip --user install wins; the harness which
        # spawned the server may carry a PATH without it
        from debops.mcp.tools import doc
        tmpdir = tempfile.mkdtemp(prefix='debops-mcp-doc-')
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        user_bin = os.path.join(tmpdir, '.local', 'bin')
        os.makedirs(user_bin)
        binary = os.path.join(user_bin, 'ansible-doc')
        with open(binary, 'w') as fp:
            fp.write('#!/bin/sh\n')
        os.chmod(binary, 0o755)
        with mock.patch.dict(os.environ, {'HOME': tmpdir}):
            resolved = doc._account_ansible_doc()
        self.assertEqual(resolved, binary)

    def test_docs_mode_falls_back_to_the_path(self):
        from debops.mcp.tools import doc
        with mock.patch.object(doc, 'ACCOUNT_ANSIBLE_DOC',
                               ('/nonexistent/ansible-doc',)):
            with mock.patch('debops.mcp.tools.doc.shutil.which',
                            return_value='/x/bin/ansible-doc'):
                self.assertEqual(doc._account_ansible_doc(),
                                 '/x/bin/ansible-doc')
            with mock.patch('debops.mcp.tools.doc.shutil.which',
                            return_value=None):
                self.assertEqual(doc._account_ansible_doc(),
                                 'ansible-doc')

    def test_docs_mode_runs_the_resolved_binary(self):
        with mock.patch('debops.mcp.tools.doc._account_ansible_doc',
                        return_value='/home/x/.local/bin/ansible-doc'):
            _result, run = self.run_doc({'plugin': 'x'})
        self.assertEqual(run.call_args[0][0][0],
                         '/home/x/.local/bin/ansible-doc')

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
