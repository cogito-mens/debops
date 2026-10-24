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
import json
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

# The log file name of an execution
_LOG_NAME = re.compile(r'^execution-(\d+)\.log$')


def _cache_base():
    """The user's cache directory (the XDG cache home)."""
    base = os.environ.get('XDG_CACHE_HOME')
    if not base:
        base = os.path.join(os.path.expanduser('~'), '.cache')
    return base


def cache_root():
    """The root of the run log cache: the XDG cache directory."""
    return os.path.join(_cache_base(), CACHE_SUBDIRECTORY)


def path_key(path, name, view):
    """The key of a project path and view: the path hash keeps two
    projects with the same name apart, the view keeps the views of
    one project apart, and the sanitized name keeps the key
    recognizable. With view='' the name carries no view — the daemon
    socket's naming, one per project, its view switchable inside."""
    digest = hashlib.sha256(
            '{}\0{}'.format(os.path.realpath(path),
                            view).encode('utf-8', 'replace'))
    key = re.sub(r'[^A-Za-z0-9._-]', '-', str(name or 'project'))
    if view:
        key += '-' + re.sub(r'[^A-Za-z0-9._-]', '-', str(view))
    return '{}-{}'.format(digest.hexdigest()[:8], key)


def project_key(project, view=None):
    """The name of the run log directory of a project view.

    The executions of one view are serialized by its lock, so the
    logs in one directory are never pruned while a run of that view
    writes them.
    """
    if view is None:
        view = getattr(project, 'view', None) or ''
    return path_key(project.path, project.name, view)


def project_directory(project, keep=KEEP_LOGS, max_age=MAX_LOG_AGE):
    """The run log directory of a project, created if needed.

    The old logs are pruned on the way: the cache is not a place where
    a long-lived server accumulates run logs forever.
    """
    base = _cache_base()
    path = os.path.join(base, CACHE_SUBDIRECTORY, project_key(project))
    try:
        os.makedirs(path, exist_ok=True)
        # A diff or a debug run renders secrets into the log; our chain
        # below the user's cache directory is private to the account
        directory = path
        while directory.startswith(base + os.sep):
            os.chmod(directory, 0o700)
            directory = os.path.dirname(directory)
    except OSError as errmsg:
        logger.warning('Cannot create the run log directory %s: %s',
                       path, errmsg)
        return None
    prune(path, keep=keep, max_age=max_age)
    return path


def log_path(project, execution_id):
    """The path of the log file of one execution, or None when the
    cache directory is not usable."""
    directory = project_directory(project)
    if directory is None:
        return None
    return os.path.join(directory, 'execution-{}.log'.format(execution_id))


def next_execution_id(directory):
    """The next free execution id in a run log directory.

    The ids come from a scan of the directory rather than a process
    counter, so a restarted server does not reuse the id of a run whose
    log still exists — a query for an old run must never return a
    newer one. The caller holds the project's exclusive lock, which
    makes the scan-and-create race-free.
    """
    highest = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 1
    for name in names:
        match = _LOG_NAME.match(name)
        if match is not None:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def new_log(project, keep=KEEP_LOGS, max_age=MAX_LOG_AGE):
    """The (id, path) of the log file for a new execution.

    Returns (None, None) when the cache directory is not usable.
    """
    directory = project_directory(project, keep=keep, max_age=max_age)
    if directory is None:
        return None, None
    execution_id = next_execution_id(directory)
    return execution_id, os.path.join(
            directory, 'execution-{}.log'.format(execution_id))


def write_metadata(log_path, metadata):
    """Write the sidecar metadata of an execution, atomically.

    The sidecar lets a later session list and address the runs without
    parsing the logs. The write is private, like the log itself.
    """
    path = os.path.splitext(log_path)[0] + '.json'
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fp:
        os.fchmod(fp.fileno(), 0o600)
        json.dump(metadata, fp, indent=2, sort_keys=True)
        fp.write('\n')
    os.rename(tmp, path)


def read_metadata(log_path):
    """The sidecar metadata of an execution, or None."""
    path = os.path.splitext(log_path)[0] + '.json'
    try:
        with open(path, 'r', encoding='utf-8') as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


_TASK_MARKER = re.compile(r'^TASK \[', re.M)

# The implicit fact gathering of a play prints a TASK banner which
# the '--list-tasks' tally does not count; excluding it keeps the
# position converging to the tally exactly. A role task named
# 'Gathering Facts' carries its role prefix and is unaffected
_FACTS_MARKER = re.compile(r'^TASK \[Gathering Facts\]', re.M)


def task_count(log_path):
    """The TASK markers of a run log so far: one per task per play —
    the position of the run against its tally. Returns None when the
    log cannot be read."""
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') \
                as fp:
            text = fp.read()
    except OSError:
        return None
    return (len(_TASK_MARKER.findall(text))
            - len(_FACTS_MARKER.findall(text)))


def current_span(counts, done):
    """The index of the tally span holding the running task: the
    playbook whose task range contains the position 'done'. None
    without spans."""
    if not counts:
        return None
    upto = 0
    for index, count in enumerate(counts):
        upto += count
        if done <= upto or index == len(counts) - 1:
            return index
    return None


def active_log(project):
    """The log of the in-flight execution, if one is writing it: the
    newest whose sidecar lacks the completion marker. Pair it with
    the busy probe — a finished run's sidecar carries finishedAt."""
    for _id, path in reversed(list_logs(project)):
        metadata = read_metadata(path)
        if metadata is not None and 'finishedAt' not in metadata:
            return path
    return None


def directory_path(project):
    """The run log directory of a project view, without creating it."""
    return os.path.join(cache_root(), project_key(project))


def list_logs(project):
    """The (id, path) pairs of the run logs of a project view, by id."""
    directory = directory_path(project)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    entries = []
    for name in names:
        match = _LOG_NAME.match(name)
        if match is not None:
            entries.append((int(match.group(1)),
                            os.path.join(directory, name)))
    entries.sort()
    return entries


def execution_log(project, execution_id):
    """The (id, path) of an existing run log, or (None, None).

    The id is an integer, or 'last' for the newest run.
    """
    entries = list_logs(project)
    if not entries:
        return None, None
    if execution_id == 'last':
        return entries[-1]
    for entry_id, path in entries:
        if entry_id == execution_id:
            return entry_id, path
    return None, None


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
