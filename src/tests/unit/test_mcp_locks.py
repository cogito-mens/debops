# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the project locking primitives.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

from debops.mcp.tools.common import (ReadWriteLock,
                                     exclusive_project, project_flock,
                                     shared_project)
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


def _auto_commit_noop(self, message=None, paths=None):
    pass


class ReadWriteLockTestCase(unittest.TestCase):

    def setUp(self):
        self.lock = ReadWriteLock()

    def test_concurrent_readers(self):
        entered = [threading.Event(), threading.Event()]

        def reader(index):
            with self.lock.read():
                entered[index].set()
                time.sleep(0.2)

        threads = [threading.Thread(target=reader, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        # Both readers hold the lock at once
        self.assertTrue(entered[0].wait(5))
        self.assertTrue(entered[1].wait(5))
        for thread in threads:
            thread.join(timeout=5)

    def test_writer_excludes_readers(self):
        writer_ran = threading.Event()

        with self.lock.read():
            def writer():
                with self.lock.write():
                    writer_ran.set()

            thread = threading.Thread(target=writer)
            thread.start()
            # The writer waits while a reader holds the lock
            self.assertFalse(writer_ran.wait(0.3))
        thread.join(timeout=5)
        self.assertTrue(writer_ran.is_set())

    def test_writer_preference(self):
        events = []

        def reader(tag, delay):
            time.sleep(delay)
            with self.lock.read():
                events.append(tag)

        with self.lock.write():
            writer_waiting = threading.Event()

            def writer():
                writer_waiting.set()
                with self.lock.write():
                    events.append('writer')

            # A reader is already waiting when the writer queues, and
            # another reader queues after the writer
            thread_w = threading.Thread(target=writer)
            thread_r1 = threading.Thread(target=reader, args=('reader1', 0.1))
            thread_w.start()
            writer_waiting.wait(5)
            thread_r1.start()
            thread_r2 = threading.Thread(target=reader, args=('reader2', 0.2))
            thread_r2.start()
            # The first reader queued behind the waiting writer
            self.assertFalse(events)
        thread_w.join(timeout=5)
        thread_r1.join(timeout=5)
        thread_r2.join(timeout=5)
        # The writer went before both readers
        self.assertEqual(events[0], 'writer')


class FlockTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-lock-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {'raw': {}})(),
        })()
        self.policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()

    def test_exclusive_conflicts(self):
        with project_flock(self.project, self.policy, exclusive=True):
            with self.assertRaises(RuntimeError):
                with project_flock(self.project, self.policy,
                                   exclusive=True):
                    pass

    def test_contention_is_a_tool_level_error(self):
        # The contention is an expected, retryable condition: a
        # dedicated exception lets the registration layer report it as
        # a result rather than an internal error
        from debops.mcp.tools.common import ProjectLockedError
        with project_flock(self.project, self.policy, exclusive=True):
            with self.assertRaises(ProjectLockedError) as caught:
                with project_flock(self.project, self.policy,
                                   exclusive=True):
                    pass
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertIn('another process is working',
                      str(caught.exception))

    def test_shared_locks_compose(self):
        with project_flock(self.project, self.policy, exclusive=False):
            with project_flock(self.project, self.policy,
                               exclusive=False):
                pass

    def test_shared_blocks_an_exclusive_writer(self):
        with project_flock(self.project, self.policy, exclusive=False):
            with self.assertRaises(RuntimeError):
                with project_flock(self.project, self.policy,
                                   exclusive=True):
                    pass

    def test_exclusive_blocks_a_reader(self):
        # flock LOCK_SH blocks (does not raise) while a writer holds it
        with project_flock(self.project, self.policy, exclusive=True):
            entered = threading.Event()

            def reader():
                with project_flock(self.project, self.policy,
                                   exclusive=False):
                    entered.set()

            thread = threading.Thread(target=reader)
            thread.start()
            self.assertFalse(entered.wait(0.3))
        thread.join(timeout=5)
        self.assertTrue(entered.is_set())


class ProjectLockTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-plock-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'auto_commit': _auto_commit_noop,
            'config': type('Config', (), {'raw': {}})(),
        })()
        self.policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()

    def test_write_blocks_a_reader(self):
        finished = threading.Event()

        def reader():
            with shared_project(self.project, self.policy):
                finished.set()

        with exclusive_project(self.project, self.policy):
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertFalse(finished.wait(0.3))
        thread.join(timeout=5)
        self.assertTrue(finished.is_set())

    def test_lock_file_in_git_dir_when_present(self):
        os.makedirs(os.path.join(self.tmpdir, '.git'))
        with exclusive_project(self.project, self.policy):
            pass
        self.assertTrue(os.path.isfile(
                os.path.join(self.tmpdir, '.git', 'mcp.lock')))

    def test_exclusive_commits_on_exit_with_the_note(self):
        calls = []
        project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
            'auto_commit': lambda self, message=None, paths=None:
                calls.append((message, paths)),
        })()
        policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()
        with exclusive_project(project, policy, note='run site',
                               paths=['site.yml']):
            pass
        self.assertEqual(calls, [('DebOps MCP: run site', ['site.yml'])])

    def test_nothing_dirty_means_no_commit(self):
        calls = []
        project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
            'auto_commit': lambda self, message=None, paths=None:
                calls.append((message, paths)),
        })()
        policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()
        # Undeclared paths and no repository to read: the delta is
        # empty and the commit never fires
        with exclusive_project(project, policy, note='run site'):
            pass
        self.assertEqual(calls, [])

    def test_a_failed_auto_commit_does_not_mask_the_outcome(self):
        def raising(self, message=None, paths=None):
            raise RuntimeError('disk full mid-commit')

        project = type('Project', (), {
            'path': self.tmpdir, 'name': 'project',
            'project_type': 'legacy', 'view': 'system',
            'config': type('Config', (), {'raw': {}})(),
            'auto_commit': raising,
        })()
        policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()
        with self.assertLogs('debops.mcp.tools.common', level='WARNING') \
                as recorded:
            with exclusive_project(project, policy, note='run site',
                                   paths=['site.yml']):
                outcome = 'the work result'
        # The operation's own outcome stands; the failure is logged
        self.assertEqual(outcome, 'the work result')
        self.assertTrue(any('Auto-commit' in line
                            for line in recorded.output))


class SweepTestCase(unittest.TestCase):
    """The tool commits are scoped to what the call touched."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-mcp-sweep-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Test',
                        GIT_AUTHOR_EMAIL='test@example.org',
                        GIT_COMMITTER_NAME='Test',
                        GIT_COMMITTER_EMAIL='test@example.org')
        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir,
                       env=self.env, check=True)
        from tests.unit.test_projectdir import make_project_at
        self.project = make_project_at(
                self.tmpdir,
                raw={'project': {'git': {'auto_commit': True}}})
        self.policy = type('Policy', (), {
            'view_root': os.path.join(self.tmpdir, 'ansible'),
        })()

    def git(self, *args):
        return subprocess.run(['git'] + list(args), cwd=self.tmpdir,
                              env=self.env, check=True,
                              capture_output=True, text=True).stdout

    def test_a_write_commits_only_its_own_path(self):
        from debops.mcp.tools.common import commit_or_report
        with open(os.path.join(self.tmpdir, 'operator.txt'), 'w') as fp:
            fp.write('the operator edited this\n')
        with open(os.path.join(self.tmpdir, 'ours.txt'), 'w') as fp:
            fp.write('the tool wrote this\n')
        with exclusive_project(self.project, self.policy,
                               note='file.write ours.txt',
                               paths=['ours.txt']):
            outcome = commit_or_report(
                    self.project, 'file.write ours.txt', 'created',
                    paths=['ours.txt'])
            self.assertIsNone(outcome)
        show = self.git('show', '--stat', '--format=%s')
        self.assertIn('ours.txt', show)
        self.assertNotIn('operator.txt', show)
        # The operator's pending change stays pending
        self.assertIn('operator.txt',
                      self.git('status', '--porcelain'))

    def test_the_dirty_delta_commits_what_the_block_created(self):
        # The custom-tool case: no declared paths, so the exit commits
        # the paths which became dirty during the block — a run which
        # creates a tree — and nothing the operator left pending
        with open(os.path.join(self.tmpdir, 'operator.txt'), 'w') as fp:
            fp.write('the operator edited this\n')
        with exclusive_project(self.project, self.policy,
                               note='run site'):
            os.makedirs(os.path.join(self.tmpdir, 'secret', 'created'))
            with open(os.path.join(self.tmpdir, 'secret', 'created',
                                   'key'), 'w') as fp:
                fp.write('the run created this\n')
        show = self.git('show', '--stat', '--format=%s')
        self.assertIn('secret/created/key', show)
        self.assertNotIn('operator.txt', show)
        self.assertIn('operator.txt',
                      self.git('status', '--porcelain'))


if __name__ == '__main__':
    unittest.main()
