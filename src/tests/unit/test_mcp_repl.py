# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp repl' interpreter."""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.repl import Repl, ReplError, execution_arguments


class FakeClient(object):
    """A scripted MCP client: (method, params) -> queued responses."""

    def __init__(self):
        self.calls = []
        self._responses = {}

    def queue(self, method, *responses):
        self._responses[method] = list(responses)

    def request(self, method, params):
        self.calls.append((method, params))
        queued = self._responses.get(method, [])
        response = queued.pop(0) if queued else {'result': {}}
        return {'result': response}


def make_repl(client, interactive=False):
    out = io.StringIO()
    err = io.StringIO()
    session = Repl(client, project_name='project', view='system',
                   out=out, err=err, interactive=interactive)
    return session, out, err


def tmp_history_home(test_case):
    """A temporary XDG data home, so a loop test never touches the
    user's real command history."""
    directory = tempfile.mkdtemp(prefix='debops-mcp-history-')
    test_case.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    patcher = mock.patch.dict(os.environ, {'XDG_DATA_HOME': directory})
    patcher.start()
    test_case.addCleanup(patcher.stop)
    return directory


class ExecutionArgumentsTestCase(unittest.TestCase):

    def test_a_single_target(self):
        self.assertEqual(execution_arguments(['site']),
                         {'playbook': 'site'})

    def test_multiple_targets_keep_the_order(self):
        self.assertEqual(
                execution_arguments(['service/nginx', 'site']),
                {'playbook': ['service/nginx', 'site']})

    def test_options(self):
        arguments = execution_arguments(
                ['site', '--limit', 'web1', '--forks', '4',
                 '--list-hosts'])
        self.assertEqual(arguments['limit'], 'web1')
        self.assertEqual(arguments['forks'], '4')
        self.assertIs(arguments['list_hosts'], True)
        self.assertEqual(arguments['playbook'], 'site')

    def test_an_unknown_option_is_a_clean_error(self):
        with self.assertRaises(ReplError) as context:
            execution_arguments(['site', '--background'])
        self.assertIn('--background', str(context.exception))

    def test_a_valued_option_needs_a_value(self):
        with self.assertRaises(ReplError):
            execution_arguments(['site', '--limit'])

    def test_a_target_is_required(self):
        with self.assertRaises(ReplError):
            execution_arguments(['--list-hosts'])


