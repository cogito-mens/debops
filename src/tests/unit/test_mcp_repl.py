# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the 'mcp repl' interpreter."""

import io
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest

from unittest import mock

from debops.mcp import repl
from debops.mcp.repl import (COMMAND_SPECS, Repl, ReplError,
                             _HelpRequested, _candidates_for,
                             _command_help, execution_arguments)


class FakeClient(object):
    """A scripted MCP client: (method, params) -> queued responses."""

    def __init__(self):
        self.calls = []
        self._responses = {}
        self.closed = False
        self.legacy = False
        # A scripted side connection for the log tails, when the
        # test wants one (the clone without it refuses)
        self.tail = None

    def close(self):
        self.closed = True

    def clone(self):
        if self.tail is None:
            raise AttributeError('no tail scripted')
        return self.tail

    def queue(self, method, *responses):
        self._responses[method] = list(responses)

    def request(self, method, params):
        self.calls.append((method, params))
        queued = self._responses.get(method, [])
        response = queued.pop(0) if queued else {'result': {}}
        return {'result': response}


class FakeTailConnection(object):
    """A scripted side connection: the queued log lines stream, then
    the stream ends (the server's finalization)."""

    def __init__(self, lines=()):
        self.lines = list(lines)
        self.closed = False
        self.subscriptions = []

    def request_streaming(self, method, params):
        self.subscriptions.append(params['notifications'])
        for line in self.lines:
            yield {'method': 'notifications/runs/log',
                   'params': {'line': line}}

    def close(self):
        self.closed = True


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


def tool_params(name, arguments, view='system'):
    """The expected tools/call params: the session's view is
    declared on every call, for the daemon's concurrency guard."""
    return {'name': name, 'arguments': arguments,
            '_meta': {'org.debops/view': view}}


class ExecutionArgumentsTestCase(unittest.TestCase):

    def test_a_single_target(self):
        self.assertEqual(execution_arguments(
                ['site'], COMMAND_SPECS['run']), {'playbook': 'site'})

    def test_multiple_targets_keep_the_order(self):
        self.assertEqual(
                execution_arguments(['service/nginx', 'site'],
                                    COMMAND_SPECS['run']),
                {'playbook': ['service/nginx', 'site']})

    def test_options(self):
        arguments = execution_arguments(
                ['site', '--limit', 'web1', '--forks', '4',
                 '--list-hosts'], COMMAND_SPECS['run'])
        self.assertEqual(arguments['limit'], 'web1')
        self.assertEqual(arguments['forks'], '4')
        self.assertIs(arguments['list_hosts'], True)
        self.assertEqual(arguments['playbook'], 'site')

    def test_an_unknown_option_is_a_clean_error(self):
        with self.assertRaises(ReplError) as context:
            execution_arguments(['site', '--background'],
                                COMMAND_SPECS['run'])
        self.assertIn('--background', str(context.exception))

    def test_a_valued_option_needs_a_value(self):
        with self.assertRaises(ReplError):
            execution_arguments(['site', '--limit'],
                                COMMAND_SPECS['run'])

    def test_a_target_is_required(self):
        with self.assertRaises(ReplError):
            execution_arguments(['--list-hosts'], COMMAND_SPECS['run'])


class CommandEngineTestCase(unittest.TestCase):
    """The command-line engine of the verbs: the ansible-playbook
    forms, the verbose help and the completion."""

    def setUp(self):
        self.spec = COMMAND_SPECS['run']

    def test_the_short_forms(self):
        arguments = execution_arguments(
                ['site', '-l', 'web*', '-t', 'php,nginx'], self.spec)
        self.assertEqual(arguments['limit'], 'web*')
        self.assertEqual(arguments['tags'], 'php,nginx')

    def test_the_attached_and_equals_forms(self):
        arguments = execution_arguments(
                ['site', '-lweb*', '--tags=php'], self.spec)
        self.assertEqual(arguments['limit'], 'web*')
        self.assertEqual(arguments['tags'], 'php')

    def test_a_flag_never_consumes_the_next_token(self):
        arguments = execution_arguments(
                ['site', '--list-tasks', 'other'], self.spec)
        self.assertIs(arguments['list_tasks'], True)
        self.assertEqual(arguments['playbook'], ['site', 'other'])

    def test_a_double_dash_ends_the_options(self):
        arguments = execution_arguments(['--', '-odd'], self.spec)
        self.assertEqual(arguments['playbook'], '-odd')

    def test_an_unknown_short_form_is_a_clean_error(self):
        with self.assertRaises(ReplError) as context:
            execution_arguments(['site', '-x'], self.spec)
        self.assertIn("'-x'", str(context.exception))
        self.assertIn('run -h', str(context.exception))

    def test_a_flag_takes_no_value(self):
        with self.assertRaises(ReplError) as context:
            execution_arguments(['site', '--list-tasks=yes'],
                                self.spec)
        self.assertIn('takes no value', str(context.exception))

    def test_the_help_lists_everything(self):
        text = _command_help(self.spec)
        self.assertIn('usage: run [-h] [-l SUBSET]', text)
        # (the usage wraps; the tail carries the positional)
        self.assertIn('[PLAYBOOK ...]', text)
        self.assertIn('positional arguments:', text)
        self.assertIn('options:', text)
        for form in ('-l, --limit SUBSET', '-t, --tags TAGS',
                     '--skip-tags TAGS', '-f, --forks N',
                     '--start-at-task NAME', '-u, --user NAME',
                     '--syntax-check', '--list-hosts', '--list-tasks',
                     '--list-tags', '--force-handlers',
                     '--flush-cache'):
            self.assertIn(form, text)
        # The opt-ins are marked as such
        self.assertIn('policy opt-in', text)

    def test_the_help_request_carries_the_text(self):
        with self.assertRaises(_HelpRequested) as context:
            execution_arguments(['-h'], self.spec)
        self.assertIn('usage: run', context.exception.text)

    def test_the_job_spec_adds_the_mode(self):
        text = _command_help(COMMAND_SPECS['job'])
        self.assertIn('usage: job add', text)
        self.assertIn('-m, --mode MODE', text)
        self.assertIn('BATCH', text)

    def test_the_completion_offers_the_option_forms(self):
        session = make_repl(FakeClient())[0]
        _word, found = _candidates_for(session, 'check site -')
        self.assertIn('-l', found)
        self.assertIn('--limit', found)
        self.assertIn('-h', found)
        _word, found = _candidates_for(session, 'job add site --')
        self.assertIn('--mode', found)
        self.assertIn('--limit', found)
        # ... but not after its other subcommands
        _word, found = _candidates_for(session, 'job prune --')
        self.assertEqual(found, [])

    def test_a_verbs_dash_h_prints_without_calling(self):
        session, out, _err = make_repl(FakeClient())
        session.execute('check -h')
        self.assertIn('usage: check', out.getvalue())
        self.assertEqual(session.client.calls, [])

    def test_help_takes_a_verb(self):
        session, out, err = make_repl(FakeClient())
        session.execute('help run')
        self.assertIn('usage: run', out.getvalue())
        # ... and a verb without a spec gets a note
        session.execute('help view')
        self.assertIn('no detailed help', err.getvalue())

    def test_job_add_takes_the_execution_short_forms(self):
        session, _out, _err = make_repl(FakeClient())
        session.client.queue(
                'tools/call',
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'queued'}]})
        session.execute('job add work site -l web1')
        call = session.client.calls[0][1]
        self.assertEqual(call['name'], 'job.add')
        self.assertEqual(call['arguments']['limit'], 'web1')
        self.assertEqual(call['arguments']['playbook'], 'site')


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
                         ('tools/call',
                          tool_params('project.status', {})))
        self.assertIn('Project: x', self.out.getvalue())

    def test_an_error_result_is_marked_and_counted(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text', 'text': 'denied'}]})
        self.repl.execute('status')
        self.assertIn('error: denied', self.err.getvalue())
        self.assertTrue(self.repl.had_error)

    def test_edit_refuses_in_a_confined_session(self):
        # The editor's own escapes would leave the confinement
        self.repl.confined = True
        with self.assertRaises(repl.ReplError) as context:
            self.repl.execute('edit playbooks/site.yml')
        self.assertIn('a confined session has no editor',
                      str(context.exception))

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
                'content': [{'type': 'text', 'text': 'switched'}],
                # The daemon's current view rides the '_meta' of
                # every result; the session follows it
                '_meta': {'org.debops/view': 'web'}})
        self.repl.execute('view web')
        self.assertEqual(self.repl.view, 'web')
        self.assertEqual(self.repl.prompt, 'debops project:web> ')
        self.assertEqual(self.repl._completion_cache, {})

    def test_a_failed_switch_keeps_the_view(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text', 'text': 'no such view'}],
                # A failed switch still carries the current view
                '_meta': {'org.debops/view': 'system'}})
        self.repl.execute('view web')
        self.assertEqual(self.repl.view, 'system')

    def test_the_session_follows_a_remote_view_switch(self):
        # Every answer's '_meta' carries the daemon's current view;
        # another client moved it — the session follows
        self.repl.cwd = 'inventory'
        self.repl._completion_cache['view'] = ['system']
        fired = []
        self.repl._history_changed = lambda: fired.append(True)
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}],
                '_meta': {'org.debops/view': 'web'}})
        self.repl.execute('status')
        self.assertEqual(self.repl.view, 'web')
        # The scope edge and the working directory moved, the
        # completion cache flushed, the history hook fired, and the
        # note said so
        self.assertEqual(self.repl.cwd, '')
        self.assertIsNone(self.repl._view_root_rel)
        self.assertEqual(self.repl._completion_cache, {})
        self.assertEqual(fired, [True])
        self.assertIn('the view is now web', self.err.getvalue())

    def test_an_unchanged_view_meta_is_a_no_op(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}],
                '_meta': {'org.debops/view': 'system'}})
        self.repl.execute('status')
        self.assertEqual(self.repl.view, 'system')
        self.assertNotIn('the view is now', self.err.getvalue())

    def test_a_result_without_the_view_meta_changes_nothing(self):
        # A legacy-shaped or foreign answer never moves the view
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}]})
        self.repl.execute('status')
        self.assertEqual(self.repl.view, 'system')
        self.assertNotIn('the view is now', self.err.getvalue())

    def test_the_session_declares_its_view_on_tool_calls(self):
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}]})
        self.repl.execute('status')
        self.assertEqual(self.client.calls[0][1]['_meta'],
                         {'org.debops/view': 'system'})

    def test_a_stale_view_refusal_moves_the_session(self):
        # The refusal carries the current view; the session follows
        # it, so the human's retry lands where they now look
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': True,
                'content': [{'type': 'text',
                             'text': 'the view is now web'}],
                '_meta': {'org.debops/view': 'web'}})
        self.repl.execute('status')
        self.assertIn('the view is now web', self.err.getvalue())
        self.assertEqual(self.repl.view, 'web')

    def test_a_timeout_says_busy_not_dead(self):
        # A slow daemon (a run holds the project lock) is not a dead
        # one; the client dropped the connection, the retry is fresh
        self.client.request = mock.Mock(
                side_effect=TimeoutError('no response within 30 '
                                         'seconds'))
        with self.assertRaises(ReplError) as context:
            self.repl.execute('status')
        self.assertIn('may be busy', str(context.exception))
        self.assertNotIn('is it running', str(context.exception))

    def test_a_stray_socket_error_is_a_line_not_a_traceback(self):
        # The timed-out reader's OSError must not kill the session
        self.client.request = mock.Mock(
                side_effect=OSError('cannot read from timed out '
                                    'object'))
        with self.assertRaises(ReplError) as context:
            self.repl.execute('status')
        self.assertIn('the daemon is not answering',
                      str(context.exception))

    def test_the_queue_verbs(self):
        self.client.queue(
                'tools/call',
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'Queue: park'}]},
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'created'}]},
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'queued'}]},
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'accepted'}]})
        self.repl.execute('queue')
        self.repl.execute('batch create work')
        self.repl.execute('job add work check site --limit web1')
        self.repl.execute('batch accept work')
        names = [call[1]['name'] for call in self.client.calls]
        self.assertEqual(names, ['queue.list', 'batch.create',
                                 'job.add', 'batch.pending'])
        job_call = self.client.calls[2][1]['arguments']
        self.assertEqual(job_call['batch'], 'work')
        self.assertEqual(job_call['mode'], 'check')
        self.assertEqual(job_call['playbook'], 'site')
        self.assertEqual(job_call['limit'], 'web1')
        # The review mark clears with pending=false
        self.assertEqual(self.client.calls[3][1]['arguments'],
                         {'name': 'work', 'pending': False})

    def test_the_queue_verbs_usage_errors(self):
        with self.assertRaises(ReplError):
            self.repl.execute('queue bogus')
        with self.assertRaises(ReplError):
            self.repl.execute('job del')

    def test_the_job_add_mode_is_a_positional_word(self):
        # UNIX-style: the mode word before the playbook, run by
        # default
        for line, expected in (
                ('job add work site', 'run'),
                ('job add work run site', 'run'),
                ('job add work check site', 'check'),
                ('job add work debug site', 'debug')):
            self.client.queue('tools/call', {
                    'resultType': 'complete', 'isError': False,
                    'content': [{'type': 'text', 'text': 'queued'}]})
            self.repl.execute(line)
            self.assertEqual(
                    self.client.calls[-1][1]['arguments']['mode'],
                    expected, line)
            self.assertEqual(
                    self.client.calls[-1][1]['arguments']['playbook'],
                    'site', line)

    def test_the_job_add_mode_option(self):
        # The -m/--mode option settles the mode from anywhere in the
        # line, and the mode word is then not consumed — a playbook
        # named 'check' stays a playbook
        for line, mode, playbooks in (
                ('job add work -m check site', 'check', 'site'),
                ('job add work --mode debug site', 'debug', 'site'),
                ('job add work site --mode=check', 'check', 'site'),
                ('job add work -m run check', 'run', 'check')):
            self.client.queue('tools/call', {
                    'resultType': 'complete', 'isError': False,
                    'content': [{'type': 'text', 'text': 'queued'}]})
            self.repl.execute(line)
            self.assertEqual(
                    self.client.calls[-1][1]['arguments']['mode'],
                    mode, line)
            self.assertEqual(
                    self.client.calls[-1][1]['arguments']['playbook'],
                    playbooks, line)

    def test_the_job_add_mode_option_validates(self):
        with self.assertRaises(ReplError) as context:
            self.repl.execute('job add work -m bogus site')
        self.assertIn("one of: 'check', 'run', 'debug'",
                      str(context.exception))
        with self.assertRaises(ReplError) as context:
            self.repl.execute('job add work -m')
        self.assertIn('needs a value', str(context.exception))
        self.assertEqual(self.client.calls, [])

    def test_the_job_add_mode_word_eats_a_same_named_playbook(self):
        # The keyword slot wins without the option; -m|--mode is the
        # way to name the mode of a playbook called 'check'
        with self.assertRaises(ReplError) as context:
            self.repl.execute('job add work check')
        self.assertIn('playbook target is required',
                      str(context.exception))
        self.assertEqual(self.client.calls, [])


