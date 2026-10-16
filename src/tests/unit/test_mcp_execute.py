# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the playbook execution tools.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. Execution is mocked at the seams: the playbook runner, the
inventory, the hooks and the subprocess.
"""

from debops.mcp.policy import McpPolicy
from debops.mcp.server import McpServer, RequestCancelled, ToolContext
from debops.mcp.tools import register_project_tools
from debops.mcp.tools.execute import _build_ansible_args
from unittest import mock
import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
import unittest


class FakeProject(object):

    def __init__(self, path, raw_config=None):
        self.path = path
        self.name = os.path.basename(path)
        self.project_type = 'legacy'
        self.view = 'system'
        self.config = type('Config', (), {
            '_env_vars': {},
            'raw': raw_config or {},
        })()


def make_process(returncode=0, output='play recap\n', poll=None):
    process = mock.Mock()
    process.poll.side_effect = poll if poll is not None else [None, 0]
    process.communicate.return_value = (output, '')
    process.returncode = returncode
    return process


class BuildArgsTestCase(unittest.TestCase):

    def test_check_appends_check_and_diff(self):
        args = _build_ansible_args({'playbook': 'site'}, check=True)
        self.assertEqual(args, ['site', '--diff', '--check'])

    def test_run_does_not(self):
        args = _build_ansible_args({'playbook': 'site'}, check=False)
        self.assertEqual(args, ['site'])

    def test_allowed_options(self):
        args = _build_ansible_args(
                {'playbook': 'site', 'limit': 'web', 'tags': 'a,b',
                 'skip_tags': 'c'}, check=False)
        self.assertEqual(args, ['site', '--limit', 'web', '--tags', 'a,b',
                                '--skip-tags', 'c'])

    def test_rejects_control_characters(self):
        with self.assertRaises(ValueError):
            _build_ansible_args({'playbook': 'site', 'limit': 'a\nb'},
                                check=False)

    def test_requires_a_playbook(self):
        with self.assertRaises(ValueError):
            _build_ansible_args({}, check=False)


class ExecuteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-exec-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = FakeProject(self.tmpdir)
        self.policy = McpPolicy(self.project)
        self.context = ToolContext(1, threading.Event())

    def call_execute(self, check=True):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner') as runner, \
                mock.patch(
                'debops.mcp.tools.execute.AnsibleInventory') as inventory, \
                mock.patch('debops.mcp.tools.execute.run_hooks') as hooks, \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            runner.return_value.command.return_value = [
                    'ansible-playbook', 'site.yml']
            inventory.return_value.unlock.return_value = True
            hooks.return_value = True
            popen.return_value = make_process()
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=check)
        return result, inventory, hooks, popen

    def test_successful_check(self):
        result, inventory, hooks, popen = self.call_execute()
        self.assertFalse(result['isError'])
        text = result['content'][0]['text']
        self.assertIn('exit code: 0', text)
        self.assertIn('play recap', text)
        # The command is spawned without a shell, as a plain argv list
        popen.assert_called_once()
        command = popen.call_args[0][0]
        self.assertEqual(command, ['ansible-playbook', 'site.yml'])
        self.assertEqual(popen.call_args[1]['cwd'], self.tmpdir)
        # Secrets are unlocked before and locked after
        inventory.return_value.unlock.assert_called_once()
        inventory.return_value.lock.assert_called_once()
        # Hooks run with the mode-specific names
        hook_names = [call[0][1] for call in hooks.call_args_list]
        self.assertEqual(hook_names, ['pre-check', 'post-check'])

    def test_failing_run_reports_the_exit_code(self):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=2,
                                              output='failed!\n',
                                              poll=[0])
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=False)
        self.assertTrue(result['isError'])
        self.assertIn('exit code: 2', result['content'][0]['text'])
        self.assertIn('failed!', result['content'][0]['text'])

    def test_runner_abort_stays_off_the_protocol_channel(self):
        """A runner which prints and exits becomes an error result."""
        from debops.mcp.tools.execute import _execute_playbook

        def aborting_runner(*args, **kwargs):
            print('Error: No inventory specified in the "ansible.cfg" '
                  'configuration file.')
            raise SystemExit(1)

        real_stdout = io.StringIO()
        with mock.patch('debops.mcp.tools.execute.AnsiblePlaybookRunner',
                        side_effect=aborting_runner), \
                contextlib.redirect_stdout(real_stdout):
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True)
        self.assertTrue(result['isError'])
        self.assertIn('No inventory specified',
                      result['content'][0]['text'])
        # Nothing reached the real stdout
        self.assertEqual(real_stdout.getvalue(), '')

    def test_hook_abort_prevents_execution(self):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=False), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True)
        self.assertTrue(result['isError'])
        self.assertIn('hook aborted', result['content'][0]['text'])
        popen.assert_not_called()

    def test_cancellation_terminates_the_process(self):
        from debops.mcp.tools.execute import _execute_playbook
        self.context.cancelled.set()
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-15,
                                              poll=[None, None])
            with self.assertRaises(RequestCancelled):
                _execute_playbook(self.project, self.policy,
                                  {'playbook': 'site'}, self.context,
                                  check=True)
        popen.return_value.terminate.assert_called_once()

    def test_timeout_kills_the_process(self):
        from debops.mcp.tools.execute import _execute_playbook
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {'execute_timeout': 0}}})
        policy = McpPolicy(project)
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-9,
                                              poll=[None, None])
            result = _execute_playbook(project, policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True)
        popen.return_value.kill.assert_called_once()
        self.assertTrue(result['isError'])
        self.assertIn('timed out', result['content'][0]['text'])


class ExecutionGateTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-gate-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def registered_tools(self, mcp_config):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': mcp_config}})
        server = McpServer(project=project, policy=McpPolicy(project))
        register_project_tools(
                server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        return server.tools.keys()

    def test_check_is_registered_by_default(self):
        names = self.registered_tools({})
        self.assertIn('check', names)
        self.assertNotIn('run', names)

    def test_none_registers_nothing(self):
        names = self.registered_tools({'execute': 'none'})
        self.assertNotIn('check', names)
        self.assertNotIn('run', names)

    def test_all_registers_both(self):
        names = self.registered_tools({'execute': 'all'})
        self.assertIn('check', names)
        self.assertIn('run', names)

    def test_unknown_mode_falls_back_to_check(self):
        names = self.registered_tools({'execute': 'everything'})
        self.assertIn('check', names)
        self.assertNotIn('run', names)

    def test_deny_list_still_applies(self):
        names = self.registered_tools({'execute': 'all',
                                       'deny': ['run']})
        self.assertIn('check', names)
        self.assertNotIn('run', names)

    # -- execute_options passthrough policy --------------------------------

    def test_execute_options_filter_the_schema(self):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {
                                  'execute_options': ['tags']}}})
        server = McpServer(project=project, policy=McpPolicy(project))
        register_project_tools(
                server, roles_path=self.tmpdir,
                playbooks_path=self.tmpdir,
                collection_index_provider=lambda: {})
        properties = server.tools['check'].input_schema['properties']
        self.assertEqual(sorted(properties), ['playbook', 'tags'])

    def test_execute_options_reject_disabled_options(self):
        from debops.mcp.tools.execute import _build_ansible_args
        with self.assertRaises(ValueError) as context:
            _build_ansible_args({'playbook': 'site', 'limit': 'web'},
                                check=True, allowed_options=['tags'])
        self.assertIn('disabled by the project policy',
                      str(context.exception))

    def test_execute_options_accept_flag_spellings(self):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {
                                  'execute_options': ['-l', '--tags']}}})
        policy = McpPolicy(project)
        self.assertEqual(policy.execute_options, ['limit', 'tags'])

    def test_execute_options_unknown_entries_ignored(self):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {
                                  'execute_options': ['--check', 'tags']}}})
        policy = McpPolicy(project)
        self.assertEqual(policy.execute_options, ['tags'])


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
