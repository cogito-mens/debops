# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the project run lock (debops.filelock).

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import fcntl
import os
import shutil
import tempfile
import threading
import time
import unittest

from debops.filelock import RunLockedError, run_lock, run_lock_path


def make_project(path, project_type='legacy', view=None, views=None):
    return type('Project', (), {
            'path': path,
            'project_type': project_type,
            'view': view,
            'config': type('Config', (), {
                    'raw': {'views': views or {}}})()})()


class RunLockPathTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-filelock-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_a_git_project_locks_in_the_git_metadata(self):
        os.makedirs(os.path.join(self.tmpdir, '.git'))
        self.assertEqual(run_lock_path(make_project(self.tmpdir)),
                         os.path.join(self.tmpdir, '.git', 'mcp.lock'))

    def test_a_legacy_project_locks_at_the_inventory_root(self):
        self.assertEqual(run_lock_path(make_project(self.tmpdir)),
                         os.path.join(self.tmpdir, 'ansible',
                                      '.mcp.lock'))

    def test_a_modern_project_locks_at_the_view_root(self):
        project = make_project(self.tmpdir, project_type='modern',
                               view='admin', views={'admin': {}})
        self.assertEqual(run_lock_path(project),
                         os.path.join(self.tmpdir, 'ansible', 'views',
                                      'admin', '.mcp.lock'))

    def test_without_a_view_the_lock_is_at_the_project_root(self):
        project = make_project(self.tmpdir, project_type='modern')
        self.assertEqual(run_lock_path(project),
                         os.path.join(self.tmpdir, '.mcp.lock'))


class RunLockTestCase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='debops-filelock-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.project = make_project(self.tmpdir)

    def test_contention_fails_fast(self):
        with run_lock(self.project):
            with self.assertRaises(RunLockedError) as context:
                with run_lock(self.project):
                    pass
        self.assertIn('is locked', str(context.exception))

    def test_the_lock_releases_on_exit(self):
        with run_lock(self.project):
            pass
        with run_lock(self.project):
            pass

    def test_wait_blocks_until_the_lock_is_free(self):
        acquired = []
        with run_lock(self.project):
            thread = threading.Thread(
                    target=lambda: acquired.append(
                            self._acquire_waiting()), daemon=True)
            thread.start()
            time.sleep(0.5)
            # Still waiting on the held lock
            self.assertEqual(acquired, [])
            released_at = time.monotonic()
        thread.join(timeout=10)
        self.assertEqual(len(acquired), 1)
        # It went through only after the release
        self.assertGreaterEqual(acquired[0], released_at)

    def test_a_bounded_wait_gives_up_after_the_deadline(self):
        with run_lock(self.project):
            started = time.monotonic()
            with self.assertRaises(RunLockedError):
                with run_lock(self.project, wait=0.4):
                    pass
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.35)
        self.assertLess(waited, 5)

    def test_a_bounded_wait_acquires_when_freed(self):
        path = run_lock_path(self.project)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        holder = open(path, 'a')
        self.addCleanup(holder.close)
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)

        def release_soon():
            time.sleep(0.3)
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

        thread = threading.Thread(target=release_soon, daemon=True)
        thread.start()
        started = time.monotonic()
        with run_lock(self.project, wait=5):
            waited = time.monotonic() - started
        # It waited for the release, not the deadline
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 4)

    def _acquire_waiting(self):
        with run_lock(self.project, wait=True):
            return time.monotonic()

    def test_the_lock_file_is_created(self):
        with run_lock(self.project) as path:
            self.assertTrue(os.path.exists(path))


if __name__ == '__main__':
    unittest.main()
