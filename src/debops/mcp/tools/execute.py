# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which execute Ansible playbooks ('check' and 'run').

Execution reuses the playbook runner for target resolution and command
construction; the command runs inside a fixed shell wrapper, every part
shell-quoted, serialized against other executions so that inventory
(un)locking cannot race. Only a fixed set of ansible-playbook options
is accepted: free-form arguments would be arbitrary code execution.

These tools run for a long time and against real infrastructure; they
run with a request context so that clients can cancel them.

A run outlives its server: every execution is detached from the server
process, keeps the project lock, and writes its output to a run log in
the cache directory. A harness closing the stdio server's process
group, or a daemon stop, does not take the run down; the log file is
the record of the run when the server is gone.
"""

import collections
import contextlib
import fnmatch
import io
import itertools
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from datetime import datetime, timezone

from ...ansible.inventory import AnsibleInventory
from ...ansibleplaybookrunner import AnsiblePlaybookRunner
from ...hooks import run_hooks
from .. import runs
from ..policy import EXECUTE_OPTIONS
from ..server import RequestCancelled

from .common import (confirmation_flow, current_lock_fd, error_result,
                     exclusive_project, mcp_environ, text_result)

logger = logging.getLogger(__name__)

# Only the tail of a run matters on failure (the recap is at the end)
EXECUTE_OUTPUT_LIMIT = 128 * 1024

# Grace period between SIGTERM and SIGKILL for a cancelled or timed out
# ansible-playbook process
TERMINATION_GRACE = 5  # seconds

# Every playbook execution gets a unique number; the log directory is
# the source of truth, this counter is the fallback when the cache is
# not usable
_EXECUTION_IDS = itertools.count(1)


def _utcnow():
    """The current time, as an ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _execution_label(execution_id, context):
    """The correlation suffix for the log lines of an execution."""
    parts = ['execution {}'.format(execution_id)]
    if context.request_id is not None:
        parts.append('request {}'.format(context.request_id))
    if context.peer_pid is not None:
        parts.append('client pid {}'.format(context.peer_pid))
    return ', '.join(parts)


def _local_playbooks(runner, project):
    """Which of the runner's playbooks are project content.

    A playbook which resolves under the project — anywhere, not just
    the playbook directories — is agent-writable content: its tasks can
    execute commands on the managed hosts even in check mode
    ('check_mode: no' on a task), so the execution tools treat it
    separately. The exception is the installed collections: that
    content arrives through the confirm-gated collection tools and is
    the trusted kind.
    """
    project_root = os.path.realpath(project.path)
    collections_root = os.path.realpath(os.path.join(
            project.path, 'ansible', 'collections'))
    local = []
    for playbook in runner._found_playbooks:
        if playbook.startswith('"') and playbook.endswith('"'):
            playbook = playbook[1:-1]
        path = os.path.realpath(os.path.join(project.path, playbook))
        if os.path.commonpath((project_root, path)) != project_root:
            continue  # a collection outside the project
        if os.path.commonpath((collections_root, path)) == \
                collections_root:
            continue  # an installed collection's content
        local.append(playbook)
    return local


def _check_local_confirmation(local, context, policy):
    """Ask the user before a check run of a project-local playbook.

    Returns None to proceed, or the result to send back (the
    elicitation request, or the refusal).
    """
    verdict = confirmation_flow(
            context,
            'Run the project playbook {} in check mode? Its tasks can '
            'execute commands on the managed hosts even in check mode '
            '(check_mode).'.format(
                ', '.join('"{}"'.format(target) for target in local)),
            required=(policy is not None and policy.confirm_required))
    if verdict is True:
        return None
    if verdict is False:
        logger.notice('The check of a project playbook was declined')
        return error_result('the run was declined by the user')
    return verdict


# Play and task headers in ansible-playbook output drive progress
# notifications; the play recap and host result lines do not
_PLAY_HEADER = re.compile(r'^PLAY \[.*\]')
_TASK_HEADER = re.compile(r'^TASK \[.*\]')

