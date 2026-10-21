# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the run log handling of the playbook executions.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp import runs
import os
import shutil
import tempfile
import time
import unittest


class FakeProject(object):

    def __init__(self, path, name=None):
        self.path = path
        self.name = name if name is not None else os.path.basename(path)


class RunsTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-runs-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        os.environ['XDG_CACHE_HOME'] = os.path.join(self.tmpdir, 'cache')

    def test_log_path_lives_in_the_cache_directory(self):
        project = FakeProject(os.path.join(self.tmpdir, 'myproject'))
        path = runs.log_path(project, 3)
        self.assertTrue(path.startswith(
                os.path.join(self.tmpdir, 'cache', 'debops', 'mcp', 'runs')))
        self.assertTrue(path.endswith('execution-3.log'))
        # The project name makes the directory recognizable
        self.assertIn('myproject', os.path.basename(os.path.dirname(path)))

    def test_log_path_creates_the_directory(self):
        project = FakeProject(os.path.join(self.tmpdir, 'myproject'))
        path = runs.log_path(project, 1)
        self.assertTrue(os.path.isdir(os.path.dirname(path)))

    def test_the_cache_chain_is_private(self):
        # A diff or a debug run renders secrets into the log; nothing
        # below the user's cache directory may be readable by others
        project = FakeProject(os.path.join(self.tmpdir, 'myproject'))
        path = runs.log_path(project, 1)
        base = os.environ['XDG_CACHE_HOME']
        directory = os.path.dirname(path)
        while directory.startswith(base + os.sep):
            mode = os.stat(directory).st_mode & 0o777
            self.assertEqual(mode, 0o700,
                             '{} has mode {:o}'.format(directory, mode))
            directory = os.path.dirname(directory)

    def test_project_key_distinguishes_same_named_projects(self):
        one = FakeProject(os.path.join(self.tmpdir, 'one', 'project'))
        two = FakeProject(os.path.join(self.tmpdir, 'two', 'project'))
        self.assertNotEqual(runs.project_key(one), runs.project_key(two))
        # ... while staying stable for the same project
        self.assertEqual(runs.project_key(one), runs.project_key(one))

    def test_project_key_sanitizes_the_name(self):
        project = FakeProject(self.tmpdir, name='my project!')
        key = runs.project_key(project)
        self.assertNotIn(' ', key)
        self.assertNotIn('!', key)

    def test_project_key_distinguishes_the_views(self):
        # The executions of one view are serialized by its lock; a
        # directory per view keeps a prune from touching the log a live
        # run of another view is still writing
        class Viewed(object):

            def __init__(self, path, view):
                self.path = path
                self.name = os.path.basename(path)
                self.view = view

        base = os.path.join(self.tmpdir, 'project')
        system = Viewed(base, 'system')
        dmz = Viewed(base, 'prod/dmz')
        self.assertNotEqual(runs.project_key(system),
                            runs.project_key(dmz))
        self.assertNotIn('/', runs.project_key(dmz))
        self.assertIn('prod-dmz', runs.project_key(dmz))

    def test_prune_keeps_the_recent_logs(self):
        directory = os.path.join(self.tmpdir, 'runs')
        os.makedirs(directory)
        now = time.time()
        for index in range(15):
            path = os.path.join(directory,
                                'execution-{}.log'.format(index))
            with open(path, 'w') as fp:
                fp.write('run {}\n'.format(index))
            # The modification order decides what is kept
            moment = now - 1000 + index
            os.utime(path, (moment, moment))
        runs.prune(directory, keep=10)
        remaining = sorted(os.listdir(directory))
        self.assertEqual(len(remaining), 10)
        self.assertIn('execution-14.log', remaining)
        self.assertNotIn('execution-0.log', remaining)

    def test_prune_drops_the_aged_logs(self):
        directory = os.path.join(self.tmpdir, 'runs')
        os.makedirs(directory)
        aged = os.path.join(directory, 'execution-1.log')
        with open(aged, 'w') as fp:
            fp.write('old\n')
        old = time.time() - runs.MAX_LOG_AGE - 60
        os.utime(aged, (old, old))
        runs.prune(directory, keep=10)
        self.assertEqual(os.listdir(directory), [])

    def test_read_tail_returns_the_whole_small_file(self):
        path = os.path.join(self.tmpdir, 'run.log')
        with open(path, 'w') as fp:
            fp.write('line one\nline two\n')
        self.assertEqual(runs.read_tail(path, 1024), 'line one\nline two\n')

    def test_read_tail_is_bounded(self):
        path = os.path.join(self.tmpdir, 'run.log')
        with open(path, 'w') as fp:
            for index in range(1000):
                fp.write('line {:04d} of the run\n'.format(index))
        tail = runs.read_tail(path, 200)
        self.assertLessEqual(len(tail), 200)
        self.assertIn('line 0999', tail)
        self.assertNotIn('line 0000', tail)
        # The cut first line is dropped
        self.assertTrue(tail.startswith('line '))

    def test_read_tail_of_a_missing_file_is_empty(self):
        self.assertEqual(runs.read_tail(
                os.path.join(self.tmpdir, 'missing.log'), 100), '')

    def test_status_tail_keeps_the_last_lines(self):
        lines = ['line {}\n'.format(index) for index in range(50)]
        tail = runs.status_tail(lines, count=5)
        self.assertEqual(tail.splitlines(),
                         ['line 45', 'line 46', 'line 47',
                          'line 48', 'line 49'])

    def test_status_tail_shortens_the_long_lines(self):
        lines = ['x' * 500 + '\n']
        tail = runs.status_tail(lines)
        self.assertLessEqual(len(tail), runs.TAIL_LINE_LENGTH + 1)
        self.assertTrue(tail.endswith('…'))

    def test_status_tail_is_capped(self):
        lines = ['x' * 150 for _ in range(runs.TAIL_LINES)]
        tail = runs.status_tail(lines, limit=200)
        self.assertLessEqual(len(tail), 200)


if __name__ == '__main__':
    unittest.main()
