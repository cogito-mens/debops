# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The execution queue: named job batches, dispatched sequentially
by a polling thread.

The model: the queue is a sequence of named batches; jobs run
sequentially within a batch, batches sequentially in the queue, and
a job failing fails its whole batch (the run logs keep what ran).
A batch can be marked pending review — it and the batches after it
then wait for a trusted client to un-pend or remove it. The queue
dispatches only when started. In-memory by design: a daemon restart
drops the queue; the run logs keep the record of what ran.
"""

import itertools
import logging
import os
import re
import threading
import time

from datetime import datetime, timezone

from . import runs
from .server import ToolContext
from .tools.execute import (_execute_playbook, _make_runner,
                            _playbook_targets, _probe_tally)
from .tools.executions import project_busy
from .tools.views import tool_view_switch

logger = logging.getLogger(__name__)

BATCH_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')

# The job states: queued -> running -> done | failed | cancelled
JOB_STATES = ('queued', 'running', 'done', 'failed', 'cancelled')

# The batch states: queued -> running -> done | failed; removed
# batches leave the queue at once
BATCH_STATES = ('queued', 'running', 'done', 'failed')


def _utcnow():
    return datetime.now(timezone.utc).isoformat().replace(
            '+00:00', 'Z')


class JobQueueError(RuntimeError):
    """A queue operation's semantic refusal (the tools render it as
    an error result)."""


class JobQueue(object):
    """The execution queue of one daemon: the batches, the dispatch
    switch, and the polling scheduler thread."""

    def __init__(self, server, interval=1.0):
        self._server = server
        self._interval = interval
        self._lock = threading.Lock()
        self._batches = []
        self._job_numbers = itertools.count(1)
        self.started = False
        self.last_failure = None
        self._active_job = None
        self._stop_event = threading.Event()
        self._thread = None

    # -- the lifecycle -------------------------------------------

    def start_thread(self):
        """Start the scheduler thread (idempotent)."""
        if self._thread is None:
            self._thread = threading.Thread(
                    target=self._loop, name='debops-mcp-queue',
                    daemon=True)
            self._thread.start()

    def stop_thread(self):
        """Stop the scheduler thread; an in-flight job runs out."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # -- the batch operations --------------------------------------

    def batch_create(self, name):
        """A new batch at the tail of the queue."""
        if not BATCH_NAME.match(name):
            raise JobQueueError(
                    'invalid batch name: {!r} (letters, digits, '
                    'dash, underscore, dot)'.format(name))
        with self._lock:
            if any(batch['name'] == name for batch in self._batches):
                raise JobQueueError(
                        'the batch exists already: {}'.format(name))
            batch = {'name': name, 'jobs': [], 'pending': False,
                     'state': 'queued', 'createdAt': _utcnow(),
                     'failure': None}
            self._batches.append(batch)
            return {'name': name, 'position': len(self._batches)}

    def batch_remove(self, name):
        """Drop a batch; its queued jobs die with it (a running job
        finishes)."""
        with self._lock:
            batch = self._find_batch(name)
            dropped = len([job for job in batch['jobs']
                           if job['state'] == 'queued'])
            self._batches.remove(batch)
            batch['state'] = 'removed'
            return {'removed': name, 'jobsDropped': dropped}

    def batch_prune(self, name):
        """Drop a batch and every batch queued after it."""
        with self._lock:
            batch = self._find_batch(name)
            index = self._batches.index(batch)
            dropped = self._batches[index:]
            del self._batches[index:]
            for entry in dropped:
                entry['state'] = 'removed'
            return {'removed': [entry['name'] for entry in dropped]}

    def batch_pending(self, name, pending):
        """Mark a batch for review, or clear the mark (the trusted
        clients only; the caller checked)."""
        with self._lock:
            batch = self._find_batch(name)
            batch['pending'] = pending
            return {'name': name, 'pending': pending}

    def batch_info(self, name):
        """The batch detail: its jobs in order, the states and the
        estimates."""
        with self._lock:
            batch = self._find_batch(name)
            return self._describe_batch(batch, detail=True)

    # -- the queue operations --------------------------------------

    def queue_list(self):
        """The batches in planned execution order."""
        with self._lock:
            return {'started': self.started,
                    'batches': [self._describe_batch(batch)
                                for batch in self._batches],
                    'activeJob': self._describe_active(),
                    'lastFailure': self.last_failure}

    def queue_clear(self):
        """Drop every queued batch; the running job finishes."""
        with self._lock:
            count = len(self._batches)
            for batch in self._batches:
                batch['state'] = 'removed'
            self._batches = []
            return {'cleared': count}

    def queue_start(self):
        """Begin or resume the dispatch."""
        with self._lock:
            was = self.started
            self.started = True
            return {'started': True, 'already': was}

    def queue_stop(self):
        """Finish the running job, dispatch nothing more."""
        with self._lock:
            was = self.started
            self.started = False
            return {'started': False, 'already': not was}

    # -- the job operations ----------------------------------------

    def job_add(self, batch_name, arguments, project, policy):
        """A new job at the tail of a batch. The tally probe runs at
        add time (the estimate is the queue's bookkeeping)."""
        mode = str(arguments.get('mode') or 'run')
        if mode not in ('check', 'run', 'debug'):
            raise JobQueueError(
                    "the mode is one of: 'check', 'run', 'debug'")
        try:
            targets = _playbook_targets(arguments)
        except ValueError as errmsg:
            raise JobQueueError(str(errmsg))
        runner = _make_runner(project, arguments, mode == 'check',
                              policy)
        tally = _probe_tally(runner, project, policy)
        if tally is None:
            raise JobQueueError(
                    'the tally probe failed for the job (the '
                    '--list-tasks/--list-hosts parse); the playbook '
                    'must list cleanly to be queued')
        with self._lock:
            batch = self._find_batch(batch_name)
            job = {'id': 'job-{}'.format(next(self._job_numbers)),
                   'batch': batch_name, 'mode': mode,
                   'targets': targets,
                   'options': {key: value for key, value
                               in arguments.items()
                               if key not in ('mode', 'batch')},
                   'view': project.view, 'createdAt': _utcnow(),
                   'state': 'queued', 'tally': tally,
                   'executionId': None, 'error': None}
            batch['jobs'].append(job)
            return self._describe_job(job, batch)

    def job_prune(self, job_id):
        """Remove a job and every job after it in its batch."""
        with self._lock:
            batch, index, job = self._find_job(job_id)
            if job['state'] == 'running':
                raise JobQueueError(
                        'the job is running; cancel the execution '
                        'instead (execution.cancel)')
            dropped = batch['jobs'][index:]
            del batch['jobs'][index:]
            for entry in dropped:
                entry['state'] = 'cancelled'
            return {'removed': [entry['id'] for entry in dropped]}

    def job_del(self, batch_name):
        """Remove the batch's last queued job."""
        with self._lock:
            batch = self._find_batch(batch_name)
            if not batch['jobs']:
                raise JobQueueError(
                        'the batch is empty: {}'.format(batch_name))
            job = batch['jobs'][-1]
            if job['state'] == 'running':
                raise JobQueueError(
                        'the last job is running; cancel the '
                        'execution instead (execution.cancel)')
            batch['jobs'].pop()
            job['state'] = 'cancelled'
            return {'removed': job['id']}

    # -- the scheduler ----------------------------------------------

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self._dispatch_once()
            except Exception:
                logger.exception('The queue dispatch failed')
            self._stop_event.wait(self._interval)

    def _dispatch_once(self):
        with self._lock:
            if not self.started or self._active_job is not None:
                return
            if not self._batches:
                return
            batch = self._batches[0]
            if batch['pending']:
                return
            job = next((entry for entry in batch['jobs']
                        if entry['state'] == 'queued'), None)
            if job is None:
                # An empty or exhausted batch completes
                batch['state'] = 'done'
                self._batches.pop(0)
                return
            batch['state'] = 'running'
            job['state'] = 'running'
            self._active_job = job
        try:
            self._run_job(batch, job)
        finally:
            with self._lock:
                self._active_job = None

    def _run_job(self, batch, job):
        server = self._server
        try:
            self._start_job(batch, job)
        except _TryLater:
            # The project is busy; the job is back at the head of
            # its batch and the next tick retries
            return
        except JobQueueError as errmsg:
            self._fail_batch(batch, job, str(errmsg))
            return
        except Exception as errmsg:
            logger.exception('The queue job %s failed to start',
                             job['id'])
            self._fail_batch(batch, job,
                             'internal error: {}'.format(errmsg))
            return
        outcome = self._wait_job(job)
        if outcome == 'done':
            with self._lock:
                job['state'] = 'done'
        else:
            self._fail_batch(batch, job, outcome)

    def _start_job(self, batch, job):
        """Start one queued job as a normal execution; raises
        JobQueueError on a refusal."""
        server = self._server
        project, policy = server.project, server.policy
        if project_busy(project, policy):
            # A manual run or another execution holds the project;
            # the job goes back to the head of its batch
            with self._lock:
                job['state'] = 'queued'
                batch['state'] = 'queued'
            raise _TryLater()
        if job['view'] is not None and job['view'] != project.view:
            # The job pinned its view at creation; the daemon follows
            switched = tool_view_switch(
                    project, policy, {'view': job['view']}, server)
            if switched.get('isError'):
                raise JobQueueError(
                        'the view switch to {} failed: {}'.format(
                                job['view'],
                                switched['content'][0]['text']))
            logger.notice('The queue switched the view to %s '
                          '(job %s)', job['view'], job['id'])
            project, policy = server.project, server.policy
        arguments = dict(job['options'])
        arguments['playbook'] = job['targets']
        if job['mode'] == 'debug':
            arguments['debug'] = True
        arguments['_tally'] = job['tally']
        arguments['_job'] = {'id': job['id'], 'batch': batch['name'],
                             'mode': job['mode'],
                             'createdAt': job['createdAt']}
        # The queue's job runs as a normal execution task: the
        # clients track it through the registry, a trusted one
        # cancels it (its owner is the daemon itself), and the run
        # metadata carries the task id
        task = server._tasks.create(
                ttl_ms=max(3600000, (policy.execute_timeout
                                     if policy.execute_timeout > 0
                                     else 43200) * 2000))
        task.owner_pid = os.getpid()
        job['taskId'] = task.task_id
        logger.notice('Task %s started: %s %s (the job queue, %s)',
                      task.task_id, job['mode'], ' '.join(job['targets']),
                      job['id'])
        context = ToolContext(
                task.task_id, task.cancel_event,
                client_capabilities={},
                client_info={'name': 'the job queue'}, trusted=True,
                task=task, server=server,
                # The queue is unattended: a confirmation gate
                # refuses the job instead of parking it on a
                # question nobody can answer
                unattended=True)
        with server._active_lock:
            server._active_requests['task:' + task.task_id] = \
                    task.cancel_event
        try:
            result = _execute_playbook(
                    project, policy, arguments, context,
                    check=(job['mode'] == 'check'),
                    enabled_check=(server.killswitch.is_enabled
                                   if server.killswitch is not None
                                   else None),
                    on_disabled=server.cancel_all)
        except Exception:
            with server._active_lock:
                server._active_requests.pop('task:' + task.task_id,
                                            None)
            task.fail({'code': -32603,
                       'message': 'Internal error: the execution '
                                  'raising'})
            raise
        with server._active_lock:
            server._active_requests.pop('task:' + task.task_id, None)
        if context.cancelled.is_set():
            task.mark_cancelled()
            logger.notice('Task %s cancelled: %s (the job queue)',
                          task.task_id, job['id'])
        else:
            task.complete(result)
            logger.notice('Task %s completed: %s (the job queue)',
                          task.task_id, job['id'])
        if result.get('isError'):
            raise JobQueueError(result['content'][0]['text'])
        structured = result.get('structuredContent') or {}
        job['executionId'] = structured.get('executionId')
        job['logPath'] = structured.get('log')
        job['startedAt'] = _utcnow()

    def _wait_job(self, job):
        """Wait for the job's run to finish; return 'done' or the
        failure text. The completion marker is the metadata's
        finishedAt; the lock freeing without it means the finalize
        never wrote."""
        while not self._stop_event.is_set():
            _id, log_path = runs.execution_log(self._server.project,
                                               job['executionId'])
            metadata = (runs.read_metadata(log_path)
                        if log_path is not None else None)
            if metadata is not None and 'finishedAt' in metadata:
                if metadata.get('exitCode') == 0:
                    return 'done'
                return ('the execution finished with exit code {}'
                        .format(metadata.get('exitCode')))
            if not project_busy(self._server.project,
                                self._server.policy):
                return 'no completion record (the finalize failed?)'
            self._stop_event.wait(self._interval)
        return 'cancelled: the daemon is stopping'

    def _fail_batch(self, batch, job, text):
        with self._lock:
            job['state'] = 'failed'
            job['error'] = text
            batch['state'] = 'failed'
            batch['failure'] = '{}: {}'.format(job['id'], text)
            self.last_failure = {'batch': batch['name'],
                                 'job': job['id'], 'error': text,
                                 'at': _utcnow()}
            if batch in self._batches:
                self._batches.remove(batch)
            for entry in batch['jobs']:
                if entry['state'] == 'queued':
                    entry['state'] = 'cancelled'
        logger.notice('The batch %s failed at %s: %s',
                      batch['name'], job['id'], text)

    # -- the descriptions ---------------------------------------------

    def _describe_batch(self, batch, detail=False):
        answer = {'name': batch['name'], 'state': batch['state'],
                  'pending': batch['pending'],
                  'createdAt': batch['createdAt'],
                  'jobs': len(batch['jobs']),
                  'estimateSeconds': sum(
                          (job['tally'] or {}).get(
                                  'estimateSeconds', 0)
                          for job in batch['jobs']
                          if job['state'] == 'queued')}
        if batch['failure'] is not None:
            answer['failure'] = batch['failure']
        if detail:
            answer['jobs'] = [self._describe_job(job, batch)
                              for job in batch['jobs']]
        return answer

    def _describe_job(self, job, batch):
        # The caller holds the lock
        pending = [entry for entry_batch in self._batches
                   for entry in entry_batch['jobs']
                   if entry['state'] == 'queued']
        position = None
        eta = None
        if job['state'] == 'queued':
            position = next(
                    (index + 1 for index, entry
                     in enumerate(pending) if entry is job), None)
            if position is not None:
                eta = sum((entry['tally'] or {}).get(
                        'estimateSeconds', 0)
                        for entry in pending[:position - 1])
        answer = dict(job)
        answer['position'] = position
        answer['etaSeconds'] = eta
        return answer

    def _describe_active(self):
        job = self._active_job
        if job is None:
            return None
        return {'id': job['id'], 'batch': job['batch'],
                'executionId': job['executionId'],
                'startedAt': job.get('startedAt')}

    # -- the lookups ----------------------------------------------------

    def _find_batch(self, name):
        for batch in self._batches:
            if batch['name'] == name:
                return batch
        raise JobQueueError('no such batch: {} (queue.list shows '
                            'them)'.format(name))

    def _find_job(self, job_id):
        for batch in self._batches:
            for index, job in enumerate(batch['jobs']):
                if job['id'] == job_id:
                    return batch, index, job
        raise JobQueueError('no such job: {} (batch.info shows them)'
                            .format(job_id))


class _TryLater(Exception):
    """The project is busy; the dispatch retries on the next tick."""
