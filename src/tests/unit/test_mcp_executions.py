# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the execution inspection tools ('execution.list' and
'execution.output').

Written with 'unittest' so that the suite runs under pytest without extra
dependencies. The logs are written directly into a test cache directory;
no playbook runs.
"""

from debops.mcp import runs
from debops.mcp.policy import McpPolicy
from debops.mcp.tools.executions import (tool_execution_list,
                                         tool_execution_output,
                                         tool_execution_purge)
import os
import shutil
import tempfile
import time
import unittest


SAMPLE_LOG = '''\
PLAY [web] *********************************************************************

TASK [Gather facts] ************************************************************
ok: [web1]
ok: [web2]

TASK [Install the package] *****************************************************
[WARNING]: Consider using the apt module instead
[DEPRECATION WARNING]: The frobnicate option is deprecated and will be
removed in a future release
changed: [web1]
changed: [web2]

TASK [Template the config] *****************************************************
ok: [web1]
fatal: [web2]: FAILED! => {"changed": false, "msg": "boom"}

PLAY RECAP *********************************************************************
web1 : ok=2 changed=1 unreachable=0 failed=0 skipped=0 rescued=0 ignored=0
web2 : ok=1 changed=0 unreachable=0 failed=1 skipped=0 rescued=0 ignored=0

debops-mcp: Ansible finished with return code 2
'''


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


class ExecutionsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-runs-query-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')
        self.project = FakeProject(os.path.join(self.tmpdir, 'project'))
        self.policy = McpPolicy(self.project)

    def write_log(self, execution_id, content=SAMPLE_LOG,
                  metadata=None):
        _id, path = runs.new_log(self.project)
        self.assertEqual(_id, execution_id)
        with open(path, 'w') as fp:
            fp.write(content)
        if metadata is not None:
            runs.write_metadata(path, metadata)
        return path

    def query(self, **arguments):
        return tool_execution_output(self.project, self.policy, arguments)

    def text(self, result):
        return result['content'][0]['text']

    # -- execution.list -------------------------------------------------

    # -- log.reader -------------------------------------------------------

    def test_log_reader_serves_the_whole_log(self):
        from debops.mcp.tools.executions import tool_log_reader
        self.write_log(1, metadata={'executionId': 1,
                                    'mode': 'check'})
        result = tool_log_reader(self.project, self.policy,
                                 {'id': 1})
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][0]['text'], SAMPLE_LOG)
        self.assertEqual(result['structuredContent']['executionId'], 1)
        self.assertGreater(result['structuredContent']['logBytes'], 0)
        # The default is the newest run
        self.write_log(2, content='PLAY [second]\n',
                       metadata={'executionId': 2, 'mode': 'run'})
        result = tool_log_reader(self.project, self.policy, {})
        self.assertEqual(result['content'][0]['text'],
                         'PLAY [second]\n')

    def test_log_reader_refuses_an_unknown_id(self):
        from debops.mcp.tools.executions import tool_log_reader
        result = tool_log_reader(self.project, self.policy,
                                 {'id': 99})
        self.assertTrue(result['isError'])
        self.assertIn('no executions recorded',
                      result['content'][0]['text'])
        result = tool_log_reader(self.project, self.policy,
                                 {'id': 'bogus'})
        self.assertTrue(result['isError'])

    def test_list_empty(self):
        result = tool_execution_list(self.project, self.policy, {})
        self.assertFalse(result['isError'])
        self.assertIn('No executions', self.text(result))
        self.assertEqual(result['structuredContent'], [])

    def test_list_reads_the_sidecar(self):
        path = self.write_log(1, metadata={
                'executionId': 1, 'mode': 'run', 'playbooks': ['site'],
                'hosts': ['web1', 'web2'], 'startedAt': '2026-10-21',
                'exitCode': 2, 'status': 'done'})
        result = tool_execution_list(self.project, self.policy, {})
        (entry,) = result['structuredContent']
        self.assertEqual(entry['id'], 1)
        self.assertEqual(entry['mode'], 'run')
        self.assertEqual(entry['playbooks'], ['site'])
        self.assertEqual(entry['exitCode'], 2)
        self.assertEqual(entry['log'], path)
        self.assertGreater(entry['logBytes'], 0)
        text = self.text(result)
        self.assertIn('run', text)
        self.assertIn('exit 2', text)

    def test_list_without_a_sidecar_uses_the_marker(self):
        self.write_log(1)
        result = tool_execution_list(self.project, self.policy, {})
        (entry,) = result['structuredContent']
        self.assertEqual(entry['exitCode'], 2)
        self.assertEqual(entry['status'], 'done')

    def test_list_notes_the_run_without_an_outcome(self):
        self.write_log(1, content='PLAY [web] ***\n')
        result = tool_execution_list(self.project, self.policy, {})
        (entry,) = result['structuredContent']
        self.assertNotIn('exitCode', entry)
        self.assertIn('no outcome recorded', self.text(result))

    def test_list_states_the_retention(self):
        # Nothing on the surface should leave a model guessing whether
        # the cache grows forever
        self.write_log(1)
        result = tool_execution_list(self.project, self.policy, {})
        text = self.text(result)
        self.assertIn('prunes itself', text)
        self.assertIn('last 10 runs', text)
        self.assertIn('execution.purge', text)

    # -- execution.output: addressing ------------------------------------

    def test_unknown_id_lists_the_known_ones(self):
        self.write_log(1)
        result = self.query(id=42)
        self.assertTrue(result['isError'])
        self.assertIn('no such execution: 42', self.text(result))
        self.assertIn('known executions: 1', self.text(result))

    def test_no_executions_at_all(self):
        result = self.query()
        self.assertTrue(result['isError'])
        self.assertIn('no executions recorded', self.text(result))

    def test_last_picks_the_newest(self):
        self.write_log(1, content='first\n')
        self.write_log(2, content='second\n')
        result = self.query()
        self.assertEqual(result['structuredContent']['executionId'], 2)
        self.assertIn('second', self.text(result))

    def test_id_accepts_a_stringified_integer(self):
        self.write_log(1)
        result = self.query(id='1')
        self.assertFalse(result['isError'])

    def test_kind_and_pattern_conflict(self):
        self.write_log(1)
        result = self.query(kind='changed', pattern='x')
        self.assertTrue(result['isError'])
        self.assertIn('not both', self.text(result))

    def test_unknown_kind_is_an_error(self):
        self.write_log(1)
        result = self.query(kind='everything')
        self.assertTrue(result['isError'])
        self.assertIn('the kinds are', self.text(result))

    def test_an_invalid_pattern_is_an_error(self):
        self.write_log(1)
        result = self.query(pattern='(unclosed')
        self.assertTrue(result['isError'])
        self.assertIn('invalid pattern', self.text(result))

    def test_max_lines_is_bounded(self):
        self.write_log(1)
        for bad in (0, -1, 1001):
            result = self.query(max_lines=bad)
            self.assertTrue(result['isError'], bad)

    # -- execution.output: queries ---------------------------------------

    def test_the_default_query_is_the_tail(self):
        self.write_log(1)
        result = self.query()
        self.assertFalse(result['isError'])
        text = self.text(result)
        self.assertIn('— tail', text)
        self.assertIn('return code 2', text)
        self.assertIn('PLAY RECAP', text)

    def test_changed_lists_the_tasks_and_their_hosts(self):
        self.write_log(1)
        result = self.query(kind='changed')
        text = self.text(result)
        self.assertIn('TASK [Install the package]', text)
        self.assertIn('changed: [web1]', text)
        self.assertIn('changed: [web2]', text)
        # The unchanged tasks and the ok lines stay out
        self.assertNotIn('Gather facts', text)
        self.assertNotIn('ok: [web1]', text)
        self.assertNotIn('PLAY RECAP', text)

    def test_the_lines_ride_the_structured_result(self):
        # A harness may show only the structured content of a tool
        # with an output schema; the lines are the answer, so they
        # must be there and not only in the text
        self.write_log(1)
        result = self.query(kind='changed')
        lines = result['structuredContent']['lines']
        self.assertTrue(lines[0].startswith('TASK [Install the package]'))
        self.assertIn('changed: [web1]', lines)
        self.assertIn('changed: [web2]', lines)
        self.assertEqual(len(lines),
                         result['structuredContent']['returned'])

    def test_no_matches_carry_empty_lines(self):
        self.write_log(1)
        result = self.query(kind='unreachable')
        self.assertEqual(result['structuredContent']['lines'], [])

    def test_failed_lists_the_failing_task(self):
        self.write_log(1)
        result = self.query(kind='failed')
        text = self.text(result)
        self.assertIn('TASK [Template the config]', text)
        self.assertIn('fatal: [web2]: FAILED!', text)
        self.assertIn('boom', text)
        self.assertNotIn('Install the package', text)

    def test_unreachable_is_distinct_from_failed(self):
        log = SAMPLE_LOG.replace(
                'fatal: [web2]: FAILED! => {"changed": false, "msg": '
                '"boom"}',
                'fatal: [web2]: UNREACHABLE! => {"msg": "no route"}')
        self.write_log(1, content=log)
        result = self.query(kind='unreachable')
        self.assertIn('UNREACHABLE', self.text(result))
        self.assertEqual(
                tool_execution_output(self.project, self.policy,
                                      {'kind': 'failed'})
                ['structuredContent']['matched'], 0)

    def test_recap_returns_the_per_host_lines(self):
        self.write_log(1)
        result = self.query(kind='recap')
        text = self.text(result)
        self.assertIn('PLAY RECAP', text)
        self.assertIn('web1 : ok=2 changed=1', text)
        self.assertIn('web2 : ok=1 changed=0', text)
        self.assertNotIn('TASK', text)

    def test_warnings(self):
        self.write_log(1)
        result = self.query(kind='warnings')
        text = self.text(result)
        self.assertIn('Consider using the apt module', text)
        # The deprecation warnings are usually what the question is
        # about; they are not '[WARNING]' lines
        self.assertIn('[DEPRECATION WARNING]: The frobnicate option',
                      text)

    def test_list_entries_carry_the_last_line(self):
        # The last line says where the run stands without a follow-up
        # query: the completion marker on a finished run
        self.write_log(1)
        result = tool_execution_list(self.project, self.policy, {})
        (entry,) = result['structuredContent']
        self.assertEqual(entry['lastLine'],
                         'debops-mcp: Ansible finished with '
                         'return code 2')
        self.assertIn(entry['lastLine'], self.text(result))

    def test_list_last_line_of_a_running_execution(self):
        self.write_log(1, content='PLAY [web] ***\n\nTASK [working] ***\n')
        result = tool_execution_list(self.project, self.policy, {})
        (entry,) = result['structuredContent']
        self.assertTrue(entry['lastLine'].startswith('TASK [working]'))

    def test_pattern_with_context(self):
        self.write_log(1)
        result = self.query(pattern='boom', context=2)
        text = self.text(result)
        self.assertIn('boom', text)
        # Two lines before and after the match
        self.assertIn('ok: [web1]', text)
        self.assertIn('PLAY RECAP', text)

    def test_pattern_windows_merge_without_duplicates(self):
        content = '\n'.join('line {}'.format(index)
                            for index in range(20)) + '\n'
        content = content.replace('line 4', 'hit 4')
        content = content.replace('line 6', 'hit 6')
        self.write_log(1, content=content)
        result = self.query(pattern='^hit', context=3)
        lines = [line for line in self.text(result).splitlines()
                 if line.startswith(('hit', 'line'))]
        # The second match sits in the context of the first: one
        # window, no duplicates, no separator
        self.assertEqual(lines.count('hit 6'), 1)
        self.assertEqual(lines[-1], 'line 9')
        self.assertNotIn('…', lines)

    def test_disjoint_windows_are_separated(self):
        content = ('hit one\n' + '\n'.join(
                'filler {}'.format(index) for index in range(20))
                + '\nhit two\n')
        self.write_log(1, content=content)
        result = self.query(pattern='^hit', context=1)
        self.assertIn('…', self.text(result).splitlines())

    def test_truncation_is_announced(self):
        content = ('TASK [touch everything] *****************************************\n'
                   + ''.join('changed: [host{}]\n'.format(index)
                             for index in range(50)))
        self.write_log(1, content=content)
        result = self.query(kind='changed', max_lines=10)
        structured = result['structuredContent']
        self.assertTrue(structured['truncated'])
        self.assertEqual(structured['returned'], 10)
        self.assertEqual(structured['matched'], 51)
        self.assertIn('more lines matched', self.text(result))

    def test_no_matches(self):
        self.write_log(1)
        result = self.query(kind='unreachable')
        self.assertFalse(result['isError'])
        self.assertIn('no matches', self.text(result))

    def test_a_custom_format_degrades_to_the_tail(self):
        # A non-default stdout callback prints no 'changed:' lines; the
        # query must not come back empty-handed on a non-empty log
        self.write_log(1, content='some custom log format\n' * 20)
        result = self.query(kind='changed')
        self.assertFalse(result['isError'])
        structured = result['structuredContent']
        self.assertTrue(structured['degraded'])
        self.assertIn('the tail', self.text(result))
        self.assertIn('some custom log format', self.text(result))

    def test_long_lines_are_shortened(self):
        self.write_log(1, content='x' * 2000 + '\n')
        result = self.query()
        for line in self.text(result).splitlines():
            self.assertLessEqual(len(line), 1100)

    def test_structured_results_match_the_schemas(self):
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        self.write_log(1, metadata={
                'executionId': 1, 'mode': 'run', 'playbooks': ['site'],
                'hosts': ['web1'], 'startedAt': '2026-10-21',
                'exitCode': 2, 'status': 'done'})
        listing = tool_execution_list(self.project, self.policy, {})
        self.assertEqual(check_schema(listing['structuredContent'],
                                      TOOL_OUTPUT_SCHEMAS[
                                          'execution.list']), [])
        answer = self.query(kind='changed')
        self.assertEqual(check_schema(answer['structuredContent'],
                                      TOOL_OUTPUT_SCHEMAS[
                                          'execution.output']), [])


class PurgeTestCase(unittest.TestCase):
    """execution.purge: the run logs of ended executions, on demand."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-purge-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')
        self.project = FakeProject(os.path.join(self.tmpdir, 'project'))
        self.policy = McpPolicy(self.project)

    def write_log(self, execution_id, content=SAMPLE_LOG, age=None):
        _id, path = runs.new_log(self.project)
        self.assertEqual(_id, execution_id)
        with open(path, 'w') as fp:
            fp.write(content)
        runs.write_metadata(path, {'executionId': execution_id})
        if age is not None:
            old = time.time() - age
            os.utime(path, (old, old))
            os.utime(os.path.splitext(path)[0] + '.json', (old, old))
        return path

    def purge(self, **arguments):
        return tool_execution_purge(self.project, self.policy,
                                    arguments)

    def test_older_than_is_required(self):
        self.assertTrue(self.purge()['isError'])
        self.assertTrue(self.purge(older_than='x')['isError'])
        self.assertTrue(self.purge(older_than=-1)['isError'])

    def test_an_old_ended_log_and_its_sidecar_go(self):
        path = self.write_log(1, age=3600)
        result = self.purge(older_than=600)
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['removed'], [1])
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(
                os.path.splitext(path)[0] + '.json'))

    def test_a_young_log_stays(self):
        self.write_log(1)
        result = self.purge(older_than=600)
        self.assertEqual(result['structuredContent']['removed'], [])
        self.assertEqual(result['structuredContent']['skipped'], 1)

    def test_the_floor_clamps_the_cutoff(self):
        self.write_log(1, age=90)
        result = self.purge(older_than=0)
        self.assertEqual(result['structuredContent']['cutoffSeconds'],
                         60)
        # Ninety seconds is past the floor: purged
        self.assertEqual(result['structuredContent']['removed'], [1])
        self.assertIn('floored', self.text(result))

    def text(self, result):
        return result['content'][0]['text']

    def test_an_unmarked_log_goes_when_no_run_holds_the_lock(self):
        self.write_log(1, content='PLAY [web] ***\n', age=3600)
        result = self.purge(older_than=600)
        self.assertEqual(result['structuredContent']['removed'], [1])

    def test_an_unmarked_log_stays_while_the_project_is_locked(self):
        # No completion marker and the flock is held: a live run may
        # be holding the log open mid quiet task
        from debops.mcp.tools.common import project_flock
        self.write_log(1, content='PLAY [web] ***\n', age=3600)
        with project_flock(self.project, self.policy, exclusive=True):
            result = self.purge(older_than=600)
        self.assertEqual(result['structuredContent']['removed'], [])
        self.assertEqual(result['structuredContent']['skipped'], 1)

    def test_a_marked_log_goes_even_while_the_project_is_locked(self):
        # The completion marker is the definitive record that the run
        # ended; whoever holds the lock now is a different job
        from debops.mcp.tools.common import project_flock
        self.write_log(1, age=3600)
        with project_flock(self.project, self.policy, exclusive=True):
            result = self.purge(older_than=600)
        self.assertEqual(result['structuredContent']['removed'], [1])

    def test_nothing_to_purge(self):
        result = self.purge(older_than=600)
        self.assertIn('Nothing to purge', self.text(result))
        self.assertEqual(result['structuredContent']['removed'], [])

    def test_structured_matches_the_schema(self):
        from tests.unit._helpers import check_schema
        from debops.mcp.tools import TOOL_OUTPUT_SCHEMAS
        self.write_log(1, age=3600)
        result = self.purge(older_than=600)
        self.assertEqual(check_schema(result['structuredContent'],
                                      TOOL_OUTPUT_SCHEMAS[
                                          'execution.purge']), [])


if __name__ == '__main__':
    unittest.main()
