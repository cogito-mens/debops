# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The project run lock: an flock(2) shared by the command line and
the MCP daemon, so that an execution and a project-modifying command
never run at the same time."""

import contextlib
import fcntl
import os
import time


def run_lock_path(project):
    """The lock file of a project view: inside the git metadata when
    there is one (invisible to the working tree), next to the view
    root or the project root otherwise."""
    git_dir = os.path.join(project.path, '.git')
    if os.path.isdir(git_dir):
        return os.path.join(git_dir, 'mcp.lock')
    view_root = None
    if project.project_type == 'legacy':
        view_root = os.path.join(project.path, 'ansible')
    elif project.view and project.view in project.config.raw.get(
            'views', {}):
        view_root = os.path.join(project.path, 'ansible', 'views',
                                 project.view)
    if view_root is not None:
        return os.path.join(os.path.realpath(view_root), '.mcp.lock')
    return os.path.join(project.path, '.mcp.lock')


class RunLockedError(RuntimeError):
    """The project is locked by another process (a run in flight).

    A RuntimeError subclass: the contention is an expected, retryable
    condition, so the commands report it as an answer, not an
    internal error.
    """


def flock_wait(fp, mode, wait):
    """flock(2) with a wait policy: 0 fails fast, None blocks
    forever, a positive number of seconds polls until that deadline.
    Raises RunLockedError on contention, naming the lock file."""
    if wait is None:
        fcntl.flock(fp.fileno(), mode)
        return
    mode |= fcntl.LOCK_NB
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fp.fileno(), mode)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RunLockedError(
                        'another process is working on this project '
                        '({} is locked)'.format(fp.name))
            time.sleep(0.05)


@contextlib.contextmanager
def run_lock(project, wait=False):
    """Hold the project's exclusive run lock for the duration of a
    command.

    'wait' accepts False (fail fast), True (block until the lock is
    free) and a number of seconds (poll until that deadline). The
    kernel releases the lock when the process dies, so a crashed
    command never wedges the project.
    """
    path = run_lock_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fp = open(path, 'a')
    try:
        if wait is True:
            wait = None
        elif wait is False:
            wait = 0
        flock_wait(fp, fcntl.LOCK_EX, wait)
        yield path
    finally:
        fp.close()
