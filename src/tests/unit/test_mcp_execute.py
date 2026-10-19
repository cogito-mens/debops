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
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


def _auto_commit_noop(self, message=None):
    pass


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

    def auto_commit(self, message=None):
        pass


def make_process(returncode=0, output='play recap\n', poll=None):
    process = mock.Mock()
    process.poll.side_effect = poll if poll is not None else [None, 0]
    # The execution loop reads output incrementally from stdout
    process.stdout = io.StringIO(output)
    process.wait.return_value = None
    process.returncode = returncode
    return process


def make_resolve_result(hosts=('web1',), returncode=0, stderr=''):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = ('  hosts ({}):\n'.format(len(hosts))
                     + ''.join('    {}\n'.format(host) for host in hosts))
    result.stderr = stderr
    return result


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

    def test_multiple_playbooks_in_order(self):
        args = _build_ansible_args(
                {'playbook': ['service/nginx', 'site']}, check=False)
        self.assertEqual(args, ['service/nginx', 'site'])

    def test_multiple_playbooks_reject_empty_list(self):
        with self.assertRaises(ValueError):
            _build_ansible_args({'playbook': []}, check=False)

    def test_multiple_playbooks_reject_empty_names(self):
        with self.assertRaises(ValueError):
            _build_ansible_args({'playbook': ['site', '']}, check=False)

    def test_multiple_playbooks_reject_control_characters(self):
        with self.assertRaises(ValueError):
            _build_ansible_args({'playbook': ['a\nb']}, check=False)


class ResolveHostsTestCase(unittest.TestCase):

    def test_hosts_are_deduplicated_across_playbooks(self):
        # Two playbooks reporting overlapping hosts in separate blocks
        result = make_resolve_result(('web1', 'web2'))
        result.stdout += ('  hosts (2):\n    web2\n    web3\n')
        with mock.patch('debops.mcp.tools.execute.subprocess.run',
                        return_value=result):
            from debops.mcp.tools.execute import resolve_hosts
            hosts = resolve_hosts({}, '/tmp', ['ansible-playbook',
                                               'a.yml', 'b.yml'])
        self.assertEqual(hosts, ['web1', 'web2', 'web3'])


class ExecuteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-exec-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = FakeProject(self.tmpdir)
        self.policy = McpPolicy(self.project)
        self.context = ToolContext(1, threading.Event())

    def call_execute(self, check=True, resolved=('web1',),
                     playbook='site'):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner') as runner, \
                mock.patch(
                'debops.mcp.tools.execute.AnsibleInventory') as inventory, \
                mock.patch('debops.mcp.tools.execute.run_hooks') as hooks, \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result(resolved)), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            runner.return_value.command.return_value = [
                    'ansible-playbook', 'site.yml']
            inventory.return_value.unlock.return_value = True
            hooks.return_value = True
            popen.return_value = make_process()
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': playbook},
                                       self.context, check=check)
        return result, runner, inventory, hooks, popen

    def test_successful_check(self):
        result, _runner, inventory, hooks, popen = self.call_execute()
        self.assertFalse(result['isError'])
        text = result['content'][0]['text']
        self.assertIn('exit code: 0', text)
        self.assertIn('play recap', text)
        # The machine-readable summary rides alongside the text
        structured = result['structuredContent']
        self.assertEqual(structured['mode'], 'check')
        self.assertEqual(structured['playbooks'], ['site'])
        self.assertEqual(structured['hosts'], ['web1'])
        self.assertEqual(structured['exitCode'], 0)
        self.assertEqual(structured['status'], 'done')
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        self.assertEqual(check_schema(structured,
                                      TOOL_OUTPUT_SCHEMAS['check']),
                         [])
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

    def test_completion_is_logged(self):
        with self.assertLogs('debops.mcp.tools.execute',
                             level='NOTICE') as recorded:
            result, _runner, _inventory, _hooks, _popen = \
                    self.call_execute()
        self.assertFalse(result['isError'])
        self.assertTrue(any('finished' in line and 'exit code 0' in line
                            for line in recorded.output),
                        recorded.output)

    def test_cancellation_is_logged(self):
        self.context.cancelled.set()
        with self.assertLogs('debops.mcp.tools.execute',
                             level='NOTICE') as recorded:
            with self.assertRaises(RequestCancelled):
                self.call_execute()
        self.assertTrue(any('cancelled by the client' in line
                            for line in recorded.output),
                        recorded.output)

    def test_executions_are_correlated_in_the_log(self):
        with self.assertLogs('debops.mcp.tools.execute',
                             level='NOTICE') as recorded:
            self.call_execute()
            self.call_execute()
        pattern = re.compile(r'\[execution (\d+)')
        starts = [pattern.search(line).group(1)
                  for line in recorded.output
                  if 'Executing a playbook' in line]
        ends = [re.search(r'execution (\d+) (\w+)', line).groups()
                for line in recorded.output
                if line.startswith('NOTICE') and
                'Playbook execution' in line]
        # Each run has its own id, and start and end lines share it
        self.assertEqual(len(starts), 2)
        self.assertNotEqual(starts[0], starts[1])
        self.assertEqual(sorted(starts),
                         sorted(execution for execution, _ in ends))
        self.assertEqual([outcome for _, outcome in ends],
                         ['finished', 'finished'])

    def test_aborted_executions_close_the_log(self):
        with self.assertLogs('debops.mcp.tools.execute',
                             level='NOTICE') as recorded:
            # Host resolution fails: the playbook target is bad
            with mock.patch(
                    'debops.mcp.tools.execute.AnsiblePlaybookRunner') \
                    as runner, \
                    mock.patch(
                    'debops.mcp.tools.execute.AnsibleInventory'), \
                    mock.patch('debops.mcp.tools.execute.run_hooks',
                               return_value=True), \
                    mock.patch(
                    'debops.mcp.tools.execute.subprocess.run',
                    side_effect=ValueError('no such playbook')):
                from debops.mcp.tools.execute import _execute_playbook
                runner.return_value.command.return_value = [
                        'ansible-playbook', 'nosuch.yml']
                result = _execute_playbook(self.project, self.policy,
                                           {'playbook': 'nosuch'},
                                           self.context, check=True)
        self.assertTrue(result['isError'])
        self.assertTrue(any('aborted' in line
                            for line in recorded.output),
                        recorded.output)

    def test_local_playbook_classification(self):
        from debops.mcp.tools.execute import _local_playbooks
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        runner = type('Runner', (), {
            '_found_playbooks': ['ansible/playbooks/local.yml',
                                 'collections/debops/debops/playbooks/'
                                 'site.yml'],
        })()
        local = _local_playbooks(runner, self.project, self.policy)
        self.assertEqual(local, ['ansible/playbooks/local.yml'])

    def test_check_of_a_local_playbook_asks_first(self):
        # A project playbook can execute commands even in check mode
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        self.context.client_capabilities = {'elicitation': {'form': {}}}
        result, runner, _i, _h, _p = self._call_with_local_playbook()
        self.assertEqual(result['resultType'], 'input_required')
        request = result['inputRequests']['confirmation']
        self.assertIn('even in check mode',
                      request['params']['message'])
        _p.assert_not_called()

    def test_check_of_a_local_playbook_confirmed(self):
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        self.context.input_responses = {
                'confirmation': {'action': 'accept',
                                 'content': {'confirmed': True}}}
        result, _r, _i, _h, popen = self._call_with_local_playbook()
        self.assertFalse(result['isError'])
        popen.assert_called_once()

    def test_check_of_a_local_playbook_declined(self):
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        self.context.input_responses = {
                'confirmation': {'action': 'decline', 'content': {}}}
        result, _r, _i, _h, popen = self._call_with_local_playbook()
        self.assertTrue(result['isError'])
        self.assertIn('declined', result['content'][0]['text'])
        popen.assert_not_called()

    def test_check_of_a_collection_playbook_does_not_ask(self):
        # The runner resolves only a collection playbook
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        self.context.client_capabilities = {'elicitation': {'form': {}}}
        result, _r, _i, _h, popen = self.call_execute()
        self.assertFalse(result['isError'])
        popen.assert_called_once()

    def test_local_playbooks_disabled_by_policy(self):
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        self.project.config.raw['project'] = {
                'mcp': {'local_playbooks': False}}
        policy = McpPolicy(self.project)
        result, _r, _i, _h, popen = self._call_with_local_playbook(
                policy=policy)
        self.assertTrue(result['isError'])
        self.assertIn('local_playbooks', result['content'][0]['text'])
        popen.assert_not_called()

    def _call_with_local_playbook(self, policy=None):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner') as runner, \
                mock.patch(
                'debops.mcp.tools.execute.AnsibleInventory') as inventory, \
                mock.patch(
                'debops.mcp.tools.execute.run_hooks') as hooks, \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result(('web1',))), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            runner.return_value.command.return_value = [
                    'ansible-playbook', 'ansible/playbooks/probe.yml']
            runner.return_value._found_playbooks = [
                    'ansible/playbooks/probe.yml']
            inventory.return_value.unlock.return_value = True
            hooks.return_value = True
            popen.return_value = make_process()
            result = _execute_playbook(
                    self.project, policy or self.policy,
                    {'playbook': 'probe'}, self.context, check=True)
        return result, runner, inventory, hooks, popen

    def test_check_local_confirmation_via_a_task(self):
        # In a task the question is the task's outstanding input
        # request, answered with tasks/update
        from debops.mcp.tasks import Task
        from debops.mcp.tools.execute import _check_local_confirmation
        task = Task(ttl_ms=60000)
        context = ToolContext(1, threading.Event(), task=task)

        outcome = []

        def ask():
            outcome.append(_check_local_confirmation(
                    ['probe.yml'], context, None))

        thread = threading.Thread(target=ask)
        thread.start()
        deadline = time.monotonic() + 5
        while task.status != 'input_required':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertIn('confirmation', task.describe()['inputRequests'])
        task.answer('confirmation', {'action': 'accept',
                                     'content': {'confirmed': True}})
        thread.join(timeout=5)
        self.assertEqual(outcome, [None])
        self.assertEqual(task.status, 'working')

    def test_log_lines_identify_the_client(self):
        self.context = ToolContext(9, threading.Event(), peer_pid=4321)
        with self.assertLogs('debops.mcp.tools.execute',
                             level='NOTICE') as recorded:
            self.call_execute()
        start = [line for line in recorded.output
                 if 'Executing a playbook' in line][0]
        self.assertIn('request 9', start)
        self.assertIn('client pid 4321', start)

    def test_multiple_playbooks_reach_the_runner_in_order(self):
        result, runner, _inventory, _hooks, _popen = self.call_execute(
                playbook=['a.yml', 'b.yml'])
        self.assertFalse(result['isError'])
        # Both playbooks, in specification order, before the options
        runner.assert_called_once_with(
                self.project,
                ansible_args=['a.yml', 'b.yml', '--diff', '--check'])
        text = result['content'][0]['text']
        self.assertIn('# debops check a.yml b.yml', text)

    def test_failing_run_reports_the_exit_code(self):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
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

    def test_unresolvable_limit_is_an_error(self):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result(
                               (), returncode=1,
                               stderr='... leaves us with no hosts to '
                               'target.')), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site',
                                        'limit': 'nosuch'}, self.context,
                                       check=True)
        self.assertTrue(result['isError'])
        self.assertIn('no hosts to target', result['content'][0]['text'])
        popen.assert_not_called()

    def test_execute_hosts_allowlist(self):
        from debops.mcp.tools.execute import _execute_playbook
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {'execute_hosts':
                                                   ['db*']}}})
        policy = McpPolicy(project)
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            result = _execute_playbook(project, policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True)
        self.assertTrue(result['isError'])
        self.assertIn('execute_hosts', result['content'][0]['text'])
        popen.assert_not_called()

    def test_hosts_in_the_result_header(self):
        result, _runner, _inventory, _hooks, _popen = self.call_execute()
        self.assertIn('hosts: web1', result['content'][0]['text'])

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

    def test_kill_switch_terminates_mid_run(self):
        from debops.mcp.tools.execute import _execute_playbook
        on_disabled = mock.Mock()
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-15,
                                              poll=[None, None])
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True,
                                       enabled_check=lambda: False,
                                       on_disabled=on_disabled)
        popen.return_value.terminate.assert_called_once()
        on_disabled.assert_called_once()
        self.assertTrue(result['isError'])
        self.assertIn('kill switch', result['content'][0]['text'])

    def test_hook_abort_prevents_execution(self):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=False), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
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
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
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
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
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
                                  'execute_options': ['-l', '--tags',
                                                      '--skip-tags']}}})
        policy = McpPolicy(project)
        self.assertEqual(policy.execute_options, ['limit', 'tags',
                                                  'skip_tags'])

    def test_execute_options_deduplicate_spellings(self):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {
                                  'execute_options': ['skip_tags',
                                                      'skip-tags',
                                                      '--skip_tags']}}})
        policy = McpPolicy(project)
        self.assertEqual(policy.execute_options, ['skip_tags'])

    def test_execute_options_unknown_entries_ignored(self):
        project = FakeProject(self.tmpdir,
                              {'project': {'mcp': {
                                  'execute_options': ['--check', 'tags']}}})
        policy = McpPolicy(project)
        self.assertEqual(policy.execute_options, ['tags'])