class EditTestCase(unittest.TestCase):
    """The edit verb: the digest of the read preconditions the write,
    and a refusal keeps the edits as a local copy."""

    READ = {'resultType': 'complete', 'isError': False,
            'content': [{'type': 'text', 'text': '# site\n'}],
            'structuredContent': {'path': 'playbooks/site.yml',
                                  'content': 'old\n',
                                  'digest': 'sha256:aaaa'}}

    def make_session(self, write_result):
        client = FakeClient()
        client.queue('tools/call', dict(self.READ), write_result)
        session, out, err = make_repl(client, interactive=True)
        return client, session, out, err

    def edit_with(self, session, append='new\n'):
        """Run 'edit' with a scripted editor; the tmp path out."""
        written = []

        def fake_editor(path):
            written.append(path)
            if append is not None:
                with open(path, 'a') as fp:
                    fp.write(append)
            return 0

        session._run_editor = fake_editor
        session.execute('edit playbooks/site.yml')
        return written[0]

    def test_the_digest_preconditions_the_write(self):
        client, session, _out, _err = self.make_session(
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': 'overwritten'}]})
        tmp = self.edit_with(session)
        arguments = client.calls[1][1]['arguments']
        self.assertEqual(client.calls[1][1]['name'], 'file.write')
        self.assertEqual(arguments['digest'], 'sha256:aaaa')
        self.assertEqual(arguments['content'], 'old\nnew\n')
        # The scratch file is cleaned up on success
        self.assertFalse(os.path.exists(tmp))

    def test_a_refusal_keeps_the_local_copy(self):
        # The file moved while the editor was open: the write is
        # refused and the edits survive on the local disk
        client, session, _out, err = self.make_session(
                {'resultType': 'complete', 'isError': True,
                 'content': [{'type': 'text', 'text':
                              'the file changed since the read '
                              '(digest mismatch)'}]})
        tmp = self.edit_with(session)
        self.assertIn('digest mismatch', err.getvalue())
        self.assertIn('local copy', err.getvalue())
        with open(tmp) as fp:
            self.assertEqual(fp.read(), 'old\nnew\n')
        os.unlink(tmp)

    def test_unchanged_edits_clean_up(self):
        client, session, _out, err = self.make_session(
                {'resultType': 'complete', 'isError': False,
                 'content': [{'type': 'text', 'text': '?'}]})
        tmp = self.edit_with(session, append=None)
        self.assertIn('unchanged', err.getvalue())
        # No write call
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(os.path.exists(tmp))

    def test_an_older_daemon_writes_without_the_precondition(self):
        read = dict(self.READ)
        read['structuredContent'] = {'path': 'playbooks/site.yml',
                                     'content': 'old\n'}
        client = FakeClient()
        client.queue('tools/call', read, {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'overwritten'}]})
        session, _out, _err = make_repl(client, interactive=True)
        tmp = self.edit_with(session)
        arguments = client.calls[1][1]['arguments']
        self.assertNotIn('digest', arguments)
        self.assertFalse(os.path.exists(tmp))


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
                                 'structuredContent': {'exitCode': 0},
                                 'content': [{'type': 'text',
                                              'text': 'recap'}]}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute('check site')
        # The status tail moved to the error channel; the answer is
        # not re-rendered — the run was shown live and needs no
        # sendoff of its own (the banner is in the log)
        self.assertIn('PLAY [one]', err.getvalue())
        self.assertNotIn('recap', out.getvalue())

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

    def _run_task_script(self, repl_obj, polls, command='run site'):
        """Drive a task-backed execution through the queued polls."""
        client = repl_obj.client
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status': 'working'})
        client.queue('tasks/get', *polls)
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute(command)

    def test_the_tailed_run_is_not_shown_again(self):
        # The repl's tail: the moving status lines, then the rule and
        # the verdict — the finished log is not rendered twice
        client = FakeClient()
        repl_obj, out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'statusMessage': 'TASK [one]',
                 'run': {'mode': 'check', 'playbooks': ['site'],
                         'taskIndex': 1, 'currentTask': 'TASK [one]'}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': [{'type': 'text',
                                         'text': 'recap'}]}}],
                command='check site')
        self.assertIn('TASK [one]', err.getvalue())
        self.assertNotIn('recap', out.getvalue())

    def test_an_untailed_run_still_renders_its_result(self):
        # A run which completed before the first poll shows its
        # answer whole
        client = FakeClient()
        repl_obj, out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'completed',
                 'result': {'isError': False,
                            'content': [{'type': 'text',
                                         'text': 'recap'}]}}],
                command='check site')
        self.assertIn('recap', out.getvalue())

    def test_the_shell_tails_without_a_completion_marker(self):
        # The shell tails the log like the repl does and the toolbar
        # carries the live status; the verdict is in the log itself
        # (the recap, the sendoff), so nothing more prints
        client = FakeClient()
        repl_obj, out, err = make_repl(client)
        repl_obj.run_display = 'shell'
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'statusMessage': 'TASK [a]',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'taskIndex': 1, 'currentTask': 'TASK [a]'}},
                {'status': 'working',
                 'statusMessage': 'TASK [c]',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'taskIndex': 3, 'currentTask': 'TASK [c]'}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': [{'type': 'text',
                                         'text': 'recap'}]}}])
        text = err.getvalue()
        self.assertIn('TASK [a]', text)
        self.assertNotIn('OK', text)
        self.assertNotIn('recap', out.getvalue())
        self.assertIsNone(repl_obj.active_run)

    def test_a_failed_tailed_run_prints_no_excerpt(self):
        # The failure scrolled by in the tail; the session fails
        # without a re-dump
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        repl_obj.run_display = 'shell'
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status': 'working'})
        client.queue('tasks/get',
                     {'status': 'working',
                      'statusMessage': 'TASK [a]',
                      'run': {'mode': 'run', 'playbooks': ['site'],
                              'taskIndex': 1}},
                     {'status': 'failed',
                      'error': {'message': 'boom'}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute('run site')
        self.assertNotIn('boom', err.getvalue())
        self.assertTrue(repl_obj.had_error)
        self.assertIsNone(repl_obj.active_run)

    def test_a_cancelled_tailed_run_prints_no_excerpt(self):
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        repl_obj.run_display = 'shell'
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status': 'working'})
        client.queue('tasks/get',
                     {'status': 'working',
                      'statusMessage': 'TASK [a]',
                      'run': {'mode': 'run', 'playbooks': ['site'],
                              'taskIndex': 1}},
                     {'status': 'cancelled',
                      'error': {'message': 'cancelled'}})
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute('run site')
        self.assertNotIn('cancelled', err.getvalue())
        self.assertTrue(repl_obj.had_error)

    def test_the_wait_retries_a_dropped_busy_connection(self):
        # A run holds the daemon busy; a timed-out poll drops the
        # connection and the wait continues on the next one
        client = FakeClient()
        repl_obj, _out, _err = make_repl(client)
        client.queue('tools/call', {'resultType': 'task',
                                    'taskId': 't1', 'status': 'working'})
        client.queue('tasks/get',
                     {'status': 'completed',
                      'result': {'isError': False,
                                 'structuredContent': {'exitCode': 0},
                                 'content': []}})
        original = client.request
        state = {'dropped': False}

        def request(method, params):
            if method == 'tasks/get' and not state['dropped']:
                state['dropped'] = True
                raise TimeoutError('slow')
            return original(method, params)

        client.request = request
        with mock.patch('debops.mcp.repl.POLL_INTERVAL', 0):
            repl_obj.execute('run site')
        self.assertTrue(state['dropped'])
        self.assertFalse(repl_obj.had_error)

    def test_the_tail_starts_when_the_run_info_lands(self):
        # The 'preparing' publish carries no execution id; the
        # prelude and the tail start when the run's own bookkeeping
        # arrives, and the tail's replay covers the gap
        client = FakeClient()
        client.tail = FakeTailConnection(['ok: [localhost]'])
        repl_obj, out, _err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'phase': 'preparing'}},
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1,
                         'playbookPaths': ['ansible/playbooks'
                                           '/site.yml']}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        self.assertEqual(client.tail.subscriptions, [{'runLog': 7}])
        text = out.getvalue()
        self.assertIn('Executing Ansible playbooks:', text)
        self.assertIn('ok: [localhost]', text)
        self.assertLess(text.index('Executing Ansible playbooks:'),
                        text.index('ok: [localhost]'))

    def test_the_run_tails_its_log(self):
        # The run's own output streams on the answer channel, the
        # status digest is superseded, the marker closes it out
        client = FakeClient()
        client.tail = FakeTailConnection(['ok: [localhost]',
                                          'changed: [localhost]'])
        repl_obj, out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'statusMessage': 'TASK [one]',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': [{'type': 'text',
                                         'text': 'recap'}]}}])
        self.assertIn('ok: [localhost]', out.getvalue())
        self.assertIn('changed: [localhost]', out.getvalue())
        self.assertNotIn('TASK [one]', err.getvalue())
        self.assertNotIn('recap', out.getvalue())
        self.assertTrue(client.tail.closed)
        self.assertEqual(client.tail.subscriptions, [{'runLog': 7}])

    def test_the_run_opens_with_the_playbook_prelude(self):
        # The plain CLI's 'Executing Ansible playbooks:' list, the
        # paths through the presentation filter, before the tail
        client = FakeClient()
        client.tail = FakeTailConnection(['ok: [localhost]'])
        repl_obj, out, _err = make_repl(client)
        home = os.path.expanduser('~')
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7,
                         'playbookPaths': [os.path.join(
                                 home, 'projects', 'x', 'site.yml')]}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        text = out.getvalue()
        self.assertLess(text.index('Executing Ansible playbooks:'),
                        text.index('ok: [localhost]'))
        self.assertIn('~/projects/x/site.yml', text)

    def test_the_tail_is_colored_on_a_terminal(self):
        client = FakeClient()
        client.tail = FakeTailConnection(
                ['TASK [one]', 'ok: [localhost]', 'changed: [x]'])
        repl_obj, out, _err = make_repl(client, interactive=True)
        out.isatty = lambda: True
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        text = out.getvalue()
        # ansible's own defaults: ok green, changed yellow, the
        # banner plain
        self.assertIn('\x1b[0;32mok: [localhost]\x1b[0m', text)
        self.assertIn('\x1b[0;33mchanged: [x]\x1b[0m', text)
        self.assertIn('TASK [one]\n', text)

    def test_the_tail_stays_plain_in_a_pipe(self):
        client = FakeClient()
        client.tail = FakeTailConnection(['ok: [localhost]'])
        repl_obj, out, _err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        self.assertNotIn('\x1b[', out.getvalue())

    def test_a_legacy_client_keeps_the_status_digest(self):
        # The 2025 era has no streams; the digest prints as before
        client = FakeClient()
        client.legacy = True
        repl_obj, _out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'statusMessage': 'TASK [one]',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        self.assertIn('TASK [one]', err.getvalue())
        self.assertIsNone(repl_obj._tail_client)

    def test_log_follow_tails_the_execution(self):
        client = FakeClient()
        client.tail = FakeTailConnection(['one', 'two'])
        session, out, _err = make_repl(client)
        session.execute('log 7 --follow')
        self.assertEqual(out.getvalue(), 'one\ntwo\n')
        self.assertEqual(client.tail.subscriptions, [{'runLog': 7}])
        self.assertTrue(client.tail.closed)

    def test_log_follow_ctrl_c_keeps_the_run(self):
        class Interrupting(FakeTailConnection):
            def request_streaming(self, method, params):
                self.subscriptions.append(params['notifications'])
                yield {'method': 'notifications/runs/log',
                       'params': {'line': 'one'}}
                raise KeyboardInterrupt()

        client = FakeClient()
        client.tail = Interrupting()
        session, out, err = make_repl(client)
        session.execute('log 7 -f')
        self.assertIn('one', out.getvalue())
        self.assertIn('the tail ends', err.getvalue())
        self.assertFalse(session.had_error)

    def test_log_follow_needs_a_stream_capable_era(self):
        client = FakeClient()
        client.legacy = True
        session, _out, _err = make_repl(client)
        with self.assertRaises(ReplError) as context:
            session.execute('log 7 -f')
        self.assertIn('2025-era', str(context.exception))

    def test_a_tailed_failure_prints_no_excerpt(self):
        # The failure scrolled by in the tail and the sendoff banner
        # named the code; nothing re-dumps, the session still fails
        client = FakeClient()
        client.tail = FakeTailConnection(['fatal: [web1]: FAILED!'])
        repl_obj, out, _err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'executionId': 7, 'taskIndex': 1}},
                {'status': 'completed',
                 'result': {'isError': True,
                            'content': [{'type': 'text',
                                         'text': 'the excerpt'}]}}])
        self.assertIn('fatal: [web1]', out.getvalue())
        self.assertNotIn('the excerpt', out.getvalue())
        self.assertTrue(repl_obj.had_error)

    def test_an_untailed_failure_keeps_the_excerpt(self):
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'completed',
                 'result': {'isError': True,
                            'content': [{'type': 'text',
                                         'text': 'the excerpt'}]}}])
        self.assertIn('error: the excerpt', err.getvalue())
        self.assertTrue(repl_obj.had_error)

    def test_the_shell_acknowledges_the_request(self):
        # The daemon's answer is the task handle arriving; the shell
        # says so before the prompt goes quiet for the run
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        repl_obj.run_display = 'shell'
        self._run_task_script(repl_obj, [
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        self.assertIn('request received, processing',
                      err.getvalue())

    def test_the_repl_does_not_acknowledge(self):
        # The repl's own flow is answer-shaped already
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        self._run_task_script(repl_obj, [
                {'status': 'completed',
                 'result': {'isError': False,
                            'structuredContent': {'exitCode': 0},
                            'content': []}}])
        self.assertNotIn('request received', err.getvalue())

    def test_the_console_tracks_the_run_without_printing(self):
        # The status lines read the session; nothing prints per poll
        client = FakeClient()
        repl_obj, _out, err = make_repl(client)
        repl_obj.run_display = 'lines'
        invalidations = []
        repl_obj._invalidate = lambda: invalidations.append(1)
        self._run_task_script(repl_obj, [
                {'status': 'working',
                 'run': {'mode': 'run', 'playbooks': ['site'],
                         'taskIndex': 1, 'currentTask': 'TASK [a]'}},
                {'status': 'completed',
                 'result': {'isError': False,
                            'content': [{'type': 'text',
                                         'text': 'recap'}]}}])
        self.assertNotIn('TASK [a]', err.getvalue())
        # The chrome repainted during the run and on its fold
        self.assertTrue(len(invalidations) >= 2)
        self.assertIsNone(repl_obj.active_run)

    def test_the_duration_format(self):
        self.assertEqual(repl._format_duration(30), '0:30')
        self.assertEqual(repl._format_duration(245), '4:05')
        self.assertEqual(repl._format_duration(3725), '1:02:05')

    def test_the_run_verb_of_a_run_dict(self):
        # The phase speaks while the run prepares; the mode's verb
        # once it runs
        self.assertEqual(repl._run_verb_of({'phase': 'preparing',
                                            'mode': 'check'}),
                         'preparing')
        self.assertEqual(repl._run_verb_of({'mode': 'check'}),
                         'Checking')
        self.assertEqual(repl._run_verb_of({'mode': 'run'}), 'Running')
        self.assertEqual(repl._run_verb_of({'mode': 'debug'}),
                         'Debugging')
        # A run which cannot say its mode yet is preparing (a
        # foreign run inside the daemon's probe window)
        self.assertEqual(repl._run_verb_of({}), 'Preparing')

    def test_the_bang_runs_a_local_command(self):
        # The '!' escape: the client-local shell answers, the daemon
        # never hears about it
        session, out, _err = make_repl(FakeClient())
        session.execute('!printf hello-local')
        self.assertEqual(out.getvalue(), 'hello-local')
        self.assertEqual(session.client.calls, [])

    def test_the_bang_needs_a_command(self):
        session, _out, _err = make_repl(FakeClient())
        with self.assertRaises(ReplError):
            session.execute('!')

    def test_the_bang_refuses_in_a_confined_session(self):
        # The confinement of the account: no local escapes, the
        # editor's rule
        session, _out, _err = make_repl(FakeClient())
        session.confined = True
        with self.assertRaises(ReplError) as context:
            session.execute('!ls')
        self.assertIn('no local shell', str(context.exception))

    def test_the_bang_notes_a_nonzero_exit(self):
        session, _out, err = make_repl(FakeClient())
        session.execute('!false')
        self.assertIn('exit 1', err.getvalue())

    def test_the_debops_prefix_strips_before_a_known_verb(self):
        client = FakeClient()
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'Project: x'}]})
        session, out, _err = make_repl(client)
        session.execute('debops status')
        self.assertEqual(client.calls[0][1]['name'], 'project.status')
        self.assertIn('Project: x', out.getvalue())

    def test_the_debops_prefix_strips_before_a_tool_hatch(self):
        client = FakeClient()
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'ok'}]})
        session, out, _err = make_repl(client)
        session.execute('debops tool/project.status')
        self.assertEqual(client.calls[0][1]['name'], 'project.status')

    def test_an_unknown_verb_after_the_prefix_keeps_it(self):
        session, _out, _err = make_repl(FakeClient())
        with self.assertRaises(ReplError) as context:
            session.execute('debops stats')
        self.assertIn('drop the prefix', str(context.exception))


