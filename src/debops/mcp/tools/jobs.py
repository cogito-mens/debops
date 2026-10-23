# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The tools of the execution queue: the named job batches and
their jobs. The queue itself lives in debops.mcp.jobs; these
handlers are its wire shape. Registered only when the daemon runs
with the queue enabled ('mcp serve' by hand, or --queue)."""

import functools

from ..jobs import JobQueueError
from .common import error_result, text_result


def _queue(server):
    """The server's queue, or the refusal when it is not enabled."""
    queue = getattr(server, 'job_queue', None)
    if queue is None:
        raise JobQueueError('the execution queue is not enabled on '
                            'this daemon (mcp serve --queue)')
    return queue


def _queue_tool(func):
    """The queue's semantic refusals are the tool's error results."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except JobQueueError as errmsg:
            return error_result(str(errmsg))
    return wrapper


def _answer(text, structured):
    return text_result(text, structured=structured)


# -- the batches -------------------------------------------------

@_queue_tool
def tool_batch_create(server, arguments):
    name = str(arguments.get('name') or '')
    created = _queue(server).batch_create(name)
    return _answer('created: batch {} (position {})'.format(
            created['name'], created['position']),
            {'batch': created['name'],
             'position': created['position']})


@_queue_tool
def tool_batch_remove(server, arguments):
    name = str(arguments.get('name') or '')
    removed = _queue(server).batch_remove(name)
    return _answer('removed: batch {} ({} queued jobs dropped)'
                   .format(removed['removed'],
                           removed['jobsDropped']), removed)


@_queue_tool
def tool_batch_prune(server, arguments):
    name = str(arguments.get('name') or '')
    pruned = _queue(server).batch_prune(name)
    return _answer('removed: {}'.format(', '.join(pruned['removed'])),
                   pruned)


@_queue_tool
def tool_batch_pending(server, arguments, context):
    name = str(arguments.get('name') or '')
    pending = bool(arguments.get('pending', True))
    if not pending and not context.trusted:
        # Anyone may flag a batch for review; the review itself is
        # the operator's
        return error_result('clearing the review mark takes a '
                            'trusted client (the trust passphrase)')
    answer = _queue(server).batch_pending(name, pending)
    if pending:
        return _answer('pending review: {} (the queue waits on it; '
                       'a trusted client un-marks it)'.format(name),
                       answer)
    return _answer('accepted: {} (the review mark is off)'.format(
            name), answer)


@_queue_tool
def tool_batch_info(server, arguments):
    name = str(arguments.get('name') or '')
    info = _queue(server).batch_info(name)
    lines = ['Batch: {} ({}{})'.format(
            info['name'], info['state'],
            ', pending review' if info['pending'] else '')]
    lines.append('Jobs: {} (estimate {} s)'.format(
            info['jobs'], info['estimateSeconds']))
    for job in info['jobs']:
        lines.append('- {}: {} {} [{}]{}'.format(
                job['id'], job['mode'], ' '.join(job['targets']),
                job['state'],
                ' (estimate {} s)'.format(
                        job['tally']['estimateSeconds'])
                if job['tally'] else ''))
    return _answer('\n'.join(lines), {'batch': info})


# -- the queue ---------------------------------------------------

@_queue_tool
def tool_queue_list(server, arguments):
    answer = _queue(server).queue_list()
    lines = ['Queue: {}{}'.format(
            'dispatching' if answer['started'] else 'parked',
            ' ({} batches)'.format(len(answer['batches']))
            if answer['batches'] else ' (empty)')]
    active = answer['activeJob']
    if active is not None:
        lines.append('Running: {} in {} (execution {})'.format(
                active['id'], active['batch'], active['executionId']))
    for batch in answer['batches']:
        lines.append('- {}: {} jobs, ~{} s{}{}'.format(
                batch['name'], batch['jobs'],
                batch['estimateSeconds'],
                ', pending review' if batch['pending'] else '',
                ', ' + batch['state']
                if batch['state'] != 'queued' else ''))
    if answer['lastFailure'] is not None:
        failure = answer['lastFailure']
        lines.append('Last failure: {} in {}: {}'.format(
                failure['job'], failure['batch'], failure['error']))
    return _answer('\n'.join(lines), answer)


@_queue_tool
def tool_queue_clear(server, arguments):
    cleared = _queue(server).queue_clear()
    return _answer('cleared: {} batches'.format(cleared['cleared']),
                   cleared)


@_queue_tool
def tool_queue_start(server, arguments):
    answer = _queue(server).queue_start()
    if answer['already']:
        return _answer('the queue is already dispatching', answer)
    return _answer('the queue dispatches (batches in order)',
                   answer)


@_queue_tool
def tool_queue_stop(server, arguments):
    answer = _queue(server).queue_stop()
    if answer['already']:
        return _answer('the queue is already parked', answer)
    return _answer('the queue is parked (the running job finishes)',
                   answer)


# -- the jobs ------------------------------------------------------

@_queue_tool
def tool_job_add(server, arguments):
    batch_name = str(arguments.get('batch') or '')
    job = _queue(server).job_add(batch_name, arguments,
                                 server.project, server.policy)
    tally = job['tally']
    return _answer('queued: {} in {} (position {}, estimate {} s — '
                   '{} tasks x {} hosts)'.format(
                           job['id'], job['batch'], job['position'],
                           tally['estimateSeconds'],
                           tally['taskCount'], tally['hostCount']),
                   {'job': job})


@_queue_tool
def tool_job_prune(server, arguments):
    job_id = str(arguments.get('id') or '')
    pruned = _queue(server).job_prune(job_id)
    return _answer('removed: {}'.format(', '.join(pruned['removed'])),
                   pruned)


@_queue_tool
def tool_job_pop(server, arguments):
    batch_name = str(arguments.get('batch') or '')
    popped = _queue(server).job_pop(batch_name)
    return _answer('removed: {}'.format(popped['removed']), popped)


# -- the registration ---------------------------------------------

_NAME_PROPERTY = {'name': {'type': 'string',
                           'description': 'The job batch name'}}
_BATCH_PROPERTY = {'batch': {'type': 'string',
                             'description': 'The job batch (create '
                             'it first with batch.create)'}}

_NAME_SCHEMA = {'type': 'object', 'properties': dict(_NAME_PROPERTY),
                'required': ['name'], 'additionalProperties': False}
_BATCH_SCHEMA = {'type': 'object', 'properties': dict(_BATCH_PROPERTY),
                 'required': ['batch'], 'additionalProperties': False}
_EMPTY_SCHEMA = {'type': 'object', 'properties': {},
                 'additionalProperties': False}


def queue_tool_entries(server):
    """The queue tools' registration entries, closing over the
    server (the queue lives at server.job_queue)."""
    from . import execution_schema
    add_properties = dict(execution_schema(server.policy)[
            'properties'])
    add_properties['batch'] = _BATCH_PROPERTY['batch']
    add_properties['mode'] = {
            'type': 'string', 'enum': ['check', 'run', 'debug'],
            'description': "The execution mode (default: 'run')"}
    add_schema = {'type': 'object', 'properties': add_properties,
                  'required': ['batch', 'playbook'],
                  'additionalProperties': False}
    return [
        ('batch.create',
         'Create a job batch at the tail of the execution queue. '
         'Add its jobs with job.add; the queue dispatches them in '
         'order once started (queue.start).',
         dict(_NAME_SCHEMA),
         lambda arguments: tool_batch_create(server, arguments)),
        ('batch.remove',
         'Remove a job batch from the queue; its queued jobs die '
         'with it (a running job finishes).',
         dict(_NAME_SCHEMA),
         lambda arguments: tool_batch_remove(server, arguments)),
        ('batch.prune',
         'Remove a job batch and every batch queued after it.',
         dict(_NAME_SCHEMA),
         lambda arguments: tool_batch_prune(server, arguments)),
        ('batch.pending',
         'Mark a job batch as pending review — it and the batches '
         'after it wait for a human operator to accept (a trusted '
         'client) or remove it. With "pending": false the mark '
         'clears; clearing takes a trusted client.',
         {'type': 'object',
          'properties': dict(_NAME_PROPERTY,
                             pending={'type': 'boolean',
                                      'description': 'The review '
                                      'mark (default: true)'}),
          'required': ['name'], 'additionalProperties': False},
         lambda arguments, context: tool_batch_pending(
                 server, arguments, context),
         True),
        ('batch.info',
         'The details of a job batch: its jobs in order, with the '
         'states and the estimates.',
         dict(_NAME_SCHEMA),
         lambda arguments: tool_batch_info(server, arguments)),
        ('queue.list',
         'List the job batches of the execution queue, in the order '
         'they are planned to run, with the active job and the last '
         'failure.',
         dict(_EMPTY_SCHEMA),
         lambda arguments: tool_queue_list(server, arguments)),
        ('queue.clear',
         'Remove every queued batch from the execution queue (a '
         'running job finishes).',
         dict(_EMPTY_SCHEMA),
         lambda arguments: tool_queue_clear(server, arguments)),
        ('queue.start',
         'Start or resume the execution of the queued job batches.',
         dict(_EMPTY_SCHEMA),
         lambda arguments: tool_queue_start(server, arguments)),
        ('queue.stop',
         'Stop dispatching queued jobs; the one already running '
         'finishes.',
         dict(_EMPTY_SCHEMA),
         lambda arguments: tool_queue_stop(server, arguments)),
        ('job.add',
         'Add a job to a job batch of the execution queue (the '
         'batch must exist — create it with batch.create). The job '
         'is an ansible-playbook run of the given mode; the server '
         'computes the task and host tally and the estimate at '
         'queue time.',
         add_schema,
         lambda arguments: tool_job_add(server, arguments)),
        ('job.prune',
         'Remove a job from its batch, with every job after it.',
         {'type': 'object',
          'properties': {'id': {'type': 'string',
                                'description': 'The job id '
                                '(batch.info lists them)'}},
          'required': ['id'], 'additionalProperties': False},
         lambda arguments: tool_job_prune(server, arguments)),
        ('job.pop',
         'Remove the last job of a job batch.',
         dict(_BATCH_SCHEMA),
         lambda arguments: tool_job_pop(server, arguments)),
    ]
