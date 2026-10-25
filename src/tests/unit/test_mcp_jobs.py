# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the execution queue (debops.mcp.jobs + tools/jobs).

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import os
import threading
import unittest

from unittest import mock

from debops.mcp import jobs as jobs_module
from debops.mcp.jobs import JobQueue, JobQueueError
from debops.mcp.tools import jobs as job_tools


def make_server(with_queue=True):
    server = mock.MagicMock()
    server.legacy = False
    server.project.view = 'system'
    server.policy.execute_debug = True
    server.policy.execute_timeout = 3600
    server.killswitch = None
    server.job_queue = JobQueue(server, interval=0.01) \
        if with_queue else None
    return server


def queue_of(server):
    return server.job_queue


def add_job(server, batch, playbook='site', mode='run',
            tally=None, view='system'):
    with mock.patch.object(
            jobs_module, '_make_runner', return_value=mock.Mock()), \
            mock.patch.object(jobs_module, '_playbook_targets',
                              return_value=[playbook]), \
            mock.patch.object(
                    jobs_module, '_probe_tally',
                    return_value=(tally if tally is not None else
                                  {'taskCount': 2, 'hostCount': 1,
                                   'estimateSeconds': 9})):
        return queue_of(server).job_add(
                batch, {'playbook': playbook, 'mode': mode},
                server.project, server.policy)


class BatchMechanicsTestCase(unittest.TestCase):
    """The queue's batch and job bookkeeping, without a scheduler."""

    def setUp(self):
        self.server = make_server()
        self.queue = queue_of(self.server)

    def test_create_then_add_lands_in_order(self):
        self.queue.batch_create('first')
        self.queue.batch_create('second')
        one = add_job(self.server, 'first')
        two = add_job(self.server, 'first')
        three = add_job(self.server, 'second')
        self.assertEqual((one['position'], two['position'],
                          three['position']), (1, 2, 3))
        listing = self.queue.queue_list()
        self.assertEqual([batch['name'] for batch in
                          listing['batches']], ['first', 'second'])
        # The estimate of the first job is its own; the second's
        # carries the first's
        self.assertEqual(two['etaSeconds'],
                         one['tally']['estimateSeconds'])

    def test_add_into_a_missing_batch_refuses(self):
        with self.assertRaises(JobQueueError) as context:
            add_job(self.server, 'nope')
        self.assertIn('no such batch', str(context.exception))

    def test_a_duplicate_batch_name_refuses(self):
        self.queue.batch_create('first')
        with self.assertRaises(JobQueueError) as context:
            self.queue.batch_create('first')
        self.assertIn('exists already', str(context.exception))

    def test_a_bad_batch_name_refuses(self):
        with self.assertRaises(JobQueueError):
            self.queue.batch_create('no/slashes')

    def test_prune_cascades_within_the_batch_only(self):
        self.queue.batch_create('a')
        self.queue.batch_create('b')
        first = add_job(self.server, 'a')
        add_job(self.server, 'a')
        other = add_job(self.server, 'b')
        pruned = self.queue.job_prune(first['id'])
        self.assertEqual(len(pruned['removed']), 2)
        # The other batch is untouched
        self.assertEqual(self.queue.batch_info('b')['jobs'][0]['id'],
                         other['id'])

    def test_del_drops_the_tail(self):
        self.queue.batch_create('a')
        add_job(self.server, 'a')
        last = add_job(self.server, 'a', playbook='other')
        deleted = self.queue.job_del('a')
        self.assertEqual(deleted['removed'], last['id'])
        self.assertEqual(len(self.queue.batch_info('a')['jobs']), 1)
        # Deleting empties the batch; an empty one refuses
        self.queue.job_del('a')
        with self.assertRaises(JobQueueError):
            self.queue.job_del('a')

    def test_batch_prune_cascades_the_following(self):
        for name in ('a', 'b', 'c'):
            self.queue.batch_create(name)
        pruned = self.queue.batch_prune('b')
        self.assertEqual(pruned['removed'], ['b', 'c'])
        self.assertEqual([batch['name'] for batch in
                          self.queue.queue_list()['batches']], ['a'])

    def test_clear_drops_everything(self):
        self.queue.batch_create('a')
        self.queue.batch_create('b')
        cleared = self.queue.queue_clear()
        self.assertEqual(cleared['cleared'], 2)
        self.assertEqual(self.queue.queue_list()['batches'], [])