# The recap line of a play: 'host : ok=12 changed=0 unreachable=0 ...'.
# One recap section prints per play; the counters aggregate per host
_RECAP_FIELDS = ('ok', 'changed', 'unreachable', 'failed', 'skipped',
                 'rescued', 'ignored')
_RECAP_LINE = re.compile(
        r'^(\S+)\s+: '
        + r'\s+'.join(r'{}=(\d+)'.format(field) for field in _RECAP_FIELDS)
        + r'\s*$')


def _failure_excerpt(output):
    """The first failure with its task header, for a failed answer.

    The full output stays in the text (or in the run log); this is the
    'start here' section of a failed run.
    """
    fatal = re.search(r'^fatal: \[', output, re.M)
    if fatal is None:
        return None
    header = None
    # (the module-level _TASK_HEADER matches per line, no re.M — the
    # progress lines; here the slice is the whole run so far)
    for match in re.finditer(r'^TASK \[.*\]', output[:fatal.start()],
                             re.M):
        header = match
    lines = []
    if header is not None:
        lines.append(header.group(0))
    block = output[fatal.start():].split('\n\n', 1)[0]
    lines.extend(block.splitlines()[:30])
    return '\n'.join(lines)


# The answer shows the end of the run: this many lines of context
# before the final recap, and the recap complete — however many hosts
# it lists — unless it is absurd
ANSWER_CONTEXT_LINES = 100
ANSWER_RECAP_LINES = 2000

# The recap section header: 'PLAY RECAP *****...'
_RECAP_HEADER = re.compile(r'^PLAY RECAP \*+\s*$')


def _answer_output(output, execution_id):
    """The answer's view of the run output.

    A blind byte tail loses one of the two things the answer is for:
    the failure context (above the recap) or the per-host recap itself
    (with a hundred hosts the table alone is a hundred lines). So the
    answer is structural: the last stretch of the play, then the
    complete final recap; the middle is elided with a pointer at the
    query tool.
    """
    lines = output.splitlines()
    recap_start = None
    for index in range(len(lines) - 1, -1, -1):
        if _RECAP_HEADER.match(lines[index]):
            recap_start = index
            break
    if recap_start is None:
        # A run which never reached a recap (a crash, a parse error):
        # the plain tail, with the same elision marker
        if len(lines) <= ANSWER_CONTEXT_LINES:
            return output
        return ('… ({} lines elided; inspect: execution.output id={})\n'
                .format(len(lines) - ANSWER_CONTEXT_LINES, execution_id)
                + '\n'.join(lines[-ANSWER_CONTEXT_LINES:]) + '\n')
    context = lines[:recap_start]
    recap = lines[recap_start:]
    out = []
    if len(context) > ANSWER_CONTEXT_LINES:
        out.append('… ({} lines elided; inspect: execution.output '
                   'id={})'.format(len(context) - ANSWER_CONTEXT_LINES,
                                   execution_id))
        out.extend(context[-ANSWER_CONTEXT_LINES:])
    else:
        out.extend(context)
    if len(recap) > ANSWER_RECAP_LINES:
        # Thousands of hosts: the table is cut, the query tool has it
        half = ANSWER_RECAP_LINES // 2
        out.extend(recap[:half])
        out.append('… ({} more recap lines; inspect: execution.output '
                   'id={} kind=recap)'
                   .format(len(recap) - ANSWER_RECAP_LINES,
                           execution_id))
        out.extend(recap[-half:])
    else:
        out.extend(recap)
    return '\n'.join(out) + ('\n' if out else '')


def _parse_recap(output):
    """The per-host counters of the play recap(s), summed per host."""
    recap = {}
    for line in output.splitlines():
        match = _RECAP_LINE.match(line.strip())
        if match is None:
            continue
        counters = recap.setdefault(
                match.group(1), dict.fromkeys(_RECAP_FIELDS, 0))
        for field, value in zip(_RECAP_FIELDS, match.groups()[1:]):
            counters[field] += int(value)
    return {host: recap[host] for host in sorted(recap)}