class ColorizerTestCase(unittest.TestCase):
    """The ansible output, colored the way ansible colors
    a terminal."""

    def test_the_line_classes(self):
        classify = repl.classify_ansible_line
        self.assertEqual(classify('ok: [web1]'), 'ok')
        self.assertEqual(classify('changed: [web1]'), 'changed')
        self.assertEqual(classify('fatal: [web1]: FAILED!'), 'failed')
        self.assertEqual(classify('failed: [web1]'), 'failed')
        self.assertEqual(classify('unreachable: [web1]'),
                         'unreachable')
        self.assertEqual(classify('skipping: [web1]'), 'skipped')
        self.assertEqual(classify('TASK [nginx : Install] ***'),
                         'header')
        self.assertEqual(classify('PLAY RECAP ***'), 'header')
        self.assertEqual(classify('PLAYBOOK RECAP ***'), 'header')
        self.assertEqual(classify('[WARNING]: watch out'), 'warning')
        self.assertEqual(classify('[DEPRECATION WARNING]: old'),
                         'deprecation')
        self.assertEqual(classify('[ERROR]: nope'), 'error')
        self.assertEqual(classify('web1 : ok=3 changed=1'),
                         'recap')
        self.assertEqual(classify('just some output'), 'plain')

    def test_the_default_theme_mirrors_ansible(self):
        colorize = repl.AnsibleColorizer()
        self.assertEqual(colorize.colorize('ok: [h]'),
                         '\x1b[0;32mok: [h]\x1b[0m')
        self.assertEqual(colorize.colorize('changed: [h]'),
                         '\x1b[0;33mchanged: [h]\x1b[0m')
        self.assertEqual(colorize.colorize('fatal: [h]: FAILED!'),
                         '\x1b[0;31mfatal: [h]: FAILED!\x1b[0m')
        self.assertEqual(colorize.colorize('unreachable: [h]'),
                         '\x1b[1;31munreachable: [h]\x1b[0m')
        self.assertEqual(colorize.colorize('skipping: [h]'),
                         '\x1b[0;36mskipping: [h]\x1b[0m')
        self.assertEqual(colorize.colorize('[WARNING]: x'),
                         '\x1b[1;35m[WARNING]: x\x1b[0m')
        self.assertEqual(colorize.colorize('[DEPRECATION WARNING]: x'),
                         '\x1b[0;35m[DEPRECATION WARNING]: x\x1b[0m')
        # The banners and the recap line are uncolored upstream
        self.assertEqual(colorize.colorize('TASK [x] ***'),
                         'TASK [x] ***')

    def test_the_recap_counts_color_by_value(self):
        colorize = repl.AnsibleColorizer()
        line = 'web1 : ok=3 changed=2 unreachable=0 failed=1'
        text = colorize.colorize(line)
        self.assertIn('ok=3', text)
        self.assertIn('\x1b[0;33mchanged=2\x1b[0m', text)
        self.assertIn('unreachable=0', text)
        self.assertIn('\x1b[0;31mfailed=1\x1b[0m', text)

    def test_the_diff_block_colors_until_it_ends(self):
        colorize = repl.AnsibleColorizer()
        self.assertEqual(colorize.colorize('--- before'),
                         '\x1b[0;36m--- before\x1b[0m')
        self.assertEqual(colorize.colorize('+added'),
                         '\x1b[0;32m+added\x1b[0m')
        self.assertEqual(colorize.colorize('-removed'),
                         '\x1b[0;31m-removed\x1b[0m')
        self.assertEqual(colorize.colorize('ok: [h]'),
                         '\x1b[0;32mok: [h]\x1b[0m')
        # The block ended at the result line; a stray plus stays plain
        self.assertEqual(colorize.colorize('+ not a diff'),
                         '+ not a diff')

    def test_a_theme_overrides_the_map(self):
        colorize = repl.AnsibleColorizer({'ok': '1;32'})
        self.assertEqual(colorize.colorize('ok: [h]'),
                         '\x1b[1;32mok: [h]\x1b[0m')
        # A class missing from the theme stays plain
        self.assertEqual(colorize.colorize('changed: [h]'),
                         'changed: [h]')

    def test_the_retry_line_is_dark_gray(self):
        # ansible's own: the retry counter prints with COLOR_DEBUG
        colorize = repl.AnsibleColorizer()
        line = ('FAILED - RETRYING: [kometa]: Download custom APT '
                'keys (3 retries left).')
        self.assertEqual(repl.classify_ansible_line(line), 'retry')
        self.assertEqual(colorize.colorize(line),
                         '\x1b[1;30m' + line + '\x1b[0m')
        # ... and the next one is its own line, not a sticky block
        self.assertEqual(repl.classify_ansible_line('ok: [kometa]'),
                         'ok')

    def test_a_warning_block_keeps_its_color_until_the_next_marker(self):
        # The multi-line blocks color whole: the deprecation
        # warning's explanation lines stay purple until the next
        # recognized line class
        colorize = repl.AnsibleColorizer()
        first = '[DEPRECATION WARNING]: DEFAULT_MANAGED_STR option.'
        second = ('Alternatives: Set the `ansible_managed` variable, '
                  'or use any custom variable.')
        third = 'TASK [core : Save local facts] ' + '*' * 20
        self.assertEqual(colorize.colorize(first),
                         '\x1b[0;35m' + first + '\x1b[0m')
        self.assertEqual(colorize.colorize(second),
                         '\x1b[0;35m' + second + '\x1b[0m')
        # The header resets the stickiness (and stays plain itself)
        self.assertEqual(colorize.colorize(third), third)
        after = 'ok: [kometa]'
        self.assertEqual(colorize.colorize(after),
                         '\x1b[0;32m' + after + '\x1b[0m')

    def test_a_plain_line_without_a_block_stays_plain(self):
        colorize = repl.AnsibleColorizer()
        self.assertEqual(colorize.colorize('just some output'),
                         'just some output')

    def test_the_recap_colors_every_nonzero_field_and_the_host(self):
        # ansible's own rules: ok green, changed yellow, unreachable
        # bright red, failed red, skipped cyan, rescued green,
        # ignored bright purple; the host takes its outcome's face
        colorize = repl.AnsibleColorizer()
        line = ('web1 : ok=2 changed=1 unreachable=0 failed=0 '
                'skipped=3 rescued=1 ignored=2')
        text = colorize.colorize(line)
        self.assertIn('\x1b[0;33mweb1\x1b[0m', text)  # changed host
        self.assertIn('ok=2', text)
        self.assertIn('\x1b[0;33mchanged=1\x1b[0m', text)
        self.assertIn('unreachable=0', text)
        self.assertIn('failed=0', text)
        self.assertIn('\x1b[0;36mskipped=3\x1b[0m', text)
        self.assertIn('\x1b[0;32mrescued=1\x1b[0m', text)
        self.assertIn('\x1b[1;35mignored=2\x1b[0m', text)

    def test_the_recap_host_turns_red_on_a_failure(self):
        colorize = repl.AnsibleColorizer()
        text = colorize.colorize(
                'web1 : ok=2 changed=0 unreachable=0 failed=1')
        self.assertIn('\x1b[0;31mweb1\x1b[0m', text)
        self.assertIn('\x1b[0;31mfailed=1\x1b[0m', text)
        self.assertIn('ok=2', text)

    def test_the_banners_repad_to_the_clients_width(self):
        # The run's log keeps its recorded width; the live display
        # pretends the local terminal's
        banner = 'TASK [core : Save local facts] ' + '*' * 20
        colorize = repl.AnsibleColorizer(width=lambda: 40)
        text = colorize.colorize(banner)
        self.assertEqual(len(text), 40)
        self.assertTrue(text.startswith('TASK [core : Save local '
                                        'facts] '))
        self.assertTrue(text.endswith('*'))
        # No width, no repad — the recorded line passes through
        plain = repl.AnsibleColorizer()
        self.assertEqual(plain.colorize(banner), banner)
        # The star run is dropped when the text alone overflows
        # (ansible's own format keeps the separator space)
        narrow = repl.AnsibleColorizer(width=lambda: 10)
        self.assertEqual(narrow.colorize(banner),
                         'TASK [core : Save local facts] ')
        # Only the banner shapes repad; a plain line keeps its stars
        self.assertEqual(colorize.colorize('a note ***'),
                         'a note ***')


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

    def test_the_daemon_view_is_learned_from_the_probe(self):
        # The discover answer's '_meta' carries the daemon's current
        # view; the client surfaces it for the session start
        answer = {'result': {
                'resultType': 'complete', 'supportedVersions': [],
                '_meta': {'org.debops/view': 'admin'}}}
        with mock.patch('debops.mcp.repl.UnixClient',
                        return_value=self.fake_client(answer)):
            connected = repl.connect_or_spawn(self.socket, None)
        self.assertEqual(connected.server_view, 'admin')

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
    """The 'mcp stop' guardrails: the pid comes from the sidecar and
    is correlated against /proc before any signal."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-stop-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.socket = os.path.join(self.tmpdir, 'x.sock')

    def _pid_file(self, *lines):
        with open(repl.socket_pid_path(self.socket), 'w') as fp:
            fp.write('\n'.join(lines) + '\n')

    def test_no_pid_file(self):
        with self.assertRaises(ReplError) as context:
            repl.stop_daemon(self.socket)
        self.assertIn('no pid file', str(context.exception))

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

    def test_a_daemon_stops_with_sigterm(self):
        self._pid_file('4242', 'spawned')
        out = io.StringIO()
        kills = []

        def fake_kill(pid, signal):
            kills.append(signal)
            # The daemon cleans its sidecar up on the way out
            os.unlink(repl.socket_pid_path(self.socket))

        with mock.patch.object(repl, '_daemon_pid',
                               return_value=(4242, 'spawned')), \
                mock.patch('os.kill', fake_kill):
            repl.stop_daemon(self.socket, out=out)
        self.assertEqual(kills, [15])
        self.assertIn('stopped', out.getvalue())
        # The report names the pid and the origin
        self.assertIn('4242', out.getvalue())
        self.assertIn('spawned', out.getvalue())

    def test_the_kill_option_is_sigkill_without_the_drain(self):
        self._pid_file('4242', 'hand-started')
        out = io.StringIO()
        kills = []
        with mock.patch.object(repl, '_daemon_pid',
                               return_value=(4242, 'hand-started')), \
                mock.patch('os.kill',
                           lambda pid, signal: kills.append(signal)):
            repl.stop_daemon(self.socket, out=out, kill=True)
        self.assertEqual(kills, [9])
        self.assertIn('hand-started', out.getvalue())
        self.assertIn('was killed', out.getvalue())
        # No drain, no cleanup: the sidecar stays for the next
        # daemon to reap
        self.assertTrue(os.path.exists(repl.socket_pid_path(
                self.socket)))


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

    def test_the_prune_keeps_the_newest_records_with_their_bytes(self):
        os.makedirs(os.path.dirname(self.path))
        content = ('\n# 2026-10-20 09:00:00.000000\n+first\n'
                   '\n# 2026-10-21 09:00:00.000000\n'
                   '+second one\n+second two\n'
                   '\n# 2026-10-22 09:00:00.000000\n+third\n')
        with open(self.path, 'w') as fp:
            fp.write(content)
        repl._prune_history_file(self.path, 2)
        with open(self.path) as fp:
            pruned = fp.read()
        # The survivors keep their bytes — timestamps and the
        # multiline record's shape included
        self.assertEqual(pruned, content.split('+first\n', 1)[1])
        self.assertNotIn('first', pruned)
        self.assertIn('+second one\n+second two\n', pruned)
        self.assertEqual(repl._read_history_strings(self.path),
                         ['second one\nsecond two', 'third'])

    def test_the_prune_handles_the_plain_format(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as fp:
            fp.write('a\nb\nc\n')
        repl._prune_history_file(self.path, 2)
        with open(self.path) as fp:
            self.assertEqual(fp.read(), 'b\nc\n')

    def test_a_file_under_the_cap_is_untouched(self):
        os.makedirs(os.path.dirname(self.path))
        content = '\n# 2026-10-20 09:00:00.000000\n+first\nplain\n'
        with open(self.path, 'w') as fp:
            fp.write(content)
        repl._prune_history_file(self.path, 10)
        with open(self.path) as fp:
            self.assertEqual(fp.read(), content)

    def test_the_plain_loop_prunes_after_an_append(self):
        fake_readline = mock.MagicMock()
        client = FakeClient()
        session, _out, _err = make_repl(client, interactive=True)
        session.history_max = 2
        with mock.patch.dict(sys.modules, {'readline': fake_readline}), \
                mock.patch('builtins.input',
                           side_effect=['pwd', 'help', 'pwd',
                                        EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertEqual(repl._read_history_strings(
                session.history_file), ['help', 'pwd'])

    def test_the_history_bound_comes_from_the_ambient_config(self):
        self.assertEqual(repl.history_max_entries({}), 10000)
        self.assertEqual(repl.history_max_entries(
                {'mcp': {'history': {'max_entries': 500}}}), 500)
        # Zero means unbounded, as with the run timeouts
        self.assertEqual(repl.history_max_entries(
                {'mcp': {'history': {'max_entries': 0}}}), 0)
        self.assertEqual(repl.history_max_entries(
                {'mcp': {'history': {'max_entries': -5}}}), 0)
        self.assertEqual(repl.history_max_entries(
                {'mcp': {'history': {'max_entries': 'lots'}}}), 10000)

    def test_the_bounded_history_prunes_on_append(self):
        try:
            from prompt_toolkit.history import FileHistory
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        client = FakeClient()
        session, _out, _err = make_repl(client)
        session.history_max = 2
        history = session._new_history()
        self.assertIsInstance(history, FileHistory)
        self.assertEqual(history.filename, session.history_file)
        for command in ('one', 'two', 'three'):
            history.store_string(command)
        self.assertEqual(
                repl._read_history_strings(history.filename),
                ['two', 'three'])

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
                                    'isError': False, 'content': [],
                                    '_meta': {'org.debops/view':
                                              'admin'}})
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
                                    'isError': False, 'content': [],
                                    '_meta': {'org.debops/view':
                                              'admin'}})
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
                                    'isError': False, 'content': [],
                                    '_meta': {'org.debops/view':
                                              'admin'}})
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

    def test_the_pipe_flushes_every_answer(self):
        # A driver's read waits for the answer; a block-buffered pipe
        # would hold it back otherwise
        client = FakeClient()
        session, out, err = make_repl(client)
        flushes = []
        out.flush = lambda: flushes.append(True)
        err.flush = lambda: None
        repl.run_pipe(session, 'help\nhelp')
        self.assertEqual(len(flushes), 2)

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

    def test_the_plain_flag_forces_the_plain_reader(self):
        # Even with a frontend named and prompt_toolkit available
        client = FakeClient()
        with mock.patch.object(repl, 'run_interactive_plain',
                               return_value=0) as plain:
            code = repl.run(client, plain=True, interactive=True,
                            frontend='console')
        self.assertEqual(code, 0)
        plain.assert_called_once()
        # ... and the session is not the console's
        session = plain.call_args[0][0]
        self.assertIs(type(session), repl.Repl)


class SpawnOptionsTestCase(unittest.TestCase):
    """The 'project.autospawn_options' mapping as serve tokens."""

    def test_scalars_booleans_and_the_underscore_fold(self):
        tokens, dropped = repl.spawn_options_tokens({
                'socket_mode': '0600', 'max_connections': 8,
                'log': True, 'stdio': False, 'view_lock': False})
        self.assertEqual(tokens, ['--log', '--max-connections', '8',
                                  '--socket-mode', '0600'])
        self.assertEqual(dropped, ['stdio', 'view-lock'])

    def test_the_spawn_machinery_keys_are_dropped(self):
        tokens, dropped = repl.spawn_options_tokens({
                'spawn': True, 'no_spawn': True, 'socket': '/x.sock',
                'detach': True, 'docs': True, 'view': 'admin',
                'idle_timeout': 60, 'instance': 'i2',
                'instance_root': '/tmp'})
        self.assertEqual(tokens, [])
        self.assertEqual(sorted(dropped),
                         ['detach', 'docs', 'idle-timeout',
                          'instance', 'instance-root', 'no-spawn',
                          'socket', 'spawn', 'view'])

    def test_an_unusable_value_is_a_config_error(self):
        with self.assertRaises(ValueError) as context:
            repl.spawn_options_tokens({'log': ['/a', '/b']})
        self.assertIn('autospawn_options', str(context.exception))


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
        # The verbs complete at the start of the line; a finished
        # word carries the trailing space of the shell habit, shown
        # only once the line moves on (the display stays clean)
        found = [entry.text for entry in completer.get_completions(
                Document('sta'), None)]
        self.assertIn('status ', found)
        # The execution options complete after the target
        found = [entry.text for entry in completer.get_completions(
                Document('check site --l'), None)]
        self.assertIn('--limit ', found)
        self.assertIn('--list-hosts ', found)

    def test_a_directory_candidate_stays_open(self):
        # No trailing space on a directory: the path below it
        # continues the completion
        try:
            from prompt_toolkit.document import Document
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        session, _out, _err = make_repl(FakeClient())
        completer = repl._Completer(session)
        with mock.patch.object(
                repl, '_candidates_for',
                return_value=('doc', ['docs/', 'docker-compose.yml'])):
            found = list(completer.get_completions(Document('doc'),
                                                   None))
        by_text = {entry.text: entry for entry in found}
        self.assertIn('docs/', by_text)
        self.assertIn('docker-compose.yml ', by_text)
        self.assertEqual(by_text['docs/'].display_text, 'docs/')
        self.assertEqual(by_text['docker-compose.yml '].display_text,
                         'docker-compose.yml')

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

    def test_the_readline_adapter_completes_the_verb(self):
        # The plain reader's protocol: (text, state) over the word
        # under the cursor
        client = FakeClient()
        session, _out, _err = make_repl(client)
        fake_readline = mock.MagicMock()
        fake_readline.get_line_buffer.return_value = 'st'
        fake_readline.get_begidx.return_value = 0
        complete = repl._readline_complete(fake_readline, session)
        self.assertEqual(complete('st', 0), 'status')
        self.assertIsNone(complete('st', 1))

    def test_the_readline_adapter_completes_paths(self):
        client = FakeClient()
        session, _out, _err = make_repl(client)
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'listing'}],
                'structuredContent': [
                        {'name': 'inventory', 'type': 'dir'},
                        {'name': 'note.txt', 'type': 'file'}]})
        fake_readline = mock.MagicMock()
        fake_readline.get_line_buffer.return_value = 'cd in'
        fake_readline.get_begidx.return_value = 3
        complete = repl._readline_complete(fake_readline, session)
        # 'cd' completes directories only
        self.assertEqual(complete('in', 0), 'inventory/')
        self.assertIsNone(complete('in', 1))

    def test_the_plain_loop_wires_the_readline_completion(self):
        tmp_history_home(self)
        fake_readline = mock.MagicMock()
        client = FakeClient()
        session, _out, _err = make_repl(client, interactive=True)
        with mock.patch.dict(sys.modules, {'readline': fake_readline}), \
                mock.patch('builtins.input', side_effect=[EOFError()]):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        fake_readline.set_completer_delims.assert_called_once_with(
                ' \t\n')
        self.assertTrue(fake_readline.set_completer.called)
        fake_readline.parse_and_bind.assert_called_once_with(
                'tab: complete')

    def test_suspend_enabled_unless_confined(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        for confined, expected in ((False, True), (True, False)):
            client = FakeClient()
            session, _out, _err = make_repl(client)
            session.confined = confined
            with mock.patch('prompt_toolkit.PromptSession') as pt:
                pt.return_value.prompt.side_effect = EOFError()
                code = repl.run_interactive(session)
            self.assertEqual(code, 0)
            _args, kwargs = pt.call_args
            self.assertIs(kwargs['enable_suspend'], expected)
            self.assertIn('key_bindings', kwargs)

    def test_the_suspend_bindings_note_the_confinement(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        client = FakeClient()
        session, _out, err = make_repl(client)
        session.confined = True
        bindings = repl._key_bindings(session)
        binding = bindings.bindings[0]
        self.assertEqual(binding.keys, ('c-z',))
        # The note binding is active only in a confined session;
        # a free one falls through to pt's own suspend binding
        self.assertTrue(binding.filter())
        binding.handler(mock.MagicMock())
        self.assertIn('a confined session does not background',
                      err.getvalue())
        session.confined = False
        self.assertFalse(binding.filter())

    def _tab_binding(self, session):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        bindings = repl._key_bindings(session)
        return [binding for binding in bindings.bindings
                if binding.keys == ('c-i',)][0]

    def _tab_event(self, document_text, candidates):
        from prompt_toolkit.completion import Completion
        from prompt_toolkit.document import Document
        buffer = mock.Mock()
        buffer.document = Document(document_text)
        buffer.completer.get_completions.return_value = [
                Completion(candidate, start_position=-len(document_text))
                for candidate in candidates]
        return mock.Mock(current_buffer=buffer), buffer

    def test_the_tab_binding_inserts_the_single_candidate(self):
        session, _out, _err = make_repl(FakeClient())
        binding = self._tab_binding(session)
        event, buffer = self._tab_event('stat', ['status'])
        binding.handler(event)
        buffer.delete_before_cursor.assert_called_once_with(4)
        buffer.insert_text.assert_called_once_with('status')

    def test_the_tab_binding_extends_the_common_prefix(self):
        session, _out, _err = make_repl(FakeClient())
        binding = self._tab_binding(session)
        event, buffer = self._tab_event('pl', ['playbook', 'playbooks'])
        binding.handler(event)
        buffer.delete_before_cursor.assert_not_called()
        buffer.insert_text.assert_called_once_with('aybook')

    def test_the_tab_binding_lists_the_ambiguous_candidates(self):
        session, _out, _err = make_repl(FakeClient())
        binding = self._tab_binding(session)
        event, buffer = self._tab_event('p', ['pull', 'push', 'pwd'])
        with mock.patch('sys.stdout', new=io.StringIO()) as out:
            binding.handler(event)
        buffer.insert_text.assert_not_called()
        self.assertIn('pull', out.getvalue())
        self.assertIn('push', out.getvalue())

    def test_the_pager_keys_do_not_recall_the_history(self):
        # PgUp/PgDn suggest a scrollback the inline prompt cannot
        # move (it is the terminal's own); they are swallowed, the
        # history stays on the arrows. The shift/control variants
        # too — an unbound key would self-insert its raw escape
        # sequence into the input line
        session, _out, _err = make_repl(FakeClient())
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        bindings = repl._key_bindings(session)
        for key in ('pageup', 'pagedown', 's-pageup', 's-pagedown',
                    'c-pageup', 'c-pagedown', 'c-s-pageup',
                    'c-s-pagedown'):
            binding = [b for b in bindings.bindings
                       if b.keys == (key,)][0]
            event = mock.MagicMock()
            binding.handler(event)
            event.app.assert_not_called()
            event.current_buffer.assert_not_called()


class ShellTestCase(unittest.TestCase):
    """The shell front end: the persistent app with the toolbar, the
    worker-submitted commands, the run status in the status line."""

    def setUp(self):
        try:
            import prompt_toolkit  # noqa: F401
        except ImportError:
            self.skipTest('prompt_toolkit is not installed')
        from debops.mcp import shell
        self.shell = shell
        self.session, self.out, self.err = make_repl(FakeClient())

    def test_build_shell_sets_the_live_flags(self):
        self.shell._build_shell(self.session)
        self.assertTrue(self.session.gate_input)
        self.assertEqual(self.session.run_display, 'shell')
        self.assertIn('aborts a run', self.session.busy_note)

    def test_enter_submits_to_the_worker(self):
        self.shell._build_shell(self.session)
        buffer = self.session._input_buffer
        with mock.patch.object(self.session, 'submit') as submit:
            buffer.text = 'status'
            buffer.validate_and_handle()
        submit.assert_called_once_with('status')
        self.assertEqual(buffer.text, '')
        # ... and the command is in the history
        self.assertEqual(buffer.history.get_strings(), ['status'])

    def test_the_gate_answer_skips_the_history(self):
        # A passphrase typed at the gate never enters the history
        self.shell._build_shell(self.session)
        buffer = self.session._input_buffer
        self.session._gate = {'secret': True,
                              'answered': threading.Event(),
                              'value': None}
        buffer.text = 'supersecret'
        with mock.patch.object(self.session, 'submit') as submit:
            buffer.validate_and_handle()
        submit.assert_called_once_with('supersecret')
        self.assertEqual(buffer.history.get_strings(), [])

    def test_the_status_line_tallies_the_run(self):
        # The verb, the current playbook and the tally — the task
        # and play names shift too fast to read, so they stay out
        self.session.run_display = 'shell'
        self.session.active_run = {
                'run': {'mode': 'run', 'playbooks': ['site'],
                        'taskIndex': 3, 'taskCount': 50,
                        'taskCounts': [50],
                        'currentTask': 'TASK [nginx : Install]'},
                'started': time.monotonic(), 'task_id': 't1'}
        left, right = self.session.status_line()
        self.assertIn('running site', right)
        self.assertIn('3/50', right)
        self.assertNotIn('nginx', right)
        # ... and not at rest
        self.session.active_run = None
        _left, right = self.session.status_line()
        self.assertNotIn('running', right)

    def test_the_run_remaining_extrapolates_from_the_tally(self):
        # 30 s for 5 of 50 tasks: 4:30 left
        self.assertEqual(repl._run_remaining(30, 5, 50, 240), 270)
        # Skipped tasks racing through their banners shrink it fast
        self.assertEqual(repl._run_remaining(40, 40, 50, 240), 10)
        # Before the first task lands: the probe's static estimate,
        # less the elapsed time
        self.assertEqual(repl._run_remaining(30, 0, 50, 240), 210)
        self.assertEqual(repl._run_remaining(300, 0, 50, 240), 0)
        # Nothing to say without any count
        self.assertIsNone(repl._run_remaining(30, 0, 50, None))
        self.assertIsNone(repl._run_remaining(30, None, None, None))
        # An overrun (a dynamic include banners past the tally)
        # floors at zero
        self.assertEqual(repl._run_remaining(30, 60, 50, 240), 0)

    def test_the_status_line_clocks_the_run(self):
        # The elapsed time and the refined estimate ride the tally
        self.session.run_display = 'shell'
        with mock.patch.object(repl.time, 'monotonic',
                               return_value=10030.0):
            self.session.active_run = {
                    'run': {'mode': 'run', 'playbooks': ['site'],
                            'taskIndex': 5, 'taskCount': 50,
                            'estimateSeconds': 240},
                    'started': 10000.0, 'task_id': 't1'}
            _left, right = self.session.status_line()
        self.assertIn('running site', right)
        self.assertIn('5/50', right)
        self.assertIn('0:30', right)
        # 30 s for 5 of 50 tasks extrapolates to 4:30 left
        self.assertIn('~4:30', right)
        self.assertIn(' │ ', right)

    def test_the_status_line_clocks_a_foreign_run(self):
        self.session.run_display = 'shell'
        self.session._foreign_run = {
                'mode': 'run', 'playbooks': ['site'],
                'tasksDone': 4, 'tasksTotal': 12,
                'elapsedSeconds': 60, 'estimateSeconds': 180}
        _left, right = self.session.status_line()
        self.assertIn('running site', right)
        self.assertIn('4/12', right)
        self.assertIn('1:00', right)
        # 60 s for 4 of 12 tasks: two minutes left
        self.assertIn('~2:00', right)

    def test_the_status_line_picks_the_current_playbook(self):
        # Several playbooks in one command: the one running
        self.session.run_display = 'shell'
        self.session.active_run = {
                'run': {'mode': 'check',
                        'playbooks': ['service/core', 'service/ferm'],
                        'taskIndex': 21, 'taskCount': 50,
                        'taskCounts': [20, 30]},
                'started': time.monotonic(), 'task_id': 't1'}
        _left, right = self.session.status_line()
        self.assertIn('checking service/ferm', right)
        self.assertNotIn('service/core ·', right)
        self.assertIn('21/50', right)

    def test_the_status_line_shows_a_foreign_run(self):
        self.session.run_display = 'shell'
        self.session._foreign_run = {
                'mode': 'run', 'playbooks': ['site'],
                'tasksDone': 4, 'tasksTotal': 12,
                'currentPlaybook': 'site'}
        _left, right = self.session.status_line()
        self.assertIn('running site', right)
        self.assertIn('4/12', right)

    def test_the_status_line_shows_the_preparing_phase(self):
        # The run's own bookkeeping announces the phase while the
        # daemon works the gates and the tally probe
        self.session.run_display = 'shell'
        self.session.active_run = {
                'run': {'mode': 'check', 'playbooks': ['site'],
                        'phase': 'preparing'},
                'started': time.monotonic(), 'task_id': 't1'}
        _left, right = self.session.status_line()
        self.assertIn('preparing site', right)
        self.assertNotIn('checking', right)

    def test_a_modeless_foreign_run_reads_as_preparing(self):
        # A run inside the daemon's probe window cannot say its
        # mode yet; it is preparing, not 'running'
        self.session.run_display = 'shell'
        self.session._foreign_run = {'runInFlight': True}
        _left, right = self.session.status_line()
        self.assertIn('preparing', right)
        self.assertNotIn('running', right)

    def test_the_playbook_list_shrinks_as_the_spans_pass(self):
        # The running playbook and the ones after it; a finished
        # playbook drops off the list
        self.assertEqual(
                repl._remaining_playbooks(['a', 'b', 'c'], 0, [2, 2, 2]),
                ['a', 'b', 'c'])
        self.assertEqual(
                repl._remaining_playbooks(['a', 'b', 'c'], 3, [2, 2, 2]),
                ['b', 'c'])
        self.assertEqual(
                repl._remaining_playbooks(['a', 'b', 'c'], 6, [2, 2, 2]),
                ['c'])
        # Without the spans the whole list stands; without playbooks
        # there is nothing
        self.assertEqual(
                repl._remaining_playbooks(['a', 'b'], 3, None),
                ['a', 'b'])
        self.assertEqual(repl._remaining_playbooks([], 0, []), [])

    def test_the_playbook_list_elides_its_middle(self):
        names = ['one', 'two', 'three', 'four', 'five', 'six']
        full = ' '.join(names)
        self.assertEqual(repl._ellipsize_middle(names, len(full)), full)
        # Whole names from the front and the back in alternation,
        # the middle elided
        self.assertEqual(repl._ellipsize_middle(names, 18),
                         'one two … six')
        self.assertEqual(repl._ellipsize_middle(names, 26),
                         'one two three … five six')
        # The running name survives an absurd budget, hard-cut
        self.assertEqual(repl._ellipsize_middle(names, 3), 'one')
        self.assertEqual(repl._ellipsize_middle([], 10), '')

    def test_the_run_segment_keeps_to_two_thirds_of_the_bar(self):
        # A long list elides its middle so the place on the left
        # keeps its third of the bar
        self.session.run_display = 'shell'
        self.session.active_run = {
                'run': {'mode': 'run',
                        'playbooks': ['service/alpha', 'service/beta',
                                      'service/gamma', 'service/delta',
                                      'service/epsilon'],
                        'taskIndex': 3, 'taskCount': 50,
                        'taskCounts': [10, 10, 10, 10, 10]},
                'started': time.monotonic(), 'task_id': 't1'}
        _left, right = self.session.status_line(width=120)
        self.assertIn('…', right)
        self.assertIn('service/alpha', right)
        self.assertIn('service/epsilon', right)
        self.assertNotIn('service/gamma', right)
        # The whole right side stays within the two thirds
        self.assertLessEqual(len(right), 120 * 2 // 3)

    def test_the_foreign_list_shrinks_by_the_status_spans(self):
        # The structured status carries the tally spans, so a run
        # watched from another client drops its finished playbooks
        # the same way
        self.session.run_display = 'shell'
        self.session._foreign_run = {
                'mode': 'run', 'playbooks': ['service/core',
                                             'service/ferm'],
                'tasksDone': 21, 'tasksTotal': 50,
                'taskCounts': [20, 30]}
        _left, right = self.session.status_line()
        self.assertIn('running service/ferm', right)
        self.assertNotIn('service/core', right)

    def test_the_poller_reads_the_project_status(self):
        watcher = FakeClient()
        watcher.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'structuredContent': {
                    'runInFlight': True, 'mode': 'run',
                    'playbooks': ['site'], 'tasksDone': 4,
                    'tasksTotal': 12, 'currentPlaybook': 'site'}},
                {'resultType': 'complete', 'isError': False,
                 'structuredContent': {'runInFlight': False}})
        session = self.session
        session.run_display = 'shell'
        invalidations = []
        session._invalidate = lambda: invalidations.append(1)
        session._poll_foreign_run_once(watcher)
        self.assertIsNotNone(session._foreign_run)
        self.assertIn('running site', session.status_line()[1])
        session._poll_foreign_run_once(watcher)
        self.assertIsNone(session._foreign_run)
        self.assertNotIn('running', session.status_line()[1])
        self.assertEqual(len(invalidations), 2)

    def test_the_poller_clears_on_a_dead_daemon(self):
        watcher = FakeClient()
        session = self.session
        session._foreign_run = {'mode': 'run'}
        watcher.request = mock.Mock(side_effect=ConnectionError('x'))
        session._poll_foreign_run_once(watcher)
        self.assertIsNone(session._foreign_run)

    def test_the_poller_stands_down_during_our_own_run(self):
        watcher = FakeClient()
        session = self.session
        session.active_run = {'run': {'mode': 'run'},
                              'started': time.monotonic(),
                              'task_id': 't1'}
        session._poll_foreign_run_once(watcher)
        self.assertEqual(watcher.calls, [])

    def test_ctrl_c_aborts_the_run_and_never_exits(self):
        # The signal passes along to the daemon; the client keeps
        # running, the second Ctrl-C too — exit is exit/Ctrl-D
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        side = mock.Mock()
        self.session.client.clone = mock.Mock(return_value=side)
        self.session.active_run = {
                'run': {'mode': 'run', 'playbooks': ['site']},
                'started': time.monotonic(), 'task_id': 't1'}
        event = mock.MagicMock()
        binding.handler(event)
        side.request.assert_called_once_with('tasks/cancel',
                                             {'taskId': 't1'})
        self.assertIn('cancelling the run', self.err.getvalue())
        event.app.exit.assert_not_called()
        # The second one while the abort clears: still no exit
        binding.handler(event)
        event.app.exit.assert_not_called()

    def test_ctrl_c_cancels_a_foreign_run(self):
        # A run started elsewhere stops from here: the status
        # answer named its task
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        side = mock.Mock()
        self.session.client.clone = mock.Mock(return_value=side)
        self.session._foreign_run = {
                'mode': 'run', 'playbooks': ['site'],
                'tasksDone': 4, 'tasksTotal': 12, 'taskId': 't-9'}
        binding.handler(mock.MagicMock())
        side.request.assert_called_once_with('tasks/cancel',
                                             {'taskId': 't-9'})
        self.assertIn('cancelling the run', self.err.getvalue())

    def test_ctrl_c_ignores_a_foreign_run_without_a_task(self):
        # A queue-driven run has no task to address; the key falls
        # through to clearing the line
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        self.session._foreign_run = {'mode': 'run',
                                     'playbooks': ['site']}
        event = mock.MagicMock()
        binding.handler(event)
        event.app.current_buffer.reset.assert_called_once_with()

    def test_the_cancel_refusal_lands_as_a_note(self):
        # The daemon's answer to a foreign cancel without the trust
        side = mock.Mock()
        side.request.side_effect = ReplError(
                'the run belongs to another client')
        self.session.client.clone = mock.Mock(return_value=side)
        self.assertFalse(self.session._cancel_run('t-9'))
        self.assertIn('belongs to another client', self.err.getvalue())

    def test_the_prompt_marker(self):
        # The plain session
        self.assertEqual(self.session._prompt_marker(), 'debops>')
        # The elevation shows on it
        self.session.elevated = True
        self.assertEqual(self.session._prompt_marker(), 'debops!>')
        self.session.elevated = False
        # The confined session wears brackets
        self.session.confined = True
        self.assertEqual(self.session._prompt_marker(), '[debops>]')
        self.session.confined = False
        # Root gets the shell convention, composed with the rest
        with mock.patch('os.geteuid', return_value=0):
            self.assertEqual(self.session._prompt_marker(), 'debops#')
            self.session.elevated = True
            self.session.confined = True
            self.assertEqual(self.session._prompt_marker(),
                             '[debops!#]')

    def test_ctrl_c_clears_the_idle_line(self):
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        event = mock.MagicMock()
        binding.handler(event)
        event.app.exit.assert_not_called()
        event.app.current_buffer.reset.assert_called_once_with()

    def test_ctrl_d_exits_on_an_empty_line(self):
        self.shell._build_shell(self.session)
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-d',)][0]
        event = mock.MagicMock()
        # The empty line leaves...
        self.assertTrue(binding.filter())
        binding.handler(event)
        event.app.exit.assert_called_once()
        self.assertIsInstance(event.app.exit.call_args[1]['exception'],
                              EOFError)
        # ... and with text the line keeps it for delete-char
        self.session._input_buffer.text = 'sta'
        self.assertFalse(binding.filter())

    def test_the_input_row_empties_during_a_run(self):
        # The prompt line stays on the screen but empty, read-only;
        # a question pending mid-run brings the marker and the
        # editing back
        app = self.shell._build_shell(self.session)
        buffer = self.session._input_buffer
        row = app.layout.container.content.children[0]
        marker_of = row.children[0].content.text
        self.assertTrue(callable(marker_of))
        self.assertEqual(marker_of(), 'debops> ')
        self.assertFalse(buffer.read_only())
        self.session.active_run = {
                'run': {'mode': 'run', 'playbooks': ['site']},
                'started': time.monotonic(), 'task_id': 't1'}
        self.assertEqual(marker_of(), '')
        self.assertTrue(buffer.read_only())
        self.session._gate = {'secret': False}
        self.assertEqual(marker_of(), 'debops> ')
        self.assertFalse(buffer.read_only())

    def test_ctrl_c_cancels_the_active_run(self):
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        side = mock.Mock()
        self.session.client.clone = mock.Mock(return_value=side)
        self.session.active_run = {
                'run': {'mode': 'run', 'playbooks': ['site']},
                'started': time.monotonic(), 'task_id': 't1'}
        binding.handler(mock.MagicMock())
        side.request.assert_called_once_with('tasks/cancel',
                                             {'taskId': 't1'})
        side.close.assert_called_once_with()
        self.assertIn('cancelling the run', self.err.getvalue())

    def test_ctrl_c_drops_a_busy_commands_connection(self):
        bindings = self.shell._shell_bindings(self.session)
        binding = [b for b in bindings.bindings if b.keys == ('c-c',)][0]
        self.session._busy = True
        binding.handler(mock.MagicMock())
        self.assertTrue(self.session.client.closed)
        self.assertIn('lost its connection', self.err.getvalue())

    def test_a_bare_enter_line_feeds(self):
        # The shell habit: Enter on an empty line scrolls the prompt
        self.shell._build_shell(self.session)
        buffer = mock.Mock()
        buffer.text = ''
        accept = self.session._input_buffer.accept_handler
        with mock.patch.object(self.session, 'submit') as submit:
            accept(buffer)
        submit.assert_not_called()
        self.assertEqual(self.out.getvalue(), 'debops>\n')

    def test_the_output_coalesces_per_window(self):
        # A burst of output lines repaints the chrome once per
        # window, not once per line
        self.session._app = mock.Mock()
        self.session._app.is_running = True
        with mock.patch.object(self.session,
                               '_print_output_text') as printer:
            for index in range(10):
                self.session._print_output_line('line {}'.format(
                        index))
            # The first line flushed; the rest coalesced
            self.assertEqual(printer.call_count, 1)
            self.assertEqual(len(self.session._pending_lines), 9)
            self.session._flush_output_lines(force=True)
            self.assertEqual(printer.call_count, 2)
            block = printer.call_args_list[1][0][0]
            self.assertEqual(block, ''.join(
                    'line {}\n'.format(index) for index in range(1, 10)))

    def test_the_app_print_rides_the_loop(self):
        self.session._app = mock.Mock()
        self.session._app.is_running = True
        with mock.patch('asyncio.run_coroutine_threadsafe') as sched:
            self.session._print_output_text('text\n')
        self.assertEqual(sched.call_count, 1)
        # The print is queued for the drain at the run's end; the
        # unscheduled coroutine of the mock goes back unawaited
        sched.call_args[0][0].close()
        self.assertEqual(len(self.session._pending_prints), 1)

    def test_completion_columns_are_column_major(self):
        self.assertEqual(
                repl._completion_columns(['aa', 'bb', 'cc', 'dd'], 10),
                ['aa  cc', 'bb  dd'])
        self.assertEqual(repl._completion_columns([], 80), [])
        # A narrow terminal wraps to one column
        self.assertEqual(
                repl._completion_columns(['aa', 'bb'], 3),
                ['aa', 'bb'])

    def test_the_confined_plain_loop_ignores_suspend(self):
        tmp_history_home(self)
        fake_readline = mock.MagicMock()
        client = FakeClient()
        session, _out, _err = make_repl(client, interactive=True)
        session.confined = True
        dispositions = []

        def peek(prompt):
            dispositions.append(signal.getsignal(signal.SIGTSTP))
            raise EOFError()

        with mock.patch.dict(sys.modules, {'readline': fake_readline}), \
                mock.patch('builtins.input', peek):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertEqual(dispositions, [signal.SIG_IGN])
        # The disposition is restored for the caller
        self.assertEqual(signal.getsignal(signal.SIGTSTP),
                         signal.SIG_DFL)

    def test_the_free_plain_loop_keeps_suspend(self):
        tmp_history_home(self)
        client = FakeClient()
        session, _out, _err = make_repl(client, interactive=True)
        dispositions = []

        def peek(prompt):
            dispositions.append(signal.getsignal(signal.SIGTSTP))
            raise EOFError()

        with mock.patch('builtins.input', peek):
            code = repl.run_interactive_plain(session)
        self.assertEqual(code, 0)
        self.assertEqual(dispositions, [signal.SIG_DFL])


class ToolHatchTestCase(unittest.TestCase):
    """The 'tool/<name>' hatch: the server's own surface, callable
    directly, listable in the help and the completion."""

    TOOLS = {'tools': [
            {'name': 'project.status',
             'description': 'Report the status of the project.'},
            {'name': 'playbook.list',
             'description': 'List the playbook targets. More text.'}]}

    def make_listed_repl(self):
        client = FakeClient()
        client.queue('tools/list', self.TOOLS)
        session, out, err = make_repl(client)
        return client, session, out, err

    def test_the_hatch_calls_the_tool(self):
        self.client = FakeClient()
        self.repl, self.out, self.err = make_repl(self.client)
        self.client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [{'type': 'text', 'text': 'ok'}]})
        self.repl.execute('tool/playbook.list detail=full')
        self.assertEqual(self.client.calls[0],
                         ('tools/call',
                          tool_params('playbook.list',
                                      {'detail': 'full'})))
        self.assertIn('ok', self.out.getvalue())

    def test_the_hatch_needs_a_name(self):
        session, _out, _err = make_repl(FakeClient())
        with self.assertRaises(ReplError) as context:
            session.execute('tool/')
        self.assertIn('usage: tool/', str(context.exception))

    def test_the_hatch_rejects_a_bare_token(self):
        session, _out, _err = make_repl(FakeClient())
        with self.assertRaises(ReplError) as context:
            session.execute('tool/playbook.list full')
        self.assertIn('key=value', str(context.exception))

    def test_an_unknown_tool_gets_the_servers_pointer(self):
        client = FakeClient()

        def unknown(method, params):
            return {'error': {'code': -32602, 'message': 'Invalid params',
                              'data': {'details':
                                       "unknown tool: nope; did you mean "
                                       "'role.info'?"}}}
        client.request = unknown
        session, _out, _err = make_repl(client)
        with self.assertRaises(ReplError) as context:
            session.execute('tool/nope')
        self.assertIn("did you mean 'role.info'?", str(context.exception))

    def test_the_argument_coercions(self):
        self.assertEqual(
                repl.tool_arguments(['a=1', 'b=true', 'c=false',
                                     'd=1.5', 'e=x', 'e=y']),
                {'a': 1, 'b': True, 'c': False, 'd': '1.5',
                 'e': ['x', 'y']})

    def test_tools_toggle_lists_and_hides(self):
        client, session, _out, err = self.make_listed_repl()
        self.assertFalse(session._tools_visible)
        session.execute('tools')
        self.assertTrue(session._tools_visible)
        self.assertIn('server tools are listed (2)', err.getvalue())
        session.execute('tools')
        self.assertFalse(session._tools_visible)
        self.assertIn('hidden', err.getvalue())
        self.assertEqual([call[0] for call in client.calls],
                         ['tools/list'])
        # Unhiding refetches: the daemon may have restarted with a
        # changed policy
        session.execute('tools')
        self.assertEqual([call[0] for call in client.calls],
                         ['tools/list', 'tools/list'])

    def test_help_lists_the_tools_sorted_when_visible(self):
        client, session, out, _err = self.make_listed_repl()
        session.execute('tools')
        out.seek(0)
        out.truncate(0)
        session.execute('help')
        text = out.getvalue()
        self.assertIn('Server tools', text)
        self.assertLess(text.index('tool/playbook.list'),
                        text.index('tool/project.status'))
        # The summary is the first sentence of the description
        self.assertIn('List the playbook targets', text)
        self.assertNotIn('More text', text)

    def test_help_hides_them_by_default(self):
        client = FakeClient()
        session, out, _err = make_repl(client)
        session.execute('help')
        text = out.getvalue()
        self.assertIn('server tools are hidden', text)
        self.assertNotIn('\n  tool/', text)
        # ... and the hidden listing costs no call
        self.assertEqual(client.calls, [])

    def test_the_completion_offers_the_tools_when_visible(self):
        client, session, _out, _err = self.make_listed_repl()
        _word, candidates = repl._candidates_for(session, '')
        self.assertNotIn('tool/playbook.list', candidates)
        session.execute('tools')
        _word, candidates = repl._candidates_for(session, '')
        self.assertIn('tool/playbook.list', candidates)
        self.assertIn('tool/project.status', candidates)
        # The human verbs stay
        self.assertIn('status', candidates)

    def test_the_busy_session_completes_from_the_cache_only(self):
        client, session, _out, _err = self.make_listed_repl()
        session._tools_visible = True
        session._busy = True
        _word, candidates = repl._candidates_for(session, 'tool/p')
        self.assertNotIn('tool/playbook.list', candidates)
        self.assertEqual(client.calls, [])


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
                          tool_params('project.list',
                                      {'path': 'ansible/inventory'})))

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
        self.assertEqual(
            client.calls[0][1],
            tool_params('project.list', {'path': 'ansible/inventory'}))

    def test_ls_at_the_project_root_omits_the_path(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = ''
        self.queue_list(client)
        session.execute('ls')
        self.assertEqual(
            client.calls[0][1],
            tool_params('project.list', {}))

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
            tool_params('project.read', {'path': 'ansible/inventory/hosts'}))
        session.execute('files group_vars')
        self.assertEqual(
            client.calls[1][1],
            tool_params('project.files', {'path': 'ansible/inventory/group_vars'}))

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
            tool_params('project.mkdir', {'path': 'resources/scripts'}))
        session.execute('rm playbooks/deploy.yml')
        self.assertEqual(
            client.calls[1][1],
            tool_params('file.delete', {'path': 'playbooks/deploy.yml'}))
        session.execute('mv playbooks/a.yml playbooks/b.yml')
        self.assertEqual(
            client.calls[2][1],
            tool_params('file.move', {'source': 'playbooks/a.yml',
                                      'target': 'playbooks/b.yml'}))

    def test_the_elevated_write_spelling_is_project_rooted(self):
        session, client, _out, _err = self.make_session(
                elevated=True)
        session.cwd = ''
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': []})
        session.execute('mkdir .debops/conf.d')
        self.assertEqual(
            client.calls[0][1],
            tool_params('project.mkdir', {'path': '/.debops/conf.d'}))

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
            tool_params('project.read', {'path': 'ansible/playbooks/a.yml'}))
        self.assertEqual(
            client.calls[1][1],
            tool_params('file.write', {'path': 'playbooks/b.yml',
                                       'content': '---\n[]\n'}))

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
            tool_params('file.write', {'path': 'resources/a.yml',
                                       'content': 'c\n'}))

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
                                    'isError': False, 'content': [],
                                    '_meta': {'org.debops/view':
                                              'webservers'}})
        session.execute('view webservers')
        self.assertEqual(session.view, 'webservers')
        self.assertEqual(session.cwd, 'ansible/views/webservers')
        self.assertIsNone(session._previous_cwd)

    def test_a_remote_switch_moves_the_scope_and_notes_it(self):
        # Another client moved the daemon's view; the answer's
        # '_meta' moves the session's scope with it
        session, client, _out, err = self.make_session(
                project_type='modern',
                view_root_rel='ansible/views/system')
        session.cwd = 'ansible/views/system/inventory'
        client.queue('tools/call', {
                'resultType': 'complete', 'isError': False,
                'content': [], '_meta': {'org.debops/view':
                                         'webservers'}})
        session.execute('ls')
        self.assertEqual(session.view, 'webservers')
        self.assertEqual(session.cwd, 'ansible/views/webservers')
        self.assertIn('the view is now webservers', err.getvalue())

    def test_the_view_switch_in_a_legacy_project(self):
        session, client, _out, _err = self.make_session()
        client.queue('tools/call', {'resultType': 'complete',
                                    'isError': False, 'content': [],
                                    '_meta': {'org.debops/view':
                                              'webservers'}})
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
        # The verbs themselves complete at the start of the line,
        # with the trailing space of a finished word
        found = [entry.text for entry in completer.get_completions(
                Document('mkd'), None)]
        self.assertIn('mkdir ', found)

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
