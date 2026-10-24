# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The task registry of the MCP Tasks extension
('io.modelcontextprotocol/tasks').

A task is a durable handle for a long-running tool call: the client
gets the handle immediately and polls 'tasks/get' for the outcome, so
a disconnect strands neither the work nor its result. The registry is
in-memory with a TTL; it survives a dead worker thread and any client
disconnect (the specification's durability bar), and a daemon restart
loses the tasks, which the specification allows. The advertised TTL is
honest: it clamps to the terminal retention when the task finishes.
"""

import logging
import threading
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# The terminal states of a task
TERMINAL_STATES = ('completed', 'failed', 'cancelled')

# How long a finished task is kept for the client to collect it
TERMINAL_RETENTION = 300  # seconds


def _now():
    return datetime.now(timezone.utc)


def _iso8601(moment):
    return moment.isoformat().replace('+00:00', 'Z')


class Task(object):
    """One running or finished piece of work, with its handle."""

    def __init__(self, ttl_ms, poll_interval_ms=1000):
        self.task_id = str(uuid.uuid4())
        self.status = 'working'
        self.status_message = None
        self.created_at = _now()
        self.last_updated_at = self.created_at
        self.ttl_ms = ttl_ms
        self.poll_interval_ms = poll_interval_ms
        # The cancellation event shared with the tool's context; set by
        # 'tasks/cancel' or the server-wide cancel
        self.cancel_event = threading.Event()
        self.result = None
        self.error = None
        # The pid of the client process which started the task: its
        # owner cancels it outright; a run task started by another
        # client asks for the trust first
        self.owner_pid = None
        # The run bookkeeping of an execution task: the mode, the
        # playbooks and the tally at the start, the advancing task
        # index and the current task name during the run; the front
        # ends render their progress from it
        self.run_info = None
        # Outstanding input requests (key -> request) and the answers
        # (key -> response); each key gets an event the asking thread
        # waits on
        self._pending = {}
        self._answers = {}
        self._answered = {}
        self._lock = threading.Lock()
        # Set by the registry: called with the task on every change
        self._on_change = None

    def _changed(self):
        if self._on_change is not None:
            self._on_change(self)

    def describe(self):
        """The task as the wire shape, with its status payload."""
        with self._lock:
            description = {
                'taskId': self.task_id,
                'status': self.status,
                'createdAt': _iso8601(self.created_at),
                'lastUpdatedAt': _iso8601(self.last_updated_at),
                'ttlMs': self.ttl_ms,
                'pollIntervalMs': self.poll_interval_ms,
            }
            if self.status_message:
                description['statusMessage'] = self.status_message
            if self.run_info is not None:
                description['run'] = dict(self.run_info)
            if self.status == 'input_required':
                description['inputRequests'] = dict(self._pending)
            elif self.status == 'completed':
                description['result'] = self.result
            elif self.status == 'failed':
                description['error'] = self.error
            return description

    def creation_result(self):
        """The CreateTaskResult returned to the original request."""
        result = self.describe()
        result['resultType'] = 'task'
        return result

    def set_status_message(self, message):
        with self._lock:
            self.status_message = message
            self.last_updated_at = _now()
        self._changed()

    def update_run(self, **fields):
        """Update the run bookkeeping; the polling clients pick it up
        with the next answer. The change notification stays with the
        status cadence — a run header stream is too chatty for one."""
        with self._lock:
            if self.run_info is None:
                self.run_info = {}
            self.run_info.update(fields)
            self.last_updated_at = _now()

    def ask(self, key, request):
        """Block the task on an input request until the client answers.

        The task moves to 'input_required' with the request outstanding;
        'tasks/update' answers it. A cancellation wakes the asker with
        RequestCancelled.
        """
        # A local import: tasks are cancelled with the same exception
        # the request-level cancellation uses
        from .server import RequestCancelled
        event = threading.Event()
        with self._lock:
            self._pending[key] = request
            self._answered[key] = event
            self.status = 'input_required'
            self.last_updated_at = _now()
        self._changed()
        try:
            while not event.wait(0.5):
                if self.cancel_event.is_set():
                    raise RequestCancelled()
            with self._lock:
                return self._answers.pop(key, {})
        finally:
            with self._lock:
                self._pending.pop(key, None)
                self._answered.pop(key, None)
                if self.status == 'input_required':
                    self.status = 'working'
                    self.last_updated_at = _now()
            self._changed()

    def answer(self, key, response):
        """Record the answer to an outstanding input request.

        Unknown or already-answered keys are ignored, per the
        specification.
        """
        with self._lock:
            event = self._answered.get(key)
            if event is None:
                return False
            self._answers[key] = response
            event.set()
            return True

    def _finish(self):
        """Terminal transitions clamp the advertised TTL to the
        terminal retention: the work may take its hour, but the answer
        is kept for the client to collect, not for the hour. Call with
        the lock held."""
        self.ttl_ms = TERMINAL_RETENTION * 1000
        self.last_updated_at = _now()

    def complete(self, result):
        with self._lock:
            self.status = 'completed'
            self.result = result
            self._finish()
        self._changed()

    def fail(self, error):
        with self._lock:
            self.status = 'failed'
            self.error = error
            self._finish()
        self._changed()

    def mark_cancelled(self):
        with self._lock:
            self.status = 'cancelled'
            self._finish()
        self._changed()

    def cancel(self):
        self.cancel_event.set()


class TaskRegistry(object):
    """The tasks of one server, reaped on access after their TTL."""

    def __init__(self):
        self._tasks = {}
        self._lock = threading.Lock()
        # Set by the server: called with the task on every change
        self.on_task_change = None

    def create(self, ttl_ms, poll_interval_ms=1000):
        task = Task(ttl_ms, poll_interval_ms)
        task._on_change = self._changed
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def _changed(self, task):
        if self.on_task_change is not None:
            self.on_task_change(task)

    def get(self, task_id):
        """The task, or None when unknown or expired.

        An expired task which never finished is cancelled first, so its
        workers stop; a finished one is kept briefly for the client to
        collect the outcome.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            now = _now()
            age = (now - task.created_at).total_seconds() * 1000
            if task.status in TERMINAL_STATES:
                idle = ((now - task.last_updated_at).total_seconds()
                        * 1000)
                if idle > TERMINAL_RETENTION * 1000:
                    del self._tasks[task_id]
                    return None
            elif age > task.ttl_ms:
                # The task outlived its TTL without finishing
                task.cancel()
                task.mark_cancelled()
            return task

    def cancel(self, task_id):
        task = self.get(task_id)
        if task is None:
            return False
        task.cancel()
        return True