class QueueToolsTestCase(unittest.TestCase):
    """The tools' wire shape over the queue."""

    def setUp(self):
        self.server = make_server()
        self.queue = queue_of(self.server)
        self.queue.batch_create('work')
        add_job(self.server, 'work')

    def test_batch_info_lists_the_jobs(self):
        response = job_tools.tool_batch_info(self.server,
                                             {'name': 'work'})
        self.assertFalse(response['isError'])
        self.assertIn('job-1', response['content'][0]['text'])
        structured = response['structuredContent']['batch']
        self.assertEqual(structured['jobs'][0]['id'], 'job-1')

    def test_queue_list_shows_the_order(self):
        response = job_tools.tool_queue_list(self.server, {})
        self.assertIn('work', response['content'][0]['text'])
        self.assertEqual(response['structuredContent']['batches'][0]
                         ['name'], 'work')

    def test_pending_unmark_needs_the_trust(self):
        context = mock.Mock()
        context.trusted = False
        job_tools.tool_batch_pending(
                self.server, {'name': 'work', 'pending': True},
                context)
        response = job_tools.tool_batch_pending(
                self.server, {'name': 'work', 'pending': False},
                context)
        self.assertTrue(response['isError'])
        self.assertIn('trusted', response['content'][0]['text'])
        self.assertTrue(self.queue.batch_info('work')['pending'])
        context.trusted = True
        response = job_tools.tool_batch_pending(
                self.server, {'name': 'work', 'pending': False},
                context)
        self.assertFalse(response['isError'])
        self.assertFalse(self.queue.batch_info('work')['pending'])

    def test_a_missing_queue_refuses(self):
        server = make_server(with_queue=False)
        response = job_tools.tool_queue_list(server, {})
        self.assertTrue(response['isError'])
        self.assertIn('not enabled', response['content'][0]['text'])

    def test_job_add_answers_with_the_tally(self):
        with mock.patch.object(
                jobs_module, '_make_runner',
                return_value=mock.Mock()), \
                mock.patch.object(jobs_module, '_playbook_targets',
                                  return_value=['site']), \
                mock.patch.object(jobs_module, '_probe_tally',
                                  return_value={'taskCount': 2,
                                                'hostCount': 1,
                                                'estimateSeconds': 9}):
            response = job_tools.tool_job_add(self.server, {
                    'batch': 'work', 'playbook': 'site',
                    'mode': 'run'})
        self.assertFalse(response['isError'])
        text = response['content'][0]['text']
        self.assertIn('queued: job-2 in work', text)
        self.assertIn('estimate 9 s', text)
        self.assertIn('2 tasks x 1 hosts', text)
        job = response['structuredContent']['job']
        self.assertEqual(job['view'], 'system')
        self.assertEqual(job['state'], 'queued')


