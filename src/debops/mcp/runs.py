# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The run logs of the playbook executions.

Every execution writes its output to a log file in the user's cache
directory, one directory per project. The file is not a convenience: an
execution which was detached from its server (the harness closed, the
server killed) completes on its own, and the log file is the only record
of what it did.

The cache directory is the right home for it. The logs are scratch data
by definition, they never belong to the project (a project must not
grow a directory of run logs), and the cache is the one place a user
(or a janitor) can empty without touching anything that matters.
"""

import hashlib
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

# The run logs live in this subdirectory of the user's cache directory
CACHE_SUBDIRECTORY = os.path.join('debops', 'mcp', 'runs')

# How many log lines the task status carries
TAIL_LINES = 20

# The size limit of the task status tail, in bytes
TAIL_BYTES = 4096

# The longest single line of the task status tail
TAIL_LINE_LENGTH = 200

# The run logs kept per project; the older ones are pruned
KEEP_LOGS = 10

# The age at which a run log is pruned, in seconds
MAX_LOG_AGE = 7 * 24 * 60 * 60


def cache_root():
    """The root of the run log cache: the XDG cache directory."""
    base = os.environ.get('XDG_CACHE_HOME')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, CACHE_SUBDIRECTORY)


def project_key(project):
    """The name of the run log directory of a project view.

    The hash of the project path keeps two projects with the same name
    apart, and the view keeps the views of one project apart: the
    executions of one view are serialized by its lock, so the logs in
    one directory are never pruned while a run of that view writes
    them. The name itself is only there to make the directory
    recognizable.
    """
    path = os.path.realpath(project.path)
    view = getattr(project, 'view', None) or ''
    digest = hashlib.sha256(
            '{}\0{}'.format(path, view).encode('utf-8', 'replace'))
    name = re.sub(r'[^A-Za-z0-9._-]', '-', str(project.name or 'project'))
    if view:
        name += '-' + re.sub(r'[^A-Za-z0-9._-]', '-', str(view))
    return '{}-{}'.format(digest.hexdigest()[:8], name)


def project_directory(project):
    """The run log directory of a project, created if needed.

    The old logs are pruned on the way: the cache is not a place where
    a long-lived server accumulates run logs forever.
    """
    path = os.path.join(cache_root(), project_key(project))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as errmsg:
        logger.warning('Cannot create the run log directory %s: %s',
                       path, errmsg)
        return None
    prune(path)
    return path


def log_path(project, execution_id):
    """The path of the log file of one execution, or None when the
    cache directory is not usable."""
    directory = project_directory(project)
    if directory is None:
        return None
    return os.path.join(directory, 'execution-{}.log'.format(execution_id))


def prune(directory, keep=KEEP_LOGS, max_age=MAX_LOG_AGE):
    """Drop the run logs which are too old or beyond the kept count."""

    def mtime(path):
        # A concurrent run may drop a file between the listing and the
        # stat; treat it as the oldest
        try:
            return os.stat(path).st_mtime
        except OSError:
            return 0

    try:
        entries = [os.path.join(directory, name)
                   for name in os.listdir(directory)
                   if name.endswith('.log')]
    except OSError:
        return
    entries.sort(key=mtime)
    now = time.time()
    for index, path in enumerate(entries):
        if index < len(entries) - keep or now - mtime(path) > max_age:
            try:
                os.unlink(path)
            except OSError:
                pass


def read_tail(path, limit):
    """The last 'limit' bytes of a log file, as text.

    A run log grows with the run and only its end matters (the recap is
    the last thing ansible-playbook prints). The read is bounded, so a
    long run does not pull megabytes into memory.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return ''
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fp:
            if size > limit:
                fp.seek(size - limit)
                # The first line is probably cut in half; drop it
                fp.readline()
            return fp.read()
    except OSError as errmsg:
        logger.warning('Cannot read the run log %s: %s', path, errmsg)
        return ''


def status_tail(lines, count=TAIL_LINES, limit=TAIL_BYTES):
    """The tail of a run, as a task status message.

    The lines are the ones the run printed most recently; each one is
    shortened, so that a chatty task cannot flood the status.
    """
    text = []
    for line in list(lines)[-count:]:
        line = line.rstrip('\n')
        if len(line) > TAIL_LINE_LENGTH:
            line = line[:TAIL_LINE_LENGTH] + '…'
        text.append(line)
    result = '\n'.join(text)
    if len(result) > limit:
        result = '…\n' + result[-(limit - 2):]
    return result
