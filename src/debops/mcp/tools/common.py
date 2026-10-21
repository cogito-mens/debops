# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared helpers for the DebOps MCP tools."""

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
import threading
import toml
import yaml

logger = logging.getLogger(__name__)

# Fold markers used in DebOps defaults files
FOLD_MARKER = re.compile(r'^\s*#\s*(\[{3}|\]{3})\d?\s*$')


def text_result(text, structured=None):
    """A tool result with the text content; 'structured' adds the
    machine-readable form of it ('structuredContent')."""
    result = {'content': [{'type': 'text', 'text': text}],
              'isError': False}
    if structured is not None:
        result['structuredContent'] = structured
    return result


def error_result(message, structured=None):
    result = {'content': [{'type': 'text', 'text': message}],
              'isError': True}
    if structured is not None:
        result['structuredContent'] = structured
    return result


def dump_default(value, flow=None):
    """Render a default value as YAML, without the trailing
    end-of-document marker PyYAML adds for scalar documents."""
    text = yaml.safe_dump(value, default_flow_style=flow, width=1000)
    lines = text.rstrip().splitlines()
    if lines and lines[-1] == '...':
        lines.pop()
    return '\n'.join(lines)


# Task keys which are not the module invocation; the module is the
# first key not in this set ('action', 'shell' and 'local_action' are
# module invocations and stay out of the set on purpose)
TASK_ATTRIBUTES = frozenset((
    'name', 'when', 'tags', 'register', 'become', 'vars', 'notify',
    'loop', 'loop_control', 'changed_when', 'failed_when', 'check_mode',
    'delegate_to', 'delegate_facts', 'run_once', 'environment', 'args',
    'ignore_errors', 'ignore_unreachable', 'no_log', 'until', 'retries',
    'delay', 'async', 'poll', 'timeout', 'connection', 'remote_user',
    'any_errors_fatal', 'debugger', 'diff', 'force_handlers',
    'max_fail_percentage', 'serial', 'strategy', 'throttle', 'order',
    'listen', 'collections', 'module_defaults', 'static',
    'import_playbook'))


def task_module(task):
    """The module a task invokes.

    The first key which is not a known task attribute; for the 'action:'
    shorthand, the first word of the action string.
    """
    for key in task:
        if key not in TASK_ATTRIBUTES:
            if key in ('action', 'shell', 'local_action'):
                value = str(task[key])
                return value.split(None, 1)[0] if value else key
            return key
    return ''


def render_task(task, detail='brief'):
    """Render one task or handler as a list of text lines."""
    name = str(task.get('name', '(unnamed)'))
    line = '- {} — {}'.format(name, task_module(task))
    listen = task.get('listen')
    if listen:
        if isinstance(listen, list):
            listen = ', '.join(str(item) for item in listen)
        line += ' (listen: {})'.format(listen)
    lines = [line]
    if detail == 'full':
        if 'when' in task:
            lines.append('    when: {}'.format(task['when']))
        if 'tags' in task:
            lines.append('    tags: {}'.format(
                    ', '.join(str(tag) for tag in task['tags'])))
    return lines


