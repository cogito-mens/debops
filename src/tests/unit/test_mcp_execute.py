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
import itertools
import os
import re
import shutil
import signal
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
    if poll is None:
        # The default tolerates concurrent callers: the detached log
        # reader polls too, so a side-effect list would be exhausted by
        # the two consumers racing
        calls = itertools.count()

        def poll():
            return 0 if next(calls) >= 2 else None
    process.poll.side_effect = poll
    # The execution loop reads output incrementally from stdout
    process.stdout = io.StringIO(output)
    process.wait.return_value = None
    process.returncode = returncode
    return process


def make_popen(output='play recap\n', returncode=0, poll=None):
    """A Popen side effect whose 'child' writes the output into the
    given stdout: the log file for a detached run, the pipe otherwise
    (the pipe reader consumes the mock's own 'stdout' stream)."""
    def popen(command, **kwargs):
        stdout = kwargs.get('stdout')
        process = make_process(returncode=returncode, output=output,
                               poll=poll)
        if stdout is not None and stdout is not subprocess.PIPE:
            stdout.write(output)
            stdout.flush()
        return process
    return popen


def make_resolve_result(hosts=('web1',), returncode=0, stderr=''):
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = ('  hosts ({}):\n'.format(len(hosts))
                     + ''.join('    {}\n'.format(host) for host in hosts))
    result.stderr = stderr
    return result


class BuildArgsTestCase(unittest.TestCase):

    def test_check_appends_check_only_by_default(self):
        # The diff is gated on the project policy: it renders the files
        # a template task would write, secrets included
        args = _build_ansible_args({'playbook': 'site'}, check=True)
        self.assertEqual(args, ['site', '--check'])

    def test_check_appends_diff_when_the_policy_opts_in(self):
        args = _build_ansible_args({'playbook': 'site'}, check=True,
                                   diff=True)
        self.assertEqual(args, ['site', '--diff', '--check'])

    def test_targets_reject_options(self):
        # A leading dash would make the target an ansible-playbook
        # option downstream, with the next target as its value
        for target in ('-c', '--playbook-dir=resources', '-e', '--'):
            with self.assertRaises(ValueError, msg=target):
                _build_ansible_args({'playbook': target}, check=True)
        with self.assertRaises(ValueError):
            _build_ansible_args({'playbook': ['-c', 'local']},
                                check=True)

    def test_targets_reject_paths_outside_the_search_space(self):
        for target in ('/etc/cron.daily/x', '../outside.yml',
                       'playbooks/../../x.yml', 'a//../b.yml'):
            with self.assertRaises(ValueError, msg=target):
                _build_ansible_args({'playbook': target}, check=True)

    def test_valid_targets_pass(self):
        args = _build_ansible_args(
                {'playbook': ['site', 'service/postfix',
                              'playbooks/custom.yml']}, check=False)
        self.assertEqual(args, ['site', 'service/postfix',
                                'playbooks/custom.yml'])

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


FAILING_OUTPUT = '''\
PLAY [web] *************************************************************

TASK [Gather facts] ****************************************************
ok: [web1]

TASK [Install the package] *********************************************

fatal: [web1]: FAILED! => {"changed": false, "msg": "No package matching 'apache3'"}

PLAY RECAP *************************************************************
web1 : ok=1 changed=0 failed=1
'''


class ExecuteTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-exec-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        # The run logs live in the cache directory; keep them in the
        # test's temporary tree
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')
        self.project = FakeProject(self.tmpdir)
        self.policy = McpPolicy(self.project)
        self.context = ToolContext(1, threading.Event())

    def run_logs(self):
        """The run log files the test's executions wrote."""
        import glob
        return glob.glob(os.path.join(
                os.environ['XDG_CACHE_HOME'], 'debops', 'mcp', 'runs',
                '*', 'execution-*.log'))

    def call_execute(self, check=True, resolved=('web1',),
                     playbook='site', output='play recap\n',
                     returncode=0, arguments=None):
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
            popen.side_effect = make_popen(output=output,
                                           returncode=returncode)
            result = _execute_playbook(self.project, self.policy,
                                       dict({'playbook': playbook},
                                            **(arguments or {})),
                                       self.context, check=check)
        return result, runner, inventory, hooks, popen

    def test_debug_refused_by_default(self):
        # The leak class: the verbosity prints task arguments — secrets
        # included — into the answer, so the policy gates it
        result, _r, _i, _h, _p = self.call_execute(
                arguments={'debug': True})
        self.assertTrue(result['isError'])
        self.assertIn('execute_debug', result['content'][0]['text'])

    def test_debug_run_payload(self):
        self.policy.execute_debug = True
        result, runner, _i, _h, popen = self.call_execute(
                arguments={'debug': True})
        self.assertFalse(result['isError'])
        # -vv rides the runner's ansible arguments: the invocation and
        # the result pair per task, without the connection chatter
        self.assertIn('-vv', runner.call_args[1]['ansible_args'])
        env = popen.call_args[1]['env']
        self.assertEqual(env['ANSIBLE_DEBUG'], 'true')
        self.assertEqual(env['ANSIBLE_KEEP_REMOTE_FILES'], '1')
        self.assertIn('debug mode', result['content'][0]['text'])

    def test_debug_failure_answer_carries_the_neighborhood(self):
        self.policy.execute_debug = True
        result, _r, _i, _h, _p = self.call_execute(
                arguments={'debug': True}, returncode=2,
                output=FAILING_OUTPUT)
        text = result['content'][0]['text']
        self.assertIn('## First failure', text)
        self.assertIn('TASK [Install the package]', text)
        self.assertIn('apache3', text)
        # The full output still follows
        self.assertIn('PLAY RECAP', text)

    def test_failure_excerpt(self):
        from debops.mcp.tools.execute import _failure_excerpt
        excerpt = _failure_excerpt(FAILING_OUTPUT)
        self.assertIn('TASK [Install the package]', excerpt)
        self.assertIn('fatal: [web1]: FAILED!', excerpt)
        self.assertIn('apache3', excerpt)
        self.assertIsNone(_failure_excerpt('all quiet\nok: [web1]\n'))

    def test_schema_hides_debug_until_the_policy_allows_it(self):
        from debops.mcp.tools import execution_schema
        schema = execution_schema(self.policy)
        self.assertNotIn('debug', schema['properties'])
        self.policy.execute_debug = True
        schema = execution_schema(self.policy)
        self.assertIn('debug', schema['properties'])
        self.assertEqual(schema['properties']['debug']['type'],
                         'boolean')

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
        # The command runs inside the fixed wrapper, the playbook argv
        # quoted into it
        popen.assert_called_once()
        command = popen.call_args[0][0]
        self.assertEqual(command[:2], ['/bin/sh', '-c'])
        self.assertIn('ansible-playbook site.yml', command[2])
        self.assertEqual(popen.call_args[1]['cwd'], self.tmpdir)
        # Secrets are unlocked before and locked after
        inventory.return_value.unlock.assert_called_once()
        inventory.return_value.lock.assert_called_once()
        # Hooks run with the mode-specific names
        hook_names = [call[0][1] for call in hooks.call_args_list]
        self.assertEqual(hook_names, ['pre-check', 'post-check'])

    def test_recap_in_the_summary(self):
        # Two plays, one host each contributing counters: the recap
        # aggregates per host, and it is the recap — not the exit code
        # — which answers whether anything changed
        output = (
                'PLAY [all] '
                '********************************************************\n'
                'TASK [Gather] '
                '****************************************************\n'
                'ok: [web1]\n'
                'PLAY RECAP '
                '********************************************************\n'
                'web1 : ok=12 changed=3 unreachable=0 failed=0 '
                'skipped=1 rescued=0 ignored=0\n'
                '\n'
                'PLAY RECAP '
                '********************************************************\n'
                'web1 : ok=2 changed=1 unreachable=0 failed=0 '
                'skipped=0 rescued=0 ignored=0\n')
        result, _r, _i, _h, _p = self.call_execute(output=output)
        self.assertFalse(result['isError'])
        recap = result['structuredContent']['recap']
        self.assertEqual(recap['web1']['ok'], 14)
        self.assertEqual(recap['web1']['changed'], 4)
        self.assertEqual(recap['web1']['skipped'], 1)
        self.assertEqual(recap['web1']['failed'], 0)
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        self.assertEqual(check_schema(result['structuredContent'],
                                      TOOL_OUTPUT_SCHEMAS['check']),
                         [])

    def test_no_recap_no_key(self):
        # A run which never reached a recap keeps the small summary
        result, _r, _i, _h, _p = self.call_execute()
        self.assertNotIn('recap', result['structuredContent'])

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
        with mock.patch('debops.mcp.tools.execute.os.killpg'):
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
        self.assertIn('cannot resolve the affected hosts',
                      result['content'][0]['text'])
        self.assertTrue(any('aborted' in line
                            for line in recorded.output),
                        recorded.output)

    def test_local_playbook_classification(self):
        from debops.mcp.tools.execute import _local_playbooks
        os.makedirs(os.path.join(self.tmpdir, 'ansible', 'playbooks'))
        runner = type('Runner', (), {
            '_found_playbooks': ['ansible/playbooks/local.yml',
                                 'ansible/collections/'
                                 'ansible_collections/debops/debops/'
                                 'playbooks/site.yml'],
        })()
        local = _local_playbooks(runner, self.project)
        self.assertEqual(local, ['ansible/playbooks/local.yml'])

    def test_a_playbook_outside_the_playbook_dirs_is_local(self):
        # The write tools may write under resources/; a playbook found
        # there is agent-writable content like any other
        from debops.mcp.tools.execute import _local_playbooks
        runner = type('Runner', (), {
            '_found_playbooks': ['resources/scripts/evil.yml'],
        })()
        local = _local_playbooks(runner, self.project)
        self.assertEqual(local, ['resources/scripts/evil.yml'])

    def test_a_collection_outside_the_project_is_not_local(self):
        from debops.mcp.tools.execute import _local_playbooks
        runner = type('Runner', (), {
            '_found_playbooks': ['/usr/share/ansible/collections/'
                                 'ansible_collections/debops/debops/'
                                 'playbooks/site.yml'],
        })()
        self.assertEqual(_local_playbooks(runner, self.project), [])

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
                ansible_args=['a.yml', 'b.yml', '--check'])
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
            popen.side_effect = make_popen(output='failed!\n',
                                           returncode=2)
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
                mock.patch('debops.mcp.tools.execute.os.killpg') \
                as killpg, \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-15)
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True,
                                       enabled_check=lambda: False,
                                       on_disabled=on_disabled)
        # The detached run leads its own process group; the kill
        # switch signals the group
        killpg.assert_called_once_with(popen.return_value.pid,
                                       signal.SIGTERM)
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
                mock.patch('debops.mcp.tools.execute.os.killpg') \
                as killpg, \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-15)
            with self.assertRaises(RequestCancelled):
                _execute_playbook(self.project, self.policy,
                                  {'playbook': 'site'}, self.context,
                                  check=True)
        killpg.assert_called_once_with(popen.return_value.pid,
                                       signal.SIGTERM)

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
                mock.patch('debops.mcp.tools.execute.os.killpg') \
                as killpg, \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            popen.return_value = make_process(returncode=-9)
            result = _execute_playbook(project, policy,
                                       {'playbook': 'site'}, self.context,
                                       check=True)
        killpg.assert_called_once_with(popen.return_value.pid,
                                       signal.SIGKILL)
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
        # 'run' stays on the surface as the policy stub
        names = self.registered_tools({})
        self.assertIn('check', names)
        self.assertIn('run', names)

    def test_none_registers_both_as_stubs(self):
        names = self.registered_tools({'execute': 'none'})
        self.assertIn('check', names)
        self.assertIn('run', names)

    def test_all_registers_both(self):
        names = self.registered_tools({'execute': 'all'})
        self.assertIn('check', names)
        self.assertIn('run', names)

    def test_unknown_mode_falls_back_to_check(self):
        names = self.registered_tools({'execute': 'everything'})
        self.assertIn('check', names)
        self.assertIn('run', names)

    def test_deny_list_still_applies(self):
        # A deliberate removal, not a discoverability surface: the
        # deny list removes even the stub
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
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')
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
            popen.side_effect = make_popen(output=output)
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

    def test_task_status_message_carries_the_log_tail(self):
        # A polling client reads the task status; the progress
        # notifications never reach it, so the status carries the tail
        # of the run log
        from debops.mcp.tasks import Task
        task = Task(60000)
        context = ToolContext(1, threading.Event(), task=task)
        with mock.patch('debops.mcp.tools.execute.STATUS_INTERVAL', 0):
            self.run_with_context(context)
        self.assertIn('TASK [Say hello]', task.status_message)
        self.assertIn('--- log tail ---', task.status_message)
        self.assertIn('ok: [localhost]', task.status_message)