PROGRESS_OUTPUT = ('PLAY [Test play] '
                   '**************************************************\n'
                   '\nTASK [Say hello] '
                   '********************************************\n'
                   'ok: [localhost]\n'
                   '\nPLAY RECAP '
                   '**********************************************************\n'
                   'localhost : ok=1 changed=0\n')


class ProgressTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-progress-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = FakeProject(self.tmpdir)
        self.policy = McpPolicy(self.project)

    def run_with_context(self, context, output=PROGRESS_OUTPUT):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner'), \
                mock.patch('debops.mcp.tools.execute.AnsibleInventory'), \
                mock.patch('debops.mcp.tools.execute.run_hooks',
                           return_value=True), \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result()), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(output=output)
            return _execute_playbook(self.project, self.policy,
                                     {'playbook': 'site'}, context,
                                     check=True)

    def test_progress_reported_with_a_token(self):
        sent = []
        context = ToolContext(1, threading.Event(), progress_token='pt-1',
                              notify=sent.append)
        self.run_with_context(context)
        notifications = [message for message in sent
                         if message.get('method')
                         == 'notifications/progress']
        self.assertTrue(notifications)
        params = notifications[0]['params']
        self.assertEqual(params['progressToken'], 'pt-1')
        self.assertIn('PLAY [Test play]', params['message'])

    def test_no_progress_without_a_token(self):
        sent = []
        context = ToolContext(1, threading.Event(), notify=sent.append)
        self.run_with_context(context)
        self.assertEqual(sent, [])

    def test_progress_is_rate_limited(self):
        sent = []
        context = ToolContext(1, threading.Event(), progress_token='pt-1',
                              notify=sent.append)
        output = '\n'.join('TASK [task {}]'.format(index)
                           for index in range(20)) + '\n'
        self.run_with_context(context, output=output)
        self.assertEqual(len(sent), 1)

    def test_output_is_tail_capped(self):
        context = ToolContext(1, threading.Event())
        result = self.run_with_context(context,
                                       output='x' * 200000 + '\n')
        text = result['content'][0]['text']
        self.assertIn('earlier output truncated', text)
        self.assertLess(len(text), 150000)


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