# Minimum interval between progress notifications
PROGRESS_INTERVAL = 0.5  # seconds

# Minimum interval between the task status refreshes; the status
# carries the tail of the run log, which a polling client reads
STATUS_INTERVAL = 2  # seconds

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
        # A target starting with a dash would be an option for
        # ansible-playbook downstream, with the next target as its
        # value; and the runner resolves an absolute path or a
        # traversal directly, out of the playbook search space
        if (not name or '\n' in name or '\r' in name
                or name.startswith('-')):
            raise ValueError('invalid playbook target: {!r}'.format(item))
        if os.path.isabs(name) or '..' in name.split('/'):
            raise ValueError('a playbook target names a playbook in the '
                             'project or a collection, not a path: {!r}'
                             .format(item))
        targets.append(name)
    if not targets:
        raise ValueError('the "playbook" argument is required')
    return targets


def _build_ansible_args(arguments, check, allowed_options=EXECUTE_OPTIONS,
                        diff=False, debug=False):
    """Build the ansible-playbook arguments from the tool arguments.

    Only options permitted by the project policy are accepted. The diff
    rides only when the project opts in (project.mcp.execute_diff): it
    renders the files a template task would write, secrets included,
    into the tool result. The debugging verbosity (-vv, gated by
    project.mcp.execute_debug upstream) is the same class of opt-in.
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
        # Mirror the 'debops check' section; the diff is gated on the
        # project policy, since it can carry rendered secrets
        if diff:
            args.append('--diff')
        args.append('--check')
    if debug:
        # -vv pairs the invocation with the result per task, without
        # the connection chatter of -vvv
        args.append('-vv')
    return args


def _shell_command(command):
    """A command as a shell line, with every part quoted."""
    return ' '.join(shlex.quote(str(part)) for part in command)


def _finalize_command(project):
    """The command which finalizes the project after a detached run.

    The server locks the secrets and commits the pending changes when
    the execution returns; a run which outlived its server needs the
    same, and the 'project lock' command does both.
    """
    command = [sys.executable, '-m', 'debops', 'project', 'lock']
    if project.view:
        command.extend(['--view', project.view])
    command.append(project.path)
    return command


def _detached_command(command, timeout, finalize=None):
    """The command of a detached execution.

    The execution leads its own process group, so the death of the
    server (or the harness which spawned it) does not reach it. Two
    wrappers ride along:

    - 'timeout' bounds the run even when no server is left to enforce
      the timeout,
    - the finalize command commits the project and locks the secrets
      again, which the server would have done when it returned.
    """
    if shutil.which('timeout') is None:
        logger.warning('The "timeout" command is not available; a '
                       'detached execution is not bounded by the server')
        script = _shell_command(command)
    else:
        # coreutils: a duration of 0 disables the limit
        script = 'timeout -k {} {} {}'.format(
                TERMINATION_GRACE, int(timeout), _shell_command(command))
    lines = [script]
    if finalize is not None:
        lines.append('rc=$?')
        lines.append('{} || echo "debops-mcp: the finalize failed"'
                     .format(_shell_command(finalize)))
        lines.append('echo "debops-mcp: the execution finished with '
                     'exit code $rc"')
        lines.append('exit $rc')
    else:
        lines.append('exit $?')
    return ['/bin/sh', '-c', '\n'.join(lines)]


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
                    enabled_check=None, on_disabled=None, input_data=None,
                    log_path=None, pass_fds=None, detached=False):
    """Run a command with cancellation, a timeout and capped output.

    Returns (output, returncode, status) where status is one of 'done',
    'cancelled', 'timeout' or 'disabled'. The output kept in memory is
    bounded to the last 4096 lines; the returned text is further
    truncated to the last EXECUTE_OUTPUT_LIMIT bytes. on_line is called
    with each output line as it arrives (progress reporting); its
    failures are logged and never break the run. 'input_data' is
    written to the command's standard input.

    With 'log_path' the output is written to that file as well; the
    file outlives the server, so a detached execution keeps its record.
    A detached execution writes to the file directly (a pipe to a dead
    server would kill the run on the next write) and the progress
    lines are read back from the file. 'pass_fds' are inherited by the
    child, so that the project lock outlives the server with the run.
    """
    output_lines = collections.deque(maxlen=4096)
    log_file = None
    stdout = subprocess.PIPE
    stdin = subprocess.PIPE if input_data is not None else None
    if log_path is not None:
        # The log can carry secrets (a diff or a debug run renders
        # them); create it private, and force the mode of a reused file
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        os.fchmod(fd, 0o600)
        log_file = os.fdopen(fd, 'w', encoding='utf-8')
        if detached:
            # The child writes to the log file itself; the file is the
            # only channel which survives the server
            stdout = log_file
    if detached and input_data is None:
        # The child outlives the server; it must not inherit the
        # server's stdin, which is the protocol channel
        stdin = subprocess.DEVNULL
    try:
        process = subprocess.Popen(
                command, env=env, cwd=cwd,
                stdin=stdin,
                stdout=stdout, stderr=subprocess.STDOUT,
                universal_newlines=True, bufsize=1,
                pass_fds=tuple(pass_fds or ()),
                start_new_session=detached)
    except Exception:
        if log_file is not None:
            log_file.close()
            log_file = None
        raise
    finally:
        if log_file is not None and stdout is log_file:
            # The child has its own copy of the descriptor
            log_file.close()
            log_file = None

    def report(line):
        """Feed a line to the progress callback.

        The callback reports progress; a failure in it must not kill
        the reader thread, which the run depends on (the pipe is
        drained only while the reader lives).
        """
        if on_line is None:
            return
        try:
            on_line(line)
        except Exception:
            logger.debug('The progress callback failed', exc_info=True)

    if detached and log_path is not None:
        def read_output():
            """Follow the log file the child writes to."""
            pending = ''
            with open(log_path, 'r', encoding='utf-8',
                      errors='replace') as fp:
                while True:
                    chunk = fp.read(65536)
                    if not chunk:
                        if process.poll() is None:
                            # The child is still running; wait for more
                            time.sleep(0.1)
                            continue
                        # The child is gone; one more read catches what
                        # it wrote before it exited
                        chunk = fp.read(65536)
                        if not chunk:
                            break
                    pending += chunk
                    lines = pending.split('\n')
                    pending = lines.pop()
                    for line in lines:
                        report(line)
            if pending:
                report(pending)
    else:
        def read_output():
            for line in process.stdout:
                output_lines.append(line)
                if log_file is not None:
                    try:
                        log_file.write(line)
                        log_file.flush()
                    except ValueError:
                        # Closed at the teardown with the reader still
                        # alive (an orphaned grandchild holds the pipe)
                        pass
                report(line)

    def signal_process(sig):
        """Signal the execution, or its whole group when it is
        detached.

        A detached execution leads its own process group, so the signal
        has to reach the group: the wrapper and the playbook below it
        must go together.
        """
        if detached:
            try:
                os.killpg(process.pid, sig)
            except OSError:
                pass
        elif sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()

    deadline = time.monotonic() + timeout
    cancelled = False
    timed_out = False
    disabled = False
    reader = threading.Thread(target=read_output, daemon=True)
    try:
        reader.start()

        if input_data is not None:
            try:
                process.stdin.write(input_data)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                # The command exited without reading its input
                pass

        while process.poll() is None:
            if enabled_check is not None and not enabled_check():
                # The kill switch flipped mid-run
                disabled = True
                logger.warning('MCP functionality disabled mid-run; '
                               'terminating the execution')
                if on_disabled is not None:
                    on_disabled()
                signal_process(signal.SIGTERM)
                break
            if context.cancelled.is_set():
                cancelled = True
                signal_process(signal.SIGTERM)
                break
            # A timeout of 0 means no limit: the run ends when it ends,
            # or when the client cancels it
            if timeout > 0 and time.monotonic() > deadline:
                timed_out = True
                signal_process(signal.SIGKILL)
                break
            time.sleep(0.2)
        try:
            process.wait(timeout=TERMINATION_GRACE)
        except subprocess.TimeoutExpired:
            signal_process(signal.SIGKILL)
            process.wait()
        reader.join(timeout=TERMINATION_GRACE)
    finally:
        if log_file is not None:
            log_file.close()
    if detached and log_path is not None:
        # The log file is the record of the run; the pipe never carried
        # it
        output = runs.read_tail(log_path, EXECUTE_OUTPUT_LIMIT)
        try:
            truncated = os.path.getsize(log_path) > EXECUTE_OUTPUT_LIMIT
        except OSError:
            truncated = False
        if truncated:
            output = ('… (earlier output truncated)\n' + output)
    else:
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


def _task_status(header, tail):
    """The status message of a running execution: the current task and
    the tail of the run log.

    The task status is what a polling client sees; the tail tells it
    what the run has printed so far, which the progress notifications
    do not carry.
    """
    parts = []
    if header:
        parts.append(header)
    text = runs.status_tail(tail)
    if text:
        parts.append('--- log tail ---')
        parts.append(text)
    return '\n'.join(parts)


def _execute_playbook(project, policy, arguments, context, check,
                      enabled_check=None, on_disabled=None):
    mode = 'check' if check else 'run'

    try:
        targets = _playbook_targets(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    debug = bool(arguments.get('debug'))
    if debug and not policy.execute_debug:
        return error_result('debugging is disabled by the project '
                            'policy (project.mcp.execute_debug): a '
                            'debug run prints task arguments — secrets '
                            'included — into the answer')

    with exclusive_project(project, policy,
                           note='{} {}'.format(mode, ' '.join(targets))):
        try:
            args = _build_ansible_args(arguments, check,
                                       policy.execute_options,
                                       diff=policy.execute_diff,
                                       debug=debug)
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
        local = _local_playbooks(runner, project)
        if local and not policy.local_playbooks:
            return error_result(
                    'project-local playbooks cannot run in this '
                    'project (project.mcp.local_playbooks): '
                    + ', '.join(local))
        if local and check and 'check' in policy.confirm:
            gated = _check_local_confirmation(local, context, policy)
            if gated is not None:
                return gated

        # The run log outlives the server: it is the record of the run
        # when the client is gone before the answer arrives. The id
        # comes from the log directory, so a restarted server does not
        # reuse the id of a run whose log still exists
        execution_id, log_path = runs.new_log(
                project, keep=policy.run_logs_keep,
                max_age=policy.run_logs_max_age)
        if execution_id is None:
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
            # The child writes to a file, where stdout would be block
            # buffered; the run log is tailed live, so it must not be
            env['PYTHONUNBUFFERED'] = '1'
            if debug:
                env['ANSIBLE_DEBUG'] = 'true'
                # The module scripts stay on the host for manual
                # re-runs ('expel' them with the next normal run)
                env['ANSIBLE_KEEP_REMOTE_FILES'] = '1'

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
            # The tail of the run log; a polling client sees it in the
            # task status, where the progress notifications do not reach
            tail = collections.deque(maxlen=runs.TAIL_LINES)
            last_status = [0.0]
            progress_line = [None]

            def on_line(line):
                tail.append(line)
                now = time.monotonic()
                if (_PLAY_HEADER.match(line)
                        or _TASK_HEADER.match(line)):
                    if now - last_progress[0] >= PROGRESS_INTERVAL:
                        last_progress[0] = now
                        progress_line[0] = line.rstrip()
                        context.progress(
                                line.rstrip(),
                                progress=next(progress_counter))
                if (context.task is not None
                        and now - last_status[0] >= STATUS_INTERVAL):
                    last_status[0] = now
                    context.task.set_status_message(
                            _task_status(progress_line[0], tail))

            started = time.monotonic()
            # The sidecar lets a later session list and address the
            # runs without parsing the logs
            metadata = {'executionId': execution_id, 'mode': mode,
                        'playbooks': targets, 'hosts': resolved_hosts,
                        'startedAt': _utcnow()}
            if log_path is not None:
                metadata['log'] = os.path.basename(log_path)
                try:
                    runs.write_metadata(log_path, metadata)
                except OSError as errmsg:
                    logger.warning('Cannot write the run metadata for '
                                   'execution %s: %s', execution_id,
                                   errmsg)
            # A detached execution keeps the project lock: the lock
            # belongs to the run, not to the server which started it
            lock_fd = current_lock_fd()
            pass_fds = (lock_fd,) if lock_fd is not None else ()
            # The wrapper carries the bounds and the finalization of
            # the run; the playbook command itself stays the same
            command = _detached_command(
                    command, policy.execute_timeout,
                    _finalize_command(project))
            output, returncode, status = run_cancellable(
                    command, env=env, cwd=project.path, context=context,
                    timeout=policy.execute_timeout, on_line=on_line,
                    enabled_check=enabled_check, on_disabled=on_disabled,
                    log_path=log_path, pass_fds=pass_fds, detached=True)
            # The outcome completes the audit trail the start lines open
            ended = {
                'done': 'finished',
                'cancelled': 'cancelled by the client',
                'timeout': 'killed: it exceeded the timeout',
                'disabled': 'terminated: the kill switch was flipped',
            }[status]
            logger.notice('Playbook execution %s %s after %.0f s '
                          '(exit code %s, log %s)', execution_id, ended,
                          time.monotonic() - started, returncode,
                          log_path or '-')
            if log_path is not None:
                metadata['exitCode'] = returncode
                metadata['status'] = status
                metadata['finishedAt'] = _utcnow()
                try:
                    runs.write_metadata(log_path, metadata)
                except OSError as errmsg:
                    logger.warning('Cannot update the run metadata for '
                                   'execution %s: %s', execution_id,
                                   errmsg)
        finally:
            if unlocked:
                inventory.lock()

    run_hooks(project.path, 'post-' + mode,
              extra_env={'DEBOPS_RETURN_CODE': returncode})

    header = '# debops {} {}\n\nexecution: {}\nexit code: {}\nhosts: {}\n'.format(
            mode, ' '.join(targets), execution_id, returncode,
            ', '.join(resolved_hosts) if resolved_hosts else '(none)')
    if log_path is not None:
        # The log outlives the server; it is where the run is read
        header += 'log: {}\n'.format(log_path)
    header += '\n'
    notes = []
    if debug:
        notes.append('debug mode (-vv, ANSIBLE_DEBUG, the remote module '
                     'scripts were kept on the hosts)')
    if returncode != 0:
        # The first failure with its neighborhood, no debug flag
        # needed; the full output is in the run log
        excerpt = _failure_excerpt(output)
        if excerpt is not None:
            notes.append('## First failure\n\n' + excerpt)
    if notes:
        header += '\n\n'.join(notes) + '\n\n'
    # The machine-readable summary; the full output stays in the run
    # log, the text carries the end of the run (see _answer_output).
    # The recap answers the maintainer's question — 'exit code 0' means
    # no failures, only the recap says whether anything changed
    summary = {'mode': mode, 'playbooks': targets,
               'hosts': resolved_hosts, 'exitCode': returncode,
               'status': status, 'executionId': execution_id}
    if log_path is not None:
        summary['log'] = log_path
    recap = _parse_recap(output)
    if recap:
        summary['recap'] = recap
    body = _answer_output(output, execution_id)
    if status == 'disabled':
        return error_result(header + 'terminated: the MCP functionality '
                            'was disabled mid-run (the kill switch)\n\n'
                            + body, structured=summary)
    if status == 'cancelled':
        raise RequestCancelled()
    if status == 'timeout':
        return error_result(header + 'timed out after {} seconds\n\n{}'
                            .format(policy.execute_timeout, body),
                            structured=summary)
    if returncode != 0:
        return error_result(header + body, structured=summary)
    return text_result(header + body, structured=summary)


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