class DetachedCommandTestCase(unittest.TestCase):
    """The wrapper which carries a detached execution."""

    def test_bounds_the_run_with_timeout(self):
        from debops.mcp.tools.execute import _detached_command
        command = _detached_command(['ansible-playbook', 'site.yml'], 3600)
        self.assertEqual(command[:2], ['/bin/sh', '-c'])
        script = command[2]
        self.assertIn('timeout -k 5 3600 ansible-playbook site.yml',
                      script)
        self.assertTrue(script.rstrip().endswith('exit $?'))

    def test_quotes_the_arguments(self):
        from debops.mcp.tools.execute import _detached_command
        command = _detached_command(
                ['ansible-playbook', 'my playbook.yml', '$(id)'], 60)
        script = command[2]
        self.assertIn("'my playbook.yml'", script)
        self.assertIn("'$(id)'", script)

    def test_finalize_preserves_the_exit_code(self):
        from debops.mcp.tools.execute import _detached_command
        command = _detached_command(
                ['ansible-playbook', 'site.yml'], 60,
                finalize=['debops', 'project', 'lock', '/srv/project'])
        lines = command[2].splitlines()
        self.assertEqual(lines[1], 'rc=$?')
        # A failing finalize must not mask the exit code of the run
        self.assertIn('debops project lock /srv/project ||', lines[2])
        self.assertIn('exit code $rc', lines[3])
        self.assertEqual(lines[4], 'exit $rc')

    def test_finalize_targets_the_project_and_its_view(self):
        from debops.mcp.tools.execute import _finalize_command
        project = FakeProject('/srv/myproject')
        command = _finalize_command(project)
        self.assertEqual(command[1:5], ['-m', 'debops', 'project', 'lock'])
        self.assertIn('--view', command)
        self.assertIn('system', command)
        self.assertEqual(command[-1], '/srv/myproject')