class ReadWriteLock(object):
    """A readers-writer lock (writer-preferring), from the standard
    library alone.

    Any number of readers may hold the read side; a writer waits for
    them to drain, and new readers queue behind a waiting writer so
    writers do not starve.
    """

    def __init__(self):
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    def acquire_read(self):
        with self._condition:
            while self._writer or self._writers_waiting:
                self._condition.wait()
            self._readers += 1

    def release_read(self):
        with self._condition:
            self._readers -= 1
            if not self._readers:
                self._condition.notify_all()

    def acquire_write(self):
        with self._condition:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._writers_waiting -= 1

    def release_write(self):
        with self._condition:
            self._writer = False
            self._condition.notify_all()

    @contextlib.contextmanager
    def read(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextlib.contextmanager
    def write(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()


# The project-wide lock: writers (executions, syncs, secrets) are
# exclusive; readers share
PROJECT_LOCK = ReadWriteLock()


def json_document(value):
    """Render a parsed document as one JSON text block, for the 'json'
    format of the document tools. Keys are sorted so that the output is
    deterministic; YAML exotica (timestamps) degrade to strings."""
    return json.dumps(value, indent=2, sort_keys=True, default=str) \
        + '\n'


# The canonical values of the document tools' 'format' argument (the
# schema enum); 'text' is the human rendering, the rest are one
# serialized document
OUTPUT_FORMATS = ('text', 'json', 'yaml', 'toml')

# The variants a model reaches for when it guesses the format value;
# absorbed quietly at the boundary
FORMAT_ALIASES = {
    'raw': 'text',
    'txt': 'text',
    'text/plain': 'text',
    'application/json': 'json',
    'yml': 'yaml',
    'text/yaml': 'yaml',
    'text/x-yaml': 'yaml',
    'application/yaml': 'yaml',
    'application/toml': 'toml',
}


def parse_output_format(arguments):
    """The 'format' argument of a document tool, normalized.

    Raises ValueError with the valid set for anything unrecognized.
    """
    raw = arguments.get('format')
    output_format = str(raw or 'text').strip().lower()
    output_format = FORMAT_ALIASES.get(output_format, output_format)
    if output_format not in OUTPUT_FORMATS:
        raise ValueError('invalid format: {!r} (text, json, yaml or '
                         'toml)'.format(raw))
    return output_format


def _has_null(value):
    """Whether the document tree contains a null (TOML has none, and
    the toml library silently drops such keys)."""
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(entry) for entry in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_null(entry) for entry in value)
    return False


def render_document(value, output_format):
    """Serialize a document for the text channel per the format."""
    if output_format == 'yaml':
        # Sorted keys, so that the output is deterministic
        return yaml.safe_dump(value, allow_unicode=True)
    if output_format == 'toml':
        if _has_null(value):
            raise ValueError('cannot render this document as TOML: '
                             'it contains a null value')
        return toml.dumps(value)
    return json_document(value)


def document_result(value, output_format):
    """The document in the requested format, as a tool result."""
    try:
        return text_result(render_document(value, output_format))
    except ValueError as errmsg:
        return error_result(str(errmsg))


def commit_or_report(project, note, outcome):
    """Commit with MCP provenance; on failure, an error result saying
    the change is not recorded. None on success. The commit is part of
    the write contract: an explicit, attributable record of the change.
    """
    try:
        project.commit(message='DebOps MCP: ' + note)
    except Exception as errmsg:
        return error_result('{}, but the commit failed: {} (the '
                            'change is not recorded in git)'
                            .format(outcome, errmsg))
    return None


def is_git_tracked_project(project):
    """Whether the project directory is git-tracked (work tree)."""
    return os.path.lexists(os.path.join(project.path, '.git'))


def require_git_tracked(project):
    """An error result when the project is not a git repository, else
    None. Writes are recoverable and attributable only with git."""
    if not is_git_tracked_project(project):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')
    return None


def confirmation_flow(context, message, required=False):
    """The shared confirmation flow for self-confirming tools.

    Returns True when confirmed (or the client cannot elicit — the
    skip is logged), False when the user declined, and a result dict
    when the user must be asked first ('input_required'). With
    'required', a client which cannot elicit is refused instead of
    proceeded. In a task the question is the task's outstanding input
    request, answered via 'tasks/update'.
    """
    from ..server import _supports_form_elicitation
    answers = context.input_responses or {}
    if 'confirmation' in answers:
        answer = answers.get('confirmation') or {}
        content = answer.get('content') or {}
        return (answer.get('action') == 'accept'
                and content.get('confirmed') is True)
    request = {
        'method': 'elicitation/create',
        'params': {
            'mode': 'form',
            'message': message,
            'requestedSchema': {
                'type': 'object',
                'properties': {
                    'confirmed': {
                        'type': 'boolean',
                        'title': 'Confirm',
                        'description': 'I understand and want to '
                        'proceed',
                        'default': False}},
                'required': ['confirmed'],
            },
        },
    }
    if context.task is not None:
        answer = context.task.ask('confirmation', request)
        content = answer.get('content') or {}
        return (answer.get('action') == 'accept'
                and content.get('confirmed') is True)
    if not _supports_form_elicitation(context.client_capabilities):
        if required:
            logger.notice('Refused: the client cannot elicit and the '
                          'policy requires confirmation')
            return error_result(
                    'the operation requires user confirmation, which '
                    'this client cannot provide (no form elicitation); '
                    'refused by the project policy '
                    '(project.mcp.confirm_required)')
        logger.notice('Proceeding without user confirmation: the '
                      'client cannot elicit')
        return True
    return {'resultType': 'input_required',
            'inputRequests': {'confirmation': request}}


class ProjectResourceError(RuntimeError):
    """An exclusive operation refused before it started (disk full)."""


def check_free_space(policy, project):
    """Refuse when the project's filesystem is nearly full.

    A write which dies mid-way — a git object, a checkout — can leave
    state behind that a later commit would happily pick up. An absolute
    floor is used: a percentage means nothing on its own.
    """
    min_free = getattr(policy, 'min_free_bytes', 64 * 1024 * 1024)
    # The view root may not exist yet (a fresh skeleton) — or at all
    # (no active view); the filesystem of the nearest existing ancestor
    # is the one being written to
    path = policy.view_root if policy.view_root is not None \
        else project.path
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent
    free = shutil.disk_usage(path).free
    if free < min_free:
        raise ProjectResourceError(
                'not enough free disk space: {} MiB left, the policy '
                'requires {} MiB (project.mcp.min_free_mb)'.format(
                    free // (1024 * 1024), min_free // (1024 * 1024)))


def _read_env_file(path):
    """A dotenv file as a dict; an unreadable file reads as empty."""
    try:
        from dotenv import dotenv_values
        return {key: value for key, value in dotenv_values(path).items()
                if value is not None}
    except Exception:
        logger.warning('Cannot read the environment file: %s', path,
                       exc_info=True)
        return {}


def mcp_environ(project):
    """The environment for the tool subprocesses of the MCP server.

    The process environment, then the project environment, then the
    project MCP environment files ('.debops/mcp/environment' for plain
    variables, '.debops/mcp/.env' for tokens), parsed fresh at each
    call so that a change applies to the next run without a restart.
    """
    env = os.environ.copy()
    env.update(project.config._env_vars)
    for name in ('environment', '.env'):
        path = os.path.join(project.path, '.debops', 'mcp', name)
        if os.path.isfile(path):
            env.update(_read_env_file(path))
    return env


def _project_lock_path(project, policy):
    """The flock file for cross-process exclusion on this project."""
    git_dir = os.path.join(project.path, '.git')
    if os.path.isdir(git_dir):
        return os.path.join(git_dir, 'mcp.lock')
    if policy.view_root is not None:
        return os.path.join(policy.view_root, '.mcp.lock')
    return os.path.join(project.path, '.mcp.lock')


# The lock file descriptor held by this thread. A child process may
# inherit it, so that the lock outlives the server when an execution is
# detached: the lock belongs to the run, not to the process which
# started it
_LOCK_STATE = threading.local()


class ProjectLockedError(RuntimeError):
    """The project is locked by another process (a run in flight).

    A RuntimeError subclass: the contention is an expected, retryable
    condition, so the tools report it as a result, not an internal
    error.
    """


def current_lock_fd():
    """The file descriptor of the project lock held by this thread."""
    return getattr(_LOCK_STATE, 'fd', None)


@contextlib.contextmanager
def project_flock(project, policy, exclusive):
    """Hold an flock(2) on the project's lock file.

    Exclusive holders fail fast when another process holds the lock;
    readers share it. The kernel releases the lock when the process
    dies, so a crashed daemon never wedges the project.
    """
    path = _project_lock_path(project, policy)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fp = open(path, 'a')
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if exclusive:
            mode |= fcntl.LOCK_NB
        try:
            fcntl.flock(fp.fileno(), mode)
        except BlockingIOError:
            raise ProjectLockedError(
                    'another process is working on this project '
                    '({} is locked)'.format(path))
        previous = getattr(_LOCK_STATE, 'fd', None)
        _LOCK_STATE.fd = fp.fileno()
        try:
            yield fp
        finally:
            _LOCK_STATE.fd = previous
    finally:
        fp.close()


@contextlib.contextmanager
def exclusive_project(project, policy, note=''):
    """Hold the in-process write lock and the cross-process exclusive
    lock, in that order. On exit, pending changes are committed (when
    the project has automatic commits enabled, which is the default),
    so the tree stays clean by construction.

    The operation is refused before it starts when the filesystem is
    nearly full.
    """
    check_free_space(policy, project)
    with PROJECT_LOCK.write():
        with project_flock(project, policy, exclusive=True):
            try:
                yield
            finally:
                # A failed ambient commit must not mask the
                # operation's own outcome
                try:
                    project.auto_commit(message='DebOps MCP: ' + note
                                        if note else
                                        'DebOps MCP operation')
                except Exception:
                    logger.warning('Auto-commit after %s failed',
                                   note or 'the operation',
                                   exc_info=True)


@contextlib.contextmanager
def shared_project(project, policy):
    """Hold the in-process read lock and the cross-process shared lock,
    in that order."""
    with PROJECT_LOCK.read():
        with project_flock(project, policy, exclusive=False):
            yield
