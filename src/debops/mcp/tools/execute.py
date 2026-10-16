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

import contextlib
import io
import logging
import os
import subprocess
import threading
import time

from ...ansible.inventory import AnsibleInventory
from ...ansibleplaybookrunner import AnsiblePlaybookRunner
from ...hooks import run_hooks
from ..policy import EXECUTE_OPTIONS
from ..server import RequestCancelled
from .common import error_result, text_result

logger = logging.getLogger(__name__)

# Only the tail of a run matters on failure (the recap is at the end)
EXECUTE_OUTPUT_LIMIT = 128 * 1024

# Grace period between SIGTERM and SIGKILL for a cancelled or timed out
# ansible-playbook process
TERMINATION_GRACE = 5  # seconds

# One execution at a time: inventory (un)locking and hook execution
# cannot race
_EXECUTION_LOCK = threading.Lock()

# ansible-playbook options the tools accept, mapped to their flags
ALLOWED_OPTIONS = {'limit': '--limit', 'tags': '--tags',
                   'skip_tags': '--skip-tags'}


def _build_ansible_args(arguments, check, allowed_options=EXECUTE_OPTIONS):
    """Build the ansible-playbook arguments from the tool arguments.

    Only options permitted by the project policy are accepted.
    """
    playbook = str(arguments.get('playbook') or '')
    if not playbook:
        raise ValueError('the "playbook" argument is required')

    args = [playbook]
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


def _execute_playbook(project, policy, arguments, context, check):
    mode = 'check' if check else 'run'

    with _EXECUTION_LOCK:
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

        logger.notice('Executing a playbook in %s mode: %s',
                      mode, ' '.join(command))
        if not run_hooks(project.path, 'pre-' + mode):
            return error_result('the pre-{} hook aborted the execution'
                                .format(mode))
        unlocked = False
        inventory = AnsibleInventory(project, name=project.view)
        try:
            unlocked = inventory.unlock()

            env = os.environ.copy()
            env.update(project.config._env_vars)
            process = subprocess.Popen(
                    command, env=env, cwd=project.path,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    universal_newlines=True)

            deadline = time.monotonic() + policy.execute_timeout
            cancelled = False
            timed_out = False
            while process.poll() is None:
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
                output, _ = process.communicate(timeout=TERMINATION_GRACE)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            returncode = process.returncode
        finally:
            if unlocked:
                inventory.lock()

    run_hooks(project.path, 'post-' + mode,
              extra_env={'DEBOPS_RETURN_CODE': returncode})

    if cancelled:
        raise RequestCancelled()

    if len(output) > EXECUTE_OUTPUT_LIMIT:
        output = ('… (earlier output truncated)\n'
                  + output[-EXECUTE_OUTPUT_LIMIT:])

    header = '# debops {} {}\n\nexit code: {}\n\n'.format(
            mode, args[0], returncode)
    if timed_out:
        return error_result(header + 'timed out after {} seconds\n\n{}'
                            .format(policy.execute_timeout, output))
    if returncode != 0:
        return error_result(header + output)
    return text_result(header + output)


def tool_check(project, policy, arguments, context):
    return _execute_playbook(project, policy, arguments, context,
                             check=True)


def tool_run(project, policy, arguments, context):
    return _execute_playbook(project, policy, arguments, context,
                             check=False)