class VerbTestCase(unittest.TestCase):

    def setUp(self):
        self.client = FakeClient()
        self.repl, self.out, self.err = make_repl(self.client)

    def test_status_calls_the_tool(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}]})
        self.repl.execute('status')
        self.assertEqual(self.client.calls[0],
                         ('tools/call', {'name': 'project.status',
                                         'arguments': {}}))
        self.assertIn('Project: x', self.out.getvalue())

    def test_an_error_result_is_marked_and_counted(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text', 'text': 'denied'}]})
        self.repl.execute('status')
        self.assertIn('error: denied', self.err.getvalue())
        self.assertTrue(self.repl.had_error)

    def test_an_unknown_verb_is_a_clean_error(self):
        with self.assertRaises(ReplError) as context:
            self.repl.execute('frobnicate')
        self.assertIn("'frobnicate'", str(context.exception))

    def test_exit_raises_eof(self):
        with self.assertRaises(EOFError):
            self.repl.execute('exit')

    def test_view_switch_updates_the_prompt_and_the_cache(self):
        self.repl._completion_cache['view'] = ['system']
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'switched'}]})
        self.repl.execute('view web')
        self.assertEqual(self.repl.view, 'web')
        self.assertEqual(self.repl.prompt, 'debops project:web> ')
        self.assertEqual(self.repl._completion_cache, {})

    def test_a_failed_switch_keeps_the_view(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text', 'text': 'no such view'}]})
        self.repl.execute('view web')
        self.assertEqual(self.repl.view, 'system')


class ConfirmationTestCase(unittest.TestCase):

    def test_the_gate_asks_and_retries(self):
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client, interactive=True)
        client.queue('tools/call', {
                'resultType': 'input_required',
                'inputRequests': {'confirmation': {
                        'params': {'message': 'run it?'}}}}, {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'done'}]})
        with mock.patch('debops.mcp.repl.input', return_value='y'):
            repl_obj.execute('run site')
        # The retry carries the acceptance
        retry = client.calls[1][1]
        self.assertEqual(retry['inputResponses']['confirmation'],
                         {'action': 'accept',
                          'content': {'confirmed': True}})

    def test_a_pipe_declines_the_gate(self):
        client = FakeClient()
        repl_obj, _out, err = make_repl(client, interactive=False)
        client.queue('tools/call', {
                'resultType': 'input_required',
                'inputRequests': {'confirmation': {
                        'params': {'message': 'run it?'}}}}, {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text', 'text': 'declined'}]})
        repl_obj.execute('run site')
        retry = client.calls[1][1]
        self.assertEqual(retry['inputResponses']['confirmation']
                         ['action'], 'decline')
        self.assertIn('a pipe cannot answer', err.getvalue())


class TaskFlowTestCase(unittest.TestCase):

    def test_the_task_is_polled_to_completion(self):
        client = FakeClient()
        repl_obj, out, err = make_repl(client)
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status':
                                    'working'})
        client.queue('tasks/get',
                     {'status': 'working',
                      'statusMessage': 'PLAY [one]'},
                     {'status': 'completed',
                      'result': {'isError': False,
                                 'content': [{'type': 'text',
                                              'text': 'recap'}]}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute('check site')
        # The status tail moved to the error channel, the recap to
        # the answer
        self.assertIn('PLAY [one]', err.getvalue())
        self.assertIn('recap', out.getvalue())

    def test_the_confirmation_inside_a_task(self):
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client, interactive=True)
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1'})
        client.queue('tasks/get',
                     {'status': 'input_required',
                      'inputRequests': {'confirmation': {
                          'params': {'message': 'run it?'}}}},
                     {'status': 'completed',
                      'result': {'isError': False,
                                 'content': [{'type': 'text',
                                              'text': 'recap'}]}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0), \
                mock.patch('debops.mcp.repl.input', return_value='y'):
            repl_obj.execute('run site')
        update = [call for call in client.calls
                  if call[0] == 'tasks/update']
        self.assertEqual(update[0][1]['inputResponses']['confirmation']
                         ['action'], 'accept')

    def test_a_failed_task_is_a_clean_error(self):
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client)
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1'})
        client.queue('tasks/get',
                     {'status': 'failed',
                      'error': {'message': 'boom'}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0), \
                self.assertRaises(ReplError) as context:
            repl_obj.execute('run site')
        self.assertIn('boom', str(context.exception))


class InteractiveLoopTestCase(unittest.TestCase):

    def test_the_plain_loop_reads_executes_and_exits(self):
        import sys as sys_module
        tmp_history_home(self)
        client = FakeClient()
        session, out, err = make_repl(client, interactive=True)
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False,
                                    'content': [{'type': 'text',
                                                 'text': 'ok'}]})
        with mock.patch('builtins.input',
                        side_effect=['status', EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertIn('ok', out.getvalue())

    def test_the_interactive_loop_falls_back_without_pt(self):
        # A missing prompt_toolkit is the plain reader, not a refusal
        import sys as sys_module
        client = FakeClient()
        session, _out, err = make_repl(client, interactive=True)
        with mock.patch.dict(sys_module.modules,
                             {'prompt_toolkit': None,
                              'prompt_toolkit.history': None}), \
                mock.patch('builtins.input', side_effect=[EOFError()]):
            code = repl.run_interactive(session)
        self.assertEqual(code, 0)
        # The session ran, banner included
        self.assertIn('debops mcp repl', err.getvalue())


class ConnectEraTestCase(unittest.TestCase):
    """The REPL checks the daemon's protocol era before talking."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-era-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.socket = os.path.join(self.tmpdir, 'x.sock')

    def fake_client(self, answer):
        instance = mock.Mock()
        instance.request.return_value = answer
        return instance

    def test_a_legacy_daemon_needs_the_flag(self):
        # The legacy answer has no resultType envelope marker
        answer = {'result': {'supportedVersions': []}}
        with mock.patch('debops.mcp.repl.UnixClient',
                        return_value=self.fake_client(answer)):
            with self.assertRaises(ReplError) as context:
                repl.connect_or_spawn(self.socket, None)
        self.assertIn('retry with --legacy', str(context.exception))

    def test_a_native_daemon_refuses_the_flag(self):
        answer = {'result': {'resultType': 'complete'}}
        with mock.patch('debops.mcp.repl.UnixClient',
                        return_value=self.fake_client(answer)):
            with self.assertRaises(ReplError) as context:
                repl.connect_or_spawn(self.socket, None, legacy=True)
        self.assertIn('drop --legacy', str(context.exception))

    def test_the_probe_carries_the_server_version(self):
        answer = {'result': {'resultType': 'complete',
                             '_meta': {'io.modelcontextprotocol/'
                                       'serverInfo':
                                       {'name': 'debops',
                                        'version': '3.1.0'}}}}
        with mock.patch('debops.mcp.repl.UnixClient',
                        return_value=self.fake_client(answer)):
            connected = repl.connect_or_spawn(self.socket, None)
        self.assertEqual(connected.server_version, '3.1.0')
        self.assertEqual(connected.protocol_version, '2026-07-28')


class TrustVerbsTestCase(unittest.TestCase):
    """The elevate/drop/watch verbs and the passphrase handling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-trustrepl-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _write_passphrase(self, words='correct horse battery staple'):
        from debops.mcp import trust
        path = os.path.join(self.tmpdir, trust.TRUST_PATH)
        os.makedirs(os.path.dirname(path))
        with open(path, 'w') as fp:
            fp.write(words + '\n')

    def test_elevate_authenticates_then_raises(self):
        self._write_passphrase()
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, err = make_repl(client, interactive=False)
        session.project_path = self.tmpdir
        client.queue('server/authenticate',
                     {'token': 'the-token', 'tokenType': 'bearer'})
        client.queue('server/elevate', {'elevated': True,
                                        'expiresIn': 900})
        session.execute('elevate')
        # The token rides from here on; the prompt carries the marker
        self.assertEqual(client.bearer, 'the-token')
        self.assertTrue(session.elevated)
        self.assertEqual(session.prompt, 'debops! project:system> ')
        self.assertEqual(client.calls[0][0], 'server/authenticate')
        self.assertEqual(client.calls[1],
                         ('server/elevate',
                          {'passphrase': 'correct horse battery '
                           'staple'}))

    def test_elevate_twice_is_a_clean_error(self):
        client = FakeClient()
        client.legacy = False
        client.bearer = 'the-token'
        session, _out, _err = make_repl(client)
        session.elevated = True
        with self.assertRaises(ReplError) as context:
            session.execute('elevate')
        self.assertIn('already elevated', str(context.exception))

    def test_elevate_needs_the_new_protocol(self):
        client = FakeClient()
        client.legacy = True
        session, _out, _err = make_repl(client)
        with self.assertRaises(ReplError) as context:
            session.execute('elevate')
        self.assertIn('2025-era', str(context.exception))

    def test_drop_clears_the_flag_and_tells_the_server(self):
        client = FakeClient()
        client.legacy = False
        client.bearer = 'the-token'
        session, _out, _err = make_repl(client)
        session.elevated = True
        client.queue('server/drop', {'elevated': False})
        session.execute('drop')
        self.assertFalse(session.elevated)
        self.assertEqual(client.calls[0][0], 'server/drop')

    def test_elevate_records_the_ttl(self):
        self._write_passphrase()
        client = FakeClient()
        client.legacy = False
        client.bearer = 'the-token'
        session, _out, _err = make_repl(client)
        session.project_path = self.tmpdir
        client.queue('server/elevate', {'elevated': True,
                                        'expiresIn': 900})
        before = time.monotonic()
        session.execute('elevate')
        self.assertTrue(session.elevated)
        self.assertIsNotNone(session._elevated_until)
        self.assertGreater(session._elevated_until, before + 800)

    def test_drop_when_not_elevated_is_a_clean_error(self):
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, _err = make_repl(client)
        with self.assertRaises(ReplError) as context:
            session.execute('drop')
        self.assertIn('not elevated', str(context.exception))

    def test_watch_needs_trust_and_a_pipe_cannot_ask(self):
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, _err = make_repl(client, interactive=False)
        session.project_path = self.tmpdir
        # No passphrase file: a pipe cannot ask — the refusal is terse
        with self.assertRaises(ReplError) as context:
            session.execute('watch')
        self.assertEqual(str(context.exception), 'no trust passphrase')

    def test_watch_streams_the_activity(self):
        client = FakeClient()
        client.legacy = False
        client.bearer = 'the-token'
        session, out, _err = make_repl(client)
        stream = iter([
                {'method': 'notifications/subscriptions/acknowledged',
                 'params': {'notifications': {'activity': True}}},
                {'method': 'notifications/activity',
                 'params': {'line': 'Tool check(site) -> ok'}},
                {'id': 1, 'result': {}},
        ])
        client.request_streaming = lambda *a, **kw: stream
        session.execute('watch')
        self.assertIn('Tool check(site) -> ok', out.getvalue())
        self.assertEqual(client.calls, [])  # the stream bypasses them

    def test_a_stalled_stream_is_a_clean_error(self):
        # The read timeout of a quiet stream is a line on the error
        # channel, not a traceback
        client = FakeClient()
        client.legacy = False
        client.bearer = 'the-token'
        session, _out, _err = make_repl(client)

        def stalled(*args, **kwargs):
            raise TimeoutError('timed out')

        client.request_streaming = stalled
        with self.assertRaises(ReplError) as context:
            session.execute('watch')
        self.assertIn('timed out', str(context.exception))

    def test_bootstrap_trust_with_a_readable_file(self):
        self._write_passphrase()
        client = FakeClient()
        client.bearer = None
        client.queue('server/authenticate',
                     {'token': 'the-token', 'tokenType': 'bearer'})
        project = type('Project', (), {'path': self.tmpdir})()
        self.assertTrue(repl.bootstrap_trust(client, project))
        self.assertEqual(client.bearer, 'the-token')

    def test_bootstrap_trust_without_a_file_is_silent(self):
        client = FakeClient()
        client.bearer = None
        project = type('Project', (), {'path': self.tmpdir})()
        self.assertFalse(repl.bootstrap_trust(client, project))
        self.assertEqual(client.calls, [])

    def test_bootstrap_trust_without_a_file_is_silent(self):
        client = FakeClient()
        client.bearer = None
        project = type('Project', (), {'path': self.tmpdir})()
        self.assertFalse(repl.bootstrap_trust(client, project))
        self.assertEqual(client.calls, [])

    def test_a_missing_trust_root_is_a_clean_refusal(self):
        # No file, nothing to check an answer against: the refusal is
        # terse, not a prompt which cannot work
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, _err = make_repl(client, interactive=True)
        session.project_path = self.tmpdir
        with self.assertRaises(ReplError) as context:
            session.execute('elevate')
        self.assertEqual(str(context.exception), 'no trust passphrase')

    def test_an_unreadable_trust_file_prompts(self):
        self._write_passphrase()
        path = os.path.join(
                self.tmpdir,
                __import__('debops.mcp.trust', fromlist=['x'])
                .TRUST_PATH)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, _err = make_repl(client, interactive=True)
        session.project_path = self.tmpdir
        client.queue('server/authenticate',
                     {'token': 'the-token', 'tokenType': 'bearer'})
        client.queue('server/elevate', {'elevated': True,
                                        'expiresIn': 900})
        with mock.patch('debops.mcp.repl.getpass.getpass',
                        return_value='correct horse battery staple'):
            session.execute('elevate')
        self.assertTrue(session.elevated)

    def test_ctrl_c_at_the_passphrase_prompt_survives_the_session(
            self):
        self._write_passphrase()
        path = os.path.join(
                self.tmpdir,
                __import__('debops.mcp.trust', fromlist=['x'])
                .TRUST_PATH)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        client.close = lambda: None
        session, _out, err = make_repl(client, interactive=True)
        session.project_path = self.tmpdir
        tmp_history_home(self)
        with mock.patch('builtins.input',
                        side_effect=['elevate', EOFError()]), \
                mock.patch('debops.mcp.repl.getpass.getpass',
                           side_effect=KeyboardInterrupt):
            code = repl.run_interactive_plain(session)
        # The interrupt became a line on the error channel, the
        # session went on to a clean exit
        self.assertEqual(code, 0)
        self.assertIn('cancelled', err.getvalue())

    def test_ctrl_c_during_a_call_closes_the_connection(self):
        # A half-read answer must not wait for the next command
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        closes = []
        client.close = lambda: closes.append(True)
        client.request = mock.Mock(side_effect=KeyboardInterrupt)
        session, _out, err = make_repl(client, interactive=True)
        tmp_history_home(self)
        with mock.patch('builtins.input',
                        side_effect=['status', EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertIn('^C', err.getvalue())
        self.assertEqual(closes, [True])

    def test_a_dead_daemon_is_a_clean_error(self):
        # The daemon died under the session: a clean error, not a
        # traceback
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        session, _out, _err = make_repl(client)

        def dead(*args):
            raise EOFError('the server closed the connection without '
                           'a response')

        client.request = dead
        with self.assertRaises(ReplError) as context:
            session.execute('status')
        self.assertIn('the daemon is not answering',
                      str(context.exception))


class StopDaemonTestCase(unittest.TestCase):
    """The '--stop' guardrails: only a client-spawned daemon goes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-stop-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.socket = os.path.join(self.tmpdir, 'x.sock')

    def _pid_file(self, *lines):
        with open(self.socket + '.pid', 'w') as fp:
            fp.write('\n'.join(lines) + '\n')

    def test_no_pid_file(self):
        with self.assertRaises(ReplError) as context:
            repl.stop_daemon(self.socket)
        self.assertIn('no pid file', str(context.exception))

    def test_a_hand_started_daemon_is_refused(self):
        # No origin line: the daemon came from a deliberate serve
        self._pid_file(str(os.getpid()))
        with self.assertRaises(ReplError) as context:
            repl.stop_daemon(self.socket)
        self.assertIn('not spawned by a client', str(context.exception))
        # The refusal names the pid, so the operator can still act
        self.assertIn('kill {}'.format(os.getpid()),
                      str(context.exception))

    def test_a_non_daemon_pid_is_refused(self):
        # A recycled pid must not die: the test process is no daemon
        self._pid_file(str(os.getpid()), 'spawned')
        with self.assertRaises(ReplError) as context:
            repl.stop_daemon(self.socket)
        self.assertIn('not a debops-mcp process',
                      str(context.exception))

    def test_a_dead_daemon_is_reported(self):
        # Beyond any pid_max: the stale file is reported, not killed
        self._pid_file('99999999', 'spawned')
        with self.assertRaises(ReplError) as context:
            repl.stop_daemon(self.socket)
        self.assertIn('already gone', str(context.exception))


class HistoryFormatTestCase(unittest.TestCase):
    """The shared command history file: one format, every front end
    reads and writes it; the legacy plain lines still load."""

    def setUp(self):
        self.tmpdir = tmp_history_home(self)
        self.path = os.path.join(self.tmpdir, 'debops',
                                 'mcp-repl-history')

    def test_the_appended_format_loads_with_prompt_toolkit(self):
        try:
            from prompt_toolkit.history import FileHistory
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        os.makedirs(os.path.dirname(self.path))
        repl._append_history_string(self.path, 'status')
        repl._append_history_string(self.path, 'run containers')
        # prompt_toolkit reads its own format back, newest first
        self.assertEqual(
                list(FileHistory(self.path).load_history_strings()),
                ['run containers', 'status'])
        # ... and our reader sees the same, oldest first
        self.assertEqual(repl._read_history_strings(self.path),
                         ['status', 'run containers'])

    def test_the_plain_format_still_loads(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as fp:
            fp.write('exit\nls\n')
        self.assertEqual(repl._read_history_strings(self.path),
                         ['exit', 'ls'])

    def test_the_mixed_format_loads(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as fp:
            fp.write('legacy command\n'
                     '\n# 2026-10-23 10:00:00.000000\n+status\n'
                     '\n# 2026-10-23 10:01:00.000000\n'
                     '+multi one\n+multi two\n')
        self.assertEqual(repl._read_history_strings(self.path),
                         ['legacy command', 'status',
                          'multi one\nmulti two'])

    def test_a_missing_history_reads_empty(self):
        self.assertEqual(repl._read_history_strings(self.path), [])

    def test_the_plain_loop_loads_and_appends(self):
        fake_readline = mock.MagicMock()
        client = FakeClient()
        session, _out, _err = make_repl(client, interactive=True)
        # A documentation session shares the documentation file
        path = session.history_file
        self.assertTrue(path.endswith('mcp-history-documentation'))
        with open(path, 'w') as fp:
            fp.write('\n# 2026-10-23 10:00:00.000000\n+seed command\n')
        with mock.patch.dict(sys.modules, {'readline': fake_readline}), \
                mock.patch('builtins.input',
                           side_effect=['help', EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        # The seed fed the recall, the command joined the file in
        # the shared format
        fake_readline.add_history.assert_called_once_with(
                'seed command')
        with open(path) as fp:
            self.assertIn('+help', fp.read())


class HistoryKeyTestCase(unittest.TestCase):
    """The per-project, per-view history files: the key naming of
    the run logs, moved by the view switch."""

    def setUp(self):
        self.tmpdir = tmp_history_home(self)
        self.project_path = os.path.join(self.tmpdir, 'project')

    def make_project_session(self, view='system'):
        client = FakeClient()
        session, _out, _err = make_repl(client)
        session.project_path = self.project_path
        session.view = view
        return session, client

    def history_name(self, view):
        from debops.mcp.runs import path_key
        return 'mcp-history-' + path_key(self.project_path,
                                         'project', view)

    def test_the_history_is_keyed_per_project_and_view(self):
        session, _client = self.make_project_session()
        name = os.path.basename(session.history_file)
        self.assertEqual(name, self.history_name('system'))
        # A view switch moves the file; the path hash stays
        session.view = 'admin'
        self.assertEqual(os.path.basename(session.history_file),
                         self.history_name('admin'))
        self.assertNotEqual(self.history_name('admin'),
                            self.history_name('system'))

    def test_a_project_without_a_view_has_a_viewless_key(self):
        session, _client = self.make_project_session(view=None)
        self.assertEqual(os.path.basename(session.history_file),
                         self.history_name(''))

    def test_the_docs_history_is_shared(self):
        client = FakeClient()
        session, _out, _err = make_repl(client)
        self.assertEqual(os.path.basename(session.history_file),
                         'mcp-history-documentation')

    def test_a_view_switch_fires_the_history_hook(self):
        session, client = self.make_project_session()
        fired = []
        session._history_changed = lambda: fired.append(True)
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        session.execute('view admin')
        self.assertEqual(fired, [True])
        self.assertEqual(session.view, 'admin')

    def test_a_failed_view_switch_keeps_the_history(self):
        session, client = self.make_project_session()
        fired = []
        session._history_changed = lambda: fired.append(True)
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': True,
                                    'content': [{'type': 'text',
                                                 'text': 'no'}]})
        session.execute('view admin')
        self.assertEqual(fired, [])
        self.assertEqual(session.view, 'system')

    def test_the_pt_loop_follows_the_view_switch(self):
        try:
            from prompt_toolkit.history import FileHistory
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, client = self.make_project_session()
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        with mock.patch('prompt_toolkit.PromptSession') as pt:
            pt.return_value.prompt.side_effect = EOFError()
            code = repl.run_interactive(session)
        self.assertEqual(code, 0)
        # The initial file is the startup view's
        _args, kwargs = pt.call_args
        self.assertTrue(kwargs['history'].filename.endswith(
                self.history_name('system')))
        # The hook re-points the buffer at the current view's file
        session.execute('view admin')
        history = pt.return_value.default_buffer.history
        self.assertIsInstance(history, FileHistory)
        self.assertTrue(history.filename.endswith(
                self.history_name('admin')))

    def test_the_plain_loop_reloads_on_a_view_switch(self):
        session, client = self.make_project_session()
        for view, command in (('system', 'system command'),
                              ('admin', 'admin command')):
            path = os.path.join(self.tmpdir, 'debops',
                                self.history_name(view))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as fp:
                fp.write('\n# 2026-10-23 10:00:00.000000\n'
                         '+{}\n'.format(command))
        fake_readline = mock.MagicMock()
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        with mock.patch.dict(sys.modules, {'readline': fake_readline}), \
                mock.patch('builtins.input',
                           side_effect=['view admin', EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertEqual(fake_readline.add_history.call_args_list,
                         [mock.call('system command'),
                          mock.call('admin command')])
        fake_readline.clear_history.assert_called_once_with()


class CommandFormTestCase(unittest.TestCase):
    """The '-c' one-shot form: the commands run non-interactively
    and the exit status notes an error."""

    def test_the_command_string_runs_in_order(self):
        client = FakeClient()
        session, out, err = make_repl(client)
        code = repl.run_pipe(session, 'help\nnosuchverb\nhelp')
        self.assertEqual(code, 1)
        # The error did not stop the script; it is noted in the
        # exit status
        self.assertEqual(out.getvalue().count('Commands'), 2)
        self.assertIn('unknown verb', err.getvalue())

    def test_a_clean_command_string_exits_zero(self):
        client = FakeClient()
        session, out, _err = make_repl(client)
        code = repl.run_pipe(session, 'help\nhelp')
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count('Commands'), 2)

    def test_the_command_form_forces_the_pipe(self):
        # A frontend and a terminal are ignored with a command
        client = FakeClient()
        with mock.patch.object(repl, 'run_pipe', return_value=0) \
                as pipe:
            code = repl.run(client, command='status', interactive=True,
                            frontend='console')
        self.assertEqual(code, 0)
        pipe.assert_called_once()
        session, commands = pipe.call_args[0]
        self.assertIs(type(session), repl.Repl)
        self.assertFalse(session.interactive)
        self.assertEqual(commands, 'status')


class BannerTestCase(unittest.TestCase):
    def test_the_banner_names_the_version_and_the_protocol(self):
        client = FakeClient()
        client.server_version = '3.1.0'
        client.protocol_version = '2026-07-28'
        session, _out, err = make_repl(client, interactive=True)
        tmp_history_home(self)
        with mock.patch('builtins.input', side_effect=[EOFError()]):
            repl.run_interactive_plain(session)
        self.assertIn('(DebOps 3.1.0, protocol 2026-07-28)',
                      err.getvalue())


class CompletionTestCase(unittest.TestCase):

    def test_the_playbook_targets_complete(self):
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client)
        client.queue('tools/call', {
                'resultType': 'complete',
                'structuredContent': [{'target': 'site'},
                                      {'target': 'service/nginx'}]})
        self.assertEqual(repl_obj.complete_candidates('check'),
                         ['site', 'service/nginx'])
        # The cache held: one call only
        self.assertEqual(repl_obj.complete_candidates('run'),
                         ['site', 'service/nginx'])
        self.assertEqual(len(client.calls), 1)

    def test_a_dead_server_completes_nothing(self):
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client)

        def dead(method, params):
            raise ConnectionError('gone')

        client.request = dead
        self.assertEqual(repl_obj.complete_candidates('check'), [])

    def test_the_completer_speaks_the_full_pt_protocol(self):
        # The async completion path of prompt_toolkit calls
        # get_completions_async, which only the real base class
        # provides — a plain-object completer died on it
        try:
            from prompt_toolkit.document import Document
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        client = FakeClient()
        session, _out, _err = make_repl(client)
        completer = repl._Completer(session)
        self.assertTrue(hasattr(completer, 'get_completions_async'))
        # The verbs complete at the start of the line
        found = [entry.text for entry in completer.get_completions(
                Document('sta'), None)]
        self.assertIn('status', found)
        # The execution options complete after the target
        found = [entry.text for entry in completer.get_completions(
                Document('check site --l'), None)]
        self.assertIn('--limit', found)
        self.assertIn('--list-hosts', found)

    def test_the_pt_path_is_taken_when_available(self):
        # A bad import inside the pt branch must not silently fall
        # back to the plain reader (the regression: CompleteStyle
        # imported from the wrong submodule)
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        client = FakeClient()
        session, _out, _err = make_repl(client)
        with mock.patch('prompt_toolkit.PromptSession') as pt_session:
            pt_session.return_value.prompt.side_effect = EOFError()
            code = repl.run_interactive(session)
        self.assertEqual(code, 0)
        self.assertTrue(pt_session.called)


class NavigationTestCase(unittest.TestCase):
    """The working-directory navigation: the resolution, the verbs,
    the prompt, the completion."""

    def make_session(self, project_type='legacy',
                     view_root_rel='ansible', elevated=False):
        client = FakeClient()
        client.legacy = False
        client.bearer = None
        out, err = io.StringIO(), io.StringIO()
        session = Repl(client, project_name='project', view='system',
                       out=out, err=err, project_type=project_type,
                       view_root_rel=view_root_rel)
        session.elevated = elevated
        return session, client, out, err

    def queue_list(self, client, *entries):
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'listing'}],
                'structuredContent': [
                        {'name': name, 'type': kind}
                        for name, kind in entries]})

    def test_the_prompt_collapses_the_parents(self):
        session, _client, _out, _err = self.make_session()
        session.cwd = 'ansible/inventory/group_vars/all'
        self.assertEqual(session.prompt,
                         'debops project:system/i/g/all/> ')

    def test_the_prompt_at_the_view_root_is_unchanged(self):
        session, _client, _out, _err = self.make_session()
        self.assertEqual(session.prompt, 'debops project:system> ')

    def test_the_prompt_above_the_view_keeps_the_view(self):
        # The execution verbs keep targeting the view; the path goes
        # project-rooted
        session, _client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = '.debops/conf.d'
        self.assertEqual(session.prompt,
                         'debops! project:system:/.debops/conf.d> ')
        session.cwd = ''
        self.assertEqual(session.prompt, 'debops! project:system:/> ')

    def test_pwd_prints_the_project_rooted_path(self):
        session, _client, out, _err = self.make_session()
        session.cwd = 'ansible/inventory/group_vars/all'
        session.execute('pwd')
        self.assertEqual(out.getvalue(),
                         '/ansible/inventory/group_vars/all\n')

    def test_cd_tracks_the_directory(self):
        session, client, _out, _err = self.make_session()
        self.queue_list(client, ('group_vars', 'dir'))
        session.execute('cd inventory')
        self.assertEqual(session.cwd, 'ansible/inventory')
        self.assertEqual(client.calls[0],
                         ('tools/call',
                          {'name': 'project.list',
                           'arguments': {'path': 'ansible/inventory'}}))

    def test_cd_validates_the_target(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text',
                             'text': 'directory not found'}]})
        session.execute('cd nowhere')
        self.assertEqual(session.cwd, 'ansible')
        self.assertTrue(session.had_error)

    def test_cd_bare_returns_to_the_view_root(self):
        session, client, _out, _err = self.make_session()
        self.queue_list(client)
        session.execute('cd inventory')
        self.queue_list(client)
        session.execute('cd')
        self.assertEqual(session.cwd, 'ansible')

    def test_cd_dash_goes_back(self):
        session, client, _out, _err = self.make_session()
        self.queue_list(client)
        session.execute('cd inventory')
        self.queue_list(client)
        session.execute('cd -')
        self.assertEqual(session.cwd, 'ansible')
        self.queue_list(client)
        session.execute('cd -')
        self.assertEqual(session.cwd, 'ansible/inventory')

    def test_cd_dash_without_a_previous_is_a_clean_error(self):
        session, _client, _out, _err = self.make_session()
        with self.assertRaises(ReplError) as context:
            session.execute('cd -')
        self.assertIn('no previous directory', str(context.exception))

    def test_the_view_edge_refuses_unelevated(self):
        session, _client, _out, _err = self.make_session()
        with self.assertRaises(ReplError) as context:
            session.execute('cd ..')
        self.assertIn("the view's edge; elevate to go further",
                      str(context.exception))

    def test_the_elevated_roam_and_the_project_edge(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        self.queue_list(client)
        session.execute('cd ..')
        self.assertEqual(session.cwd, '')
        with self.assertRaises(ReplError) as context:
            session.execute('cd ..')
        self.assertIn("the project's edge", str(context.exception))

    def test_the_elevated_absolute_anchor(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        self.queue_list(client, ('conf.d', 'dir'))
        session.execute('cd /.debops')
        self.assertEqual(session.cwd, '.debops')

    def test_the_absolute_anchor_unelevated_is_the_edge(self):
        session, _client, _out, _err = self.make_session()
        with self.assertRaises(ReplError) as context:
            session.execute('ls /.debops')
        self.assertIn("the view's edge", str(context.exception))

    def test_ls_defaults_to_the_cwd(self):
        session, client, _out, _err = self.make_session()
        session.cwd = 'ansible/inventory'
        self.queue_list(client, ('hosts', 'file'))
        session.execute('ls')
        self.assertEqual(client.calls[0][1],
                         {'name': 'project.list',
                          'arguments': {'path': 'ansible/inventory'}})

    def test_ls_at_the_project_root_omits_the_path(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = ''
        self.queue_list(client)
        session.execute('ls')
        self.assertEqual(client.calls[0][1],
                         {'name': 'project.list', 'arguments': {}})

    def test_read_and_files_resolve_against_the_cwd(self):
        session, client, _out, _err = self.make_session()
        session.cwd = 'ansible/inventory'
        client.queue('tools/call',
                     {'resultType': 'complete', 'isError': False,
                      'content': []},
                     {'resultType': 'complete', 'isError': False,
                      'content': []})
        session.execute('read hosts')
        self.assertEqual(
                client.calls[0][1],
                {'name': 'project.read',
                 'arguments': {'path': 'ansible/inventory/hosts'}})
        session.execute('files group_vars')
        self.assertEqual(
                client.calls[1][1],
                {'name': 'project.files',
                 'arguments': {'path': 'ansible/inventory/group_vars'}})

    def test_mkdir_rm_mv_spell_the_view_relative_path(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call',
                     {'resultType': 'complete', 'isError': False,
                      'content': []},
                     {'resultType': 'complete', 'isError': False,
                      'content': []},
                     {'resultType': 'complete', 'isError': False,
                      'content': []})
        session.execute('mkdir resources/scripts')
        self.assertEqual(
                client.calls[0][1],
                {'name': 'project.mkdir',
                 'arguments': {'path': 'resources/scripts'}})
        session.execute('rm playbooks/deploy.yml')
        self.assertEqual(
                client.calls[1][1],
                {'name': 'file.delete',
                 'arguments': {'path': 'playbooks/deploy.yml'}})
        session.execute('mv playbooks/a.yml playbooks/b.yml')
        self.assertEqual(
                client.calls[2][1],
                {'name': 'file.move',
                 'arguments': {'source': 'playbooks/a.yml',
                               'target': 'playbooks/b.yml'}})

    def test_the_elevated_write_spelling_is_project_rooted(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = ''
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        session.execute('mkdir .debops/conf.d')
        self.assertEqual(
                client.calls[0][1],
                {'name': 'project.mkdir',
                 'arguments': {'path': '/.debops/conf.d'}})

    def test_cp_reads_then_writes(self):
        session, client, _out, _err = self.make_session()
        session.cwd = 'ansible/playbooks'
        client.queue('tools/call',
                     {'resultType': 'complete', 'isError': False,
                      'content': [{'type': 'text', 'text': '# x'}],
                      'structuredContent': {
                              'path': 'ansible/playbooks/a.yml',
                              'content': '---\n[]\n'}},
                     {'resultType': 'complete', 'isError': False,
                      'content': []})
        session.execute('cp a.yml b.yml')
        self.assertEqual(
                client.calls[0][1],
                {'name': 'project.read',
                 'arguments': {'path': 'ansible/playbooks/a.yml'}})
        self.assertEqual(
                client.calls[1][1],
                {'name': 'file.write',
                 'arguments': {'path': 'playbooks/b.yml',
                               'content': '---\n[]\n'}})

    def test_cp_a_directory_target_takes_the_basename(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [],
                'structuredContent': {'path': 'x', 'content': 'c\n'}},
                {'resultType': 'complete', 'isError': False,
                 'content': []})
        session.execute('cp playbooks/a.yml resources/')
        self.assertEqual(
                client.calls[1][1],
                {'name': 'file.write',
                 'arguments': {'path': 'resources/a.yml',
                               'content': 'c\n'}})

    def test_cp_needs_structured_reads(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': '# fenced'}]})
        with self.assertRaises(ReplError) as context:
            session.execute('cp a.yml b.yml')
        self.assertIn('structured', str(context.exception))

    def test_the_view_switch_resets_the_cwd(self):
        session, client, _out, _err = self.make_session(
                project_type='modern',
                view_root_rel='ansible/views/system')
        session.cwd = 'ansible/views/system/inventory'
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        session.execute('view webservers')
        self.assertEqual(session.view, 'webservers')
        self.assertEqual(session.cwd, 'ansible/views/webservers')
        self.assertIsNone(session._previous_cwd)

    def test_the_view_switch_in_a_legacy_project(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        session.execute('view webservers')
        self.assertEqual(session.cwd, 'ansible')

    def test_drop_resets_an_out_of_scope_cwd(self):
        session, client, _out, err = self.make_session(elevated=True)
        client.bearer = 'the-token'
        session.cwd = '.debops'
        client.queue('server/drop', {'elevated': False})
        session.execute('drop')
        self.assertEqual(session.cwd, 'ansible')
        self.assertIn('the view root', err.getvalue())

    def test_resolve_write_refuses_the_roots(self):
        session, _client, _out, _err = self.make_session()
        with self.assertRaises(ReplError) as context:
            session.execute('rm .')
        self.assertIn('the view directory itself',
                      str(context.exception))
        session.elevated = True
        session.cwd = ''
        with self.assertRaises(ReplError) as context:
            session.execute('rm /')
        self.assertIn('the project directory itself',
                      str(context.exception))

    def test_path_candidates_complete_relative_to_the_cwd(self):
        session, client, _out, _err = self.make_session()
        session.cwd = 'ansible/inventory'
        self.queue_list(client, ('group_vars', 'dir'),
                        ('hosts', 'file'))
        self.assertEqual(session.path_candidates('ls', 'gr'),
                         ['group_vars/'])
        # The cache held: one call only
        self.assertEqual(session.path_candidates('ls', 'h'), ['hosts'])
        self.assertEqual(len(client.calls), 1)

    def test_path_candidates_cd_completes_directories_only(self):
        session, client, _out, _err = self.make_session()
        self.queue_list(client, ('group_vars', 'dir'),
                        ('hosts', 'file'))
        self.assertEqual(session.path_candidates('cd', ''),
                         ['group_vars/'])

    def test_path_candidates_with_a_subdirectory(self):
        session, client, _out, _err = self.make_session()
        self.queue_list(client, ('all', 'dir'))
        self.assertEqual(
                session.path_candidates('cd', 'inventory/group_vars/'),
                ['inventory/group_vars/all/'])

    def test_path_candidates_respect_the_edge(self):
        session, _client, _out, _err = self.make_session()
        self.assertEqual(session.path_candidates('ls', '../..'), [])

    def test_the_completer_dispatches_the_path_verbs(self):
        try:
            from prompt_toolkit.document import Document
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, client, _out, _err = self.make_session()
        self.queue_list(client, ('inventory', 'dir'),
                        ('playbooks', 'dir'))
        completer = repl._Completer(session)
        found = [entry.text for entry in completer.get_completions(
                Document('cd in'), None)]
        self.assertEqual(found, ['inventory/'])
        # The verbs themselves complete at the start of the line
        found = [entry.text for entry in completer.get_completions(
                Document('mkd'), None)]
        self.assertIn('mkdir', found)

    def test_the_status_line(self):
        session, _client, _out, _err = self.make_session()
        session.cwd = 'ansible/inventory/group_vars/all'
        self.assertEqual(session.status_line(),
                         ('project:system/i/g/all/', ''))

    def test_the_status_line_above_the_view(self):
        session, _client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = '.debops/conf.d'
        session._elevated_until = time.monotonic() + 900
        self.assertEqual(session.status_line(),
                         ('project:system:/.debops/conf.d',
                          '!elev 15m'))

    def test_the_status_line_marks_the_trust(self):
        session, client, _out, _err = self.make_session()
        client.bearer = 'the-token'
        self.assertEqual(session.status_line(),
                         ('project:system', 'trusted'))

    def test_the_status_line_drops_an_expired_elevation(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        client.bearer = 'the-token'
        session._elevated_until = time.monotonic() - 1
        self.assertEqual(session.status_line(),
                         ('project:system', 'trusted'))
        self.assertFalse(session.elevated)


if __name__ == '__main__':
    unittest.main()