class SchedulerTestCase(unittest.TestCase):
    """The dispatch: started/stopped, the pending gate, the busy
    yield, the batch failure cascade."""

    def setUp(self):
        self.server = make_server()
        self.queue = queue_of(self.server)
        self.started_jobs = []
        self.fail_next = None
        self.busy = False

        def fake_execute(project, policy, arguments, context,
                         check=None, enabled_check=None,
                         on_disabled=None):
            job = arguments.get('_job') or {}
            if self.fail_next is not None \
                    and job.get('id') == self.fail_next:
                return {'isError': True,
                        'content': [{'type': 'text',
                                     'text': 'the playbook refused'}]}
            self.started_jobs.append(job.get('id'))
            return {'isError': False,
                    'content': [{'type': 'text', 'text': 'started'}],
                    'structuredContent': {'executionId': 1}}

        patcher = mock.patch.object(jobs_module, '_execute_playbook',
                                    fake_execute)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.queue._wait_job = lambda job: 'done'
        busy_patcher = mock.patch.object(jobs_module, 'project_busy',
                                         lambda project, policy:
                                         self.busy)
        busy_patcher.start()
        self.addCleanup(busy_patcher.stop)

    def tick(self, times=1):
        for _ in range(times):
            self.queue._dispatch_once()

    def test_a_job_run_is_a_real_cancellable_task(self):
        # The queue's job registers in the server's task registry:
        # the clients track it, a trusted one cancels it; the daemon
        # itself owns it
        from debops.mcp.tasks import TaskRegistry
        self.server._tasks = TaskRegistry()
        self.server._active_lock = threading.Lock()
        self.server._active_requests = {}
        started = threading.Event()

        def blocking_execute(project, policy, arguments, context,
                             check=None, enabled_check=None,
                             on_disabled=None):
            started.set()
            context.cancelled.wait(10)
            return {'isError': True,
                    'content': [{'type': 'text',
                                 'text': 'cancelled by the client'}]}

        with mock.patch.object(jobs_module, '_execute_playbook',
                               blocking_execute):
            self.queue.batch_create('work')
            add_job(self.server, 'work')
            self.queue.queue_start()
            runner = threading.Thread(target=self.tick)
            runner.start()
            self.assertTrue(started.wait(5))
            job = self.queue._find_batch('work')['jobs'][0]
            task_id = job['taskId']
            task = self.server._tasks.get(task_id)
            self.assertIsNotNone(task)
            self.assertEqual(task.owner_pid, os.getpid())
            self.assertEqual(task.status, 'working')
            self.assertTrue(self.server._tasks.cancel(task_id))
            runner.join(timeout=5)
            self.assertFalse(runner.is_alive())
            self.assertEqual(task.status, 'cancelled')
            # The queue unwinds the cancelled run as a failed job
            self.assertEqual(job['state'], 'failed')

    def test_nothing_dispatches_while_parked(self):
        self.queue.batch_create('work')
        add_job(self.server, 'work')
        self.tick()
        self.assertEqual(self.started_jobs, [])

    def test_the_queue_dispatches_in_order(self):
        self.queue.batch_create('work')
        add_job(self.server, 'work')
        add_job(self.server, 'work', playbook='other')
        self.queue.queue_start()
        self.tick(3)
        self.assertEqual(self.started_jobs, ['job-1', 'job-2'])
        # The batch completed and left the queue
        self.assertEqual(self.queue.queue_list()['batches'], [])

    def test_a_pending_batch_gates_the_following(self):
        self.queue.batch_create('gated')
        self.queue.batch_create('after')
        add_job(self.server, 'gated')
        add_job(self.server, 'after')
        self.queue.batch_pending('gated', True)
        self.queue.queue_start()
        self.tick(3)
        self.assertEqual(self.started_jobs, [])
        self.queue.batch_pending('gated', False)
        self.tick(4)
        self.assertEqual(self.started_jobs, ['job-1', 'job-2'])

    def test_a_busy_project_parks_the_dispatch(self):
        self.queue.batch_create('work')
        add_job(self.server, 'work')
        self.queue.queue_start()
        self.busy = True
        self.tick()
        self.assertEqual(self.started_jobs, [])
        # The job is back at the head, the batch queued again
        info = self.queue.batch_info('work')
        self.assertEqual(info['state'], 'queued')
        self.assertEqual(info['jobs'][0]['state'], 'queued')
        self.busy = False
        self.tick()
        self.assertEqual(self.started_jobs, ['job-1'])

    def test_a_failing_job_fails_its_batch_and_the_queue_moves_on(self):
        self.queue.batch_create('work')
        self.queue.batch_create('after')
        add_job(self.server, 'work')
        add_job(self.server, 'work', playbook='other')
        add_job(self.server, 'after')
        self.fail_next = 'job-1'
        self.queue.queue_start()
        self.tick(4)
        # The batch is gone, its pending jobs cancelled, the failure
        # recorded; the next batch ran
        self.assertEqual(self.queue.queue_list()['batches'], [])
        self.assertEqual(self.queue.last_failure['batch'], 'work')
        self.assertEqual(self.started_jobs, ['job-3'])

    def test_an_empty_head_batch_completes(self):
        self.queue.batch_create('empty')
        self.queue.queue_start()
        self.tick()
        self.assertEqual(self.queue.queue_list()['batches'], [])

    def test_stop_and_start(self):
        self.queue.batch_create('work')
        add_job(self.server, 'work')
        self.queue.queue_start()
        self.queue.queue_stop()
        self.tick()
        self.assertEqual(self.started_jobs, [])
        self.queue.queue_start()
        self.tick()
        self.assertEqual(self.started_jobs, ['job-1'])


class RegistrationTestCase(unittest.TestCase):
    """The queue tools register only when the daemon has a queue."""

    def test_the_twelve_tools(self):
        server = make_server()
        entries = job_tools.queue_tool_entries(server)
        names = [entry[0] for entry in entries]
        self.assertEqual(sorted(names), [
                'batch.create', 'batch.info', 'batch.pending',
                'batch.prune', 'batch.remove', 'job.add', 'job.del',
                'job.prune', 'queue.clear', 'queue.list',
                'queue.start', 'queue.stop'])

    def test_the_tools_are_view_neutral(self):
        # The queue steers no view-anchored path; the view guard
        # must not refuse it
        from debops.mcp.tools import TOOL_ANNOTATIONS
        for name in ('job.add', 'batch.create', 'queue.start'):
            self.assertTrue(TOOL_ANNOTATIONS[name][
                    'org.debops/viewNeutral'])


if __name__ == '__main__':
    unittest.main()