class DetachedRunTestCase(unittest.TestCase):
    """A detached execution with a real child process."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-detach-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.context = ToolContext(1, threading.Event())

    def test_the_output_lands_in_the_log_file(self):
        from debops.mcp.tools.execute import run_cancellable
        log = os.path.join(self.tmpdir, 'run.log')
        output, returncode, status = run_cancellable(
                ['/bin/sh', '-c', 'echo hello; echo world'],
                env=dict(os.environ), cwd=self.tmpdir,
                context=self.context, timeout=30,
                log_path=log, detached=True)
        self.assertEqual(status, 'done')
        self.assertEqual(returncode, 0)
        self.assertEqual(output, 'hello\nworld\n')
        with open(log) as fp:
            self.assertEqual(fp.read(), 'hello\nworld\n')
        # The log can carry secrets; it is private to the account
        mode = os.stat(log).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_the_progress_lines_come_back_from_the_log_file(self):
        from debops.mcp.tools.execute import run_cancellable
        log = os.path.join(self.tmpdir, 'run.log')
        seen = []
        run_cancellable(
                ['/bin/sh', '-c', 'echo one; echo two'],
                env=dict(os.environ), cwd=self.tmpdir,
                context=self.context, timeout=30, on_line=seen.append,
                log_path=log, detached=True)
        self.assertEqual(seen, ['one', 'two'])

    def test_cancel_signals_the_process_group(self):
        from debops.mcp.tools.execute import run_cancellable
        process = make_process(returncode=-15)
        self.context.cancelled.set()
        with mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen',
                return_value=process), \
                mock.patch('debops.mcp.tools.execute.os.killpg') as killpg:
            _output, _rc, status = run_cancellable(
                    ['sleep', '60'], env={}, cwd=self.tmpdir,
                    context=self.context, timeout=30, detached=True)
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(status, 'cancelled')

    def test_cancel_terminates_a_non_detached_process(self):
        from debops.mcp.tools.execute import run_cancellable
        process = make_process(returncode=-15)
        self.context.cancelled.set()
        with mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen',
                return_value=process), \
                mock.patch('debops.mcp.tools.execute.os.killpg') as killpg:
            _output, _rc, status = run_cancellable(
                    ['sleep', '60'], env={}, cwd=self.tmpdir,
                    context=self.context, timeout=30, detached=False)
        killpg.assert_not_called()
        process.terminate.assert_called_once_with()
        self.assertEqual(status, 'cancelled')

    def test_timeout_kills_the_detached_process_group(self):
        from debops.mcp.tools.execute import run_cancellable
        process = make_process(returncode=-9, poll=[None, 0])
        with mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen',
                return_value=process), \
                mock.patch('debops.mcp.tools.execute.os.killpg') as killpg:
            _output, _rc, status = run_cancellable(
                    ['sleep', '60'], env={}, cwd=self.tmpdir,
                    context=self.context, timeout=0, detached=True)
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(status, 'timeout')

    def test_cancel_kills_the_whole_detached_tree(self):
        # A real run: the grandchild writes the marker only when it
        # survives the cancellation, which the group kill must prevent
        from debops.mcp.tools.execute import run_cancellable
        log = os.path.join(self.tmpdir, 'run.log')
        marker = os.path.join(self.tmpdir, 'survived')
        threading.Timer(0.3, self.context.cancelled.set).start()
        _output, _rc, status = run_cancellable(
                ['/bin/sh', '-c',
                 '(sleep 1; touch {}) & sleep 60'.format(marker)],
                env=dict(os.environ), cwd=self.tmpdir,
                context=self.context, timeout=120,
                log_path=log, detached=True)
        self.assertEqual(status, 'cancelled')
        time.sleep(1.5)
        self.assertFalse(os.path.exists(marker))


class DetachedExecutionTestCase(unittest.TestCase):
    """The execution tools: every run is detached from the server."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-exec-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')
        self.project = FakeProject(self.tmpdir)
        self.policy = McpPolicy(self.project)
        self.context = ToolContext(1, threading.Event())

    def call_detached(self, output='play recap\n', returncode=0):
        from debops.mcp.tools.execute import _execute_playbook
        with mock.patch(
                'debops.mcp.tools.execute.AnsiblePlaybookRunner') as runner, \
                mock.patch(
                'debops.mcp.tools.execute.AnsibleInventory') as inventory, \
                mock.patch('debops.mcp.tools.execute.run_hooks') as hooks, \
                mock.patch('debops.mcp.tools.execute.subprocess.run',
                           return_value=make_resolve_result(('web1',))), \
                mock.patch(
                'debops.mcp.tools.execute.subprocess.Popen') as popen:
            runner.return_value.command.return_value = [
                    'ansible-playbook', 'site.yml']
            inventory.return_value.unlock.return_value = True
            hooks.return_value = True
            popen.side_effect = make_popen(output=output,
                                           returncode=returncode)
            result = _execute_playbook(self.project, self.policy,
                                       {'playbook': 'site'},
                                       self.context, check=True)
        return result, popen

    def run_logs(self):
        import glob
        return glob.glob(os.path.join(
                os.environ['XDG_CACHE_HOME'], 'debops', 'mcp', 'runs',
                '*', 'execution-*.log'))

    def test_the_command_is_wrapped_and_detached(self):
        result, popen = self.call_detached()
        self.assertFalse(result['isError'])
        command = popen.call_args[0][0]
        self.assertEqual(command[:2], ['/bin/sh', '-c'])
        script = command[2]
        self.assertIn('timeout -k 5 3600 ansible-playbook site.yml',
                      script)
        self.assertIn('-m debops', script)
        self.assertIn('project lock', script)
        self.assertIn('--view system', script)

    def test_the_child_leads_its_own_session(self):
        result, popen = self.call_detached()
        self.assertFalse(result['isError'])
        kwargs = popen.call_args[1]
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)

    def test_the_project_lock_rides_along(self):
        # The flock file descriptor is passed to the child, so the
        # project stays locked for as long as the run, not for as long
        # as the server
        result, popen = self.call_detached()
        self.assertFalse(result['isError'])
        (fd,) = popen.call_args[1]['pass_fds']
        self.assertIsInstance(fd, int)

    def test_the_child_output_is_unbuffered(self):
        # The log file is tailed live; a block-buffered stdout would
        # deliver the progress in chunks
        result, popen = self.call_detached()
        self.assertFalse(result['isError'])
        env = popen.call_args[1]['env']
        self.assertEqual(env['PYTHONUNBUFFERED'], '1')

    def test_the_answer_points_at_the_run_log(self):
        result, _p = self.call_detached()
        self.assertFalse(result['isError'])
        text = result['content'][0]['text']
        (log,) = self.run_logs()
        self.assertIn('log: ' + log, text)
        self.assertEqual(result['structuredContent']['log'], log)

    def test_the_answer_carries_the_execution_id(self):
        result, _p = self.call_detached()
        self.assertFalse(result['isError'])
        summary = result['structuredContent']
        self.assertEqual(summary['executionId'], 1)
        self.assertIn('execution: 1', result['content'][0]['text'])

    def test_the_sidecar_records_the_run(self):
        from debops.mcp import runs
        result, _p = self.call_detached()
        self.assertFalse(result['isError'])
        metadata = runs.read_metadata(result['structuredContent']['log'])
        self.assertEqual(metadata['executionId'], 1)
        self.assertEqual(metadata['mode'], 'check')
        self.assertEqual(metadata['playbooks'], ['site'])
        self.assertEqual(metadata['hosts'], ['web1'])
        self.assertEqual(metadata['exitCode'], 0)
        self.assertEqual(metadata['status'], 'done')
        self.assertIn('startedAt', metadata)
        self.assertIn('finishedAt', metadata)

    def test_the_ids_survive_a_server_restart(self):
        # The ids come from the log directory, so a new server process
        # does not reuse the id of a run whose log still exists
        first, _p1 = self.call_detached()
        second, _p2 = self.call_detached()
        self.assertEqual(first['structuredContent']['executionId'], 1)
        self.assertEqual(second['structuredContent']['executionId'], 2)
        self.assertEqual(len(self.run_logs()), 2)


if __name__ == '__main__':
    unittest.main()


if __name__ == '__main__':
    unittest.main()
