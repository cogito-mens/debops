# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which execute Ansible playbooks ('check' and 'run').

Execution reuses the playbook runner for target resolution and command
construction, then runs the command without a shell, serialized against
other executions so that inventory (un)locking cannot race. Only a fixed
set of ansible-playbook options is accepted: free-form arguments would
be arbitrary code execution.

These tools run for a long time and against real infrastructure; they
run with a request context so that clients can cancel them.
"""

import collections
import contextlib
import fnmatch
import io
import itertools
import logging
import os
import re
import subprocess
import threading
import time

from ...ansible.inventory import AnsibleInventory
from ...ansibleplaybookrunner import AnsiblePlaybookRunner
from ...hooks import run_hooks
from ..policy import EXECUTE_OPTIONS
from ..server import RequestCancelled, _supports_form_elicitation
from .playbooks import project_playbook_dirs
from .common import (error_result, exclusive_project, mcp_environ,
                     text_result)

logger = logging.getLogger(__name__)

# Only the tail of a run matters on failure (the recap is at the end)
EXECUTE_OUTPUT_LIMIT = 128 * 1024

# Grace period between SIGTERM and SIGKILL for a cancelled or timed out
# ansible-playbook process
TERMINATION_GRACE = 5  # seconds

# Every playbook execution gets a daemon-unique number, so the log lines
# of a run can be correlated when clients overlap
_EXECUTION_IDS = itertools.count(1)


def _execution_label(execution_id, context):
    """The correlation suffix for the log lines of an execution."""
    parts = ['execution {}'.format(execution_id)]
    if context.request_id is not None:
        parts.append('request {}'.format(context.request_id))
    if context.peer_pid is not None:
        parts.append('client pid {}'.format(context.peer_pid))
    return ', '.join(parts)


def _local_playbooks(runner, project, policy):
    """Which of the runner's playbooks live in the project's playbook
    directories (not in a collection).

    A playbook from the project is agent-writable content: its tasks
    can execute commands on the managed hosts even in check mode
    ('check_mode: no' on a task), so the execution tools treat it
    separately.
    """
    local_dirs = [os.path.realpath(directory)
                  for _label, directory
                  in project_playbook_dirs(project, policy)]
    local = []
    for playbook in runner._found_playbooks:
        if playbook.startswith('"') and playbook.endswith('"'):
            playbook = playbook[1:-1]
        path = os.path.realpath(os.path.join(project.path, playbook))
        if any(os.path.commonpath((directory, path)) == directory
               for directory in local_dirs):
            local.append(playbook)
    return local


def _check_local_confirmation(local, context):
    """Ask the user before a check run of a project-local playbook.

    Returns None to proceed, or the result to send back (the
    elicitation request, or the refusal). In a task, the question is
    the task's outstanding input request, answered via 'tasks/update'.
    """
    message = ('Run the project playbook {} in check mode? Its tasks '
               'can execute commands on the managed hosts even in '
               'check mode (check_mode).'.format(
                   ', '.join('"{}"'.format(target) for target in local)))
    if context.task is not None:
        answer = context.task.ask('confirmation', {
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
                                'description': 'I understand and want '
                                'to proceed',
                                'default': False}},
                        'required': ['confirmed']}}})
        content = answer.get('content') or {}
        if (answer.get('action') == 'accept'
                and content.get('confirmed') is True):
            return None
        logger.notice('The check of a project playbook was declined')
        return error_result('the run was declined by the user')

    answers = context.input_responses or {}
    if 'confirmation' in answers:
        answer = answers.get('confirmation') or {}
        content = answer.get('content') or {}
        if (answer.get('action') == 'accept'
                and content.get('confirmed') is True):
            return None
        logger.notice('The check of a project playbook was declined')
        return error_result('the run was declined by the user')
    if not _supports_form_elicitation(context.client_capabilities):
        # A client which cannot ask may not block the work; the skip
        # is written to the audit log
        logger.notice('Check of a project playbook runs without user '
                      'confirmation: the client cannot elicit')
        return None
    return {'resultType': 'input_required',
            'inputRequests': {
                'confirmation': {
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
                                    'description': 'I understand and '
                                    'want to proceed',
                                    'default': False}},
                            'required': ['confirmed'],
                        },
                    },
                },
            }}


# Play and task headers in ansible-playbook output drive progress
# notifications; the play recap and host result lines do not
_PLAY_HEADER = re.compile(r'^PLAY \[.*\]')
_TASK_HEADER = re.compile(r'^TASK \[.*\]')

# Minimum interval between progress notifications
PROGRESS_INTERVAL = 0.5  # seconds

# ansible-playbook options the tools accept, mapped to their flags
ALLOWED_OPTIONS = {'limit': '--limit', 'tags': '--tags',
                   'skip_tags': '--skip-tags'}


def _playbook_targets(arguments):
    """The playbook targets of an execution, in specification order.

    Accepts a single name or a list of names; each may resolve to a
    playbook or expand to a playbook set downstream.
    """
    playbook = arguments.get('playbook')
    if isinstance(playbook, str):
        playbooks = [playbook]
    elif isinstance(playbook, (list, tuple)):
        playbooks = playbook
    else:
        playbooks = []
    targets = []
    for item in playbooks:
        name = str(item)
        if not name or '\n' in name or '\r' in name:
            raise ValueError('invalid playbook target: {!r}'.format(item))
        targets.append(name)
    if not targets:
        raise ValueError('the "playbook" argument is required')
    return targets


def _build_ansible_args(arguments, check, allowed_options=EXECUTE_OPTIONS):
    """Build the ansible-playbook arguments from the tool arguments.

    Only options permitted by the project policy are accepted.
    """
    args = _playbook_targets(arguments)
    for option, flag in ALLOWED_OPTIONS.items():
        value = arguments.get(option)
        if value is None:
            continue
        if option not in allowed_options:
            raise ValueError('the "{}" option is disabled by the project '
                             'policy'.format(option))
        value = str(value)
        if not value or '\n' in value or '\r' in value:
            raise ValueError('invalid value for "{}": {!r}'.format(
                    option, value))
        args.extend([flag, value])
    if check:
        # Mirror the 'debops check' section
        args.extend(['--diff', '--check'])
    return args


def resolve_hosts(env, cwd, command, timeout=30):
    """Resolve the host pattern of an ansible command to concrete hosts.

    Runs the command with '--list-hosts' (no execution) and returns the
    host names it would touch. Works for 'ansible <pattern>' and for
    'ansible-playbook <playbook> [--limit <pattern>]'.
    """
    result = subprocess.run(
            command + ['--list-hosts'], env=env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout)
    if result.returncode != 0:
        # Ansible reports the cause in [ERROR] lines; everything else
        # (warnings, deprecation noise) would drown it in a tail slice
        errors = [line for line in result.stderr.splitlines()
                  if line.startswith(('[ERROR]', 'ERROR'))]
        raise ValueError('\n'.join(errors) if errors
                         else (result.stderr.strip()[-500:]
                               or 'cannot resolve the host pattern'))
    # Hosts are listed one per line, indented under a 'hosts (N):' header
    hosts = []
    in_hosts = False
    for line in result.stdout.splitlines():
        if re.match(r'^\s*hosts \(\d+\):', line):
            in_hosts = True
            continue
        if in_hosts:
            if line.startswith('    '):
                hosts.append(line.strip())
            else:
                in_hosts = False
    # Multiple playbooks report their hosts in separate blocks; a host
    # targeted by several of them counts once
    unique = []
    for host in hosts:
        if host not in unique:
            unique.append(host)
    return unique


def run_cancellable(command, env, cwd, context, timeout, on_line=None,
                    enabled_check=None, on_disabled=None):
    """Run a command with cancellation, a timeout and capped output.

    Returns (output, returncode, status) where status is one of 'done',
    'cancelled', 'timeout' or 'disabled'. Only the last
    EXECUTE_OUTPUT_LIMIT bytes of combined output are kept. on_line is
    called with each output line as it arrives (progress reporting).
    """
    process = subprocess.Popen(
            command, env=env, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1)

    # A reader thread consumes the output as it comes; the buffer is
    # bounded so that long runs use constant memory
    output_lines = collections.deque(maxlen=4096)

    def read_output():
        for line in process.stdout:
            output_lines.append(line)
            if on_line is not None:
                on_line(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    disabled = False
    while process.poll() is None:
        if enabled_check is not None and not enabled_check():
            # The kill switch flipped mid-run
            disabled = True
            logger.warning('MCP functionality disabled mid-run; '
                           'terminating the execution')
            if on_disabled is not None:
                on_disabled()
            process.terminate()
            break
        if context.cancelled.is_set():
            cancelled = True
            process.terminate()
            break
        if time.monotonic() > deadline:
            timed_out = True
            process.kill()
            break
        time.sleep(0.2)
    try:
        process.wait(timeout=TERMINATION_GRACE)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    reader.join(timeout=TERMINATION_GRACE)
    output = ''.join(output_lines)
    if len(output) > EXECUTE_OUTPUT_LIMIT:
        output = ('… (earlier output truncated)\n'
                  + output[-EXECUTE_OUTPUT_LIMIT:])
    if disabled:
        status = 'disabled'
    elif cancelled:
        status = 'cancelled'
    elif timed_out:
        status = 'timeout'
    else:
        status = 'done'
    return output, process.returncode, status


def _execute_playbook(project, policy, arguments, context, check,
                      enabled_check=None, on_disabled=None):
    mode = 'check' if check else 'run'

    try:
        targets = _playbook_targets(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    with exclusive_project(project, policy,
                           note='{} {}'.format(mode, ' '.join(targets))):
        try:
            args = _build_ansible_args(arguments, check,
                                       policy.execute_options)
        except ValueError as errmsg:
            return error_result(str(errmsg))

        # The runner is command-line machinery: some of its error paths
        # print to stdout and call sys.exit(). Both would damage a server
        # (stdout is the protocol channel, and an exit kills the daemon),
        # so its construction is captured and its aborts become results
        runner_stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(runner_stdout):
                runner = AnsiblePlaybookRunner(project, ansible_args=args)
        except (ValueError, FileNotFoundError,
                ChildProcessError) as errmsg:
            return error_result(str(errmsg))
        except SystemExit as errmsg:
            details = runner_stdout.getvalue().strip()
            return error_result('the playbook runner aborted (code {}): {}'
                                .format(errmsg.code, details))
        command = runner.command()

        # A playbook from the project is agent-writable content: its
        # tasks can execute commands on the managed hosts even in
        # check mode. The policy may forbid local playbooks outright,
        # and a check run of one asks the user first
        local = _local_playbooks(runner, project, policy)
        if local and not policy.local_playbooks:
            return error_result(
                    'project-local playbooks cannot run in this '
                    'project (project.mcp.local_playbooks): '
                    + ', '.join(local))
        if local and check and 'check' in policy.confirm:
            gated = _check_local_confirmation(local, context)
            if gated is not None:
                return gated

        execution_id = next(_EXECUTION_IDS)
        logger.notice('Executing a playbook in %s mode: %s [%s]',
                      mode, ' '.join(command),
                      _execution_label(execution_id, context))
        if not run_hooks(project.path, 'pre-' + mode):
            logger.notice('Playbook execution %s aborted: the pre-%s '
                          'hook failed', execution_id, mode)
            return error_result('the pre-{} hook aborted the execution'
                                .format(mode))
        unlocked = False
        inventory = AnsibleInventory(project, name=project.view)
        try:
            unlocked = inventory.unlock()

            env = mcp_environ(project)

            # Resolve the affected hosts before running: the inventory
            # is the security boundary, and the resolution is the audit
            # trail and the policy enforcement point
            try:
                resolved_hosts = resolve_hosts(env, project.path, command)
            except (ValueError, subprocess.TimeoutExpired,
                    FileNotFoundError) as errmsg:
                logger.notice('Playbook execution %s aborted: %s',
                              execution_id, errmsg)
                return error_result('cannot resolve the affected hosts: '
                                    + str(errmsg))
            rejected = [host for host in resolved_hosts
                        if policy.execute_hosts
                        and not any(fnmatch.fnmatch(host, allowed)
                                    for allowed in policy.execute_hosts)]
            if rejected:
                logger.notice('Playbook execution %s aborted: hosts '
                              'rejected by the policy: %s',
                              execution_id, ', '.join(rejected))
                return error_result(
                        'not allowed by the project policy '
                        '(project.mcp.execute_hosts): '
                        + ', '.join(rejected))
            if resolved_hosts:
                shown = resolved_hosts[:8]
                logger.notice('The execution affects %s host(s): %s '
                              '[execution %s]',
                              len(resolved_hosts), ', '.join(shown) + (
                                  ', and {} more'.format(
                                          len(resolved_hosts) - 8)
                                  if len(resolved_hosts) > 8 else ''),
                              execution_id)
            else:
                logger.notice('The execution matches no hosts '
                              '[execution %s]', execution_id)

            # Play and task headers become progress notifications
            progress_counter = itertools.count(1)
            last_progress = [0.0]

            def on_line(line):
                if (_PLAY_HEADER.match(line)
                        or _TASK_HEADER.match(line)):
                    now = time.monotonic()
                    if now - last_progress[0] >= PROGRESS_INTERVAL:
                        last_progress[0] = now
                        context.progress(
                                line.rstrip(),
                                progress=next(progress_counter))

            started = time.monotonic()
            output, returncode, status = run_cancellable(
                    command, env=env, cwd=project.path, context=context,
                    timeout=policy.execute_timeout, on_line=on_line,
                    enabled_check=enabled_check, on_disabled=on_disabled)
            # The outcome completes the audit trail the start lines open
            ended = {
                'done': 'finished',
                'cancelled': 'cancelled by the client',
                'timeout': 'killed: it exceeded the timeout',
                'disabled': 'terminated: the kill switch was flipped',
            }[status]
            logger.notice('Playbook execution %s %s after %.0f s '
                          '(exit code %s)', execution_id, ended,
                          time.monotonic() - started, returncode)
        finally:
            if unlocked:
                inventory.lock()

    run_hooks(project.path, 'post-' + mode,
              extra_env={'DEBOPS_RETURN_CODE': returncode})

    header = '# debops {} {}\n\nexit code: {}\nhosts: {}\n\n'.format(
            mode, ' '.join(targets), returncode,
            ', '.join(resolved_hosts) if resolved_hosts else '(none)')
    if status == 'disabled':
        return error_result(header + 'terminated: the MCP functionality '
                            'was disabled mid-run (the kill switch)\n\n'
                            + output)
    if status == 'cancelled':
        raise RequestCancelled()
    if status == 'timeout':
        return error_result(header + 'timed out after {} seconds\n\n{}'
                            .format(policy.execute_timeout, output))
    if returncode != 0:
        return error_result(header + output)
    return text_result(header + output)


def tool_check(project, policy, arguments, context, enabled_check=None,
               on_disabled=None):
    return _execute_playbook(project, policy, arguments, context,
                             check=True, enabled_check=enabled_check,
                             on_disabled=on_disabled)


def tool_run(project, policy, arguments, context, enabled_check=None,
             on_disabled=None):
    return _execute_playbook(project, policy, arguments, context,
                             check=False, enabled_check=enabled_check,
                             on_disabled=on_disabled)
