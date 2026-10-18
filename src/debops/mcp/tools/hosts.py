# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which query managed hosts directly (a 'debops exec' subset).

Only connectivity checks and fact gathering are exposed — arbitrary
modules are not. The commands contact live hosts but change nothing;
they are serialized with playbook executions.
"""

import fnmatch
import json
import logging
import os
import re
import subprocess
import yaml

from ...ansible.inventory import AnsibleInventory
from ..server import RequestCancelled
from .common import error_result, text_result
from .execute import _EXECUTION_LOCK, resolve_hosts, run_cancellable

logger = logging.getLogger(__name__)

# exec calls get a shorter leash than playbook runs
EXEC_TIMEOUT_LIMIT = 120  # seconds

# Facts output is capped per host and in total; a full setup dump is
# hundreds of kilobytes per host
FACTS_HOST_LIMIT = 16 * 1024
EXEC_OUTPUT_LIMIT = 64 * 1024

# A host pattern is a single word (no whitespace or control characters);
# the command runs without a shell, so that is sufficient
_FACT_FILTER = re.compile(r'^[A-Za-z0-9_*?\[\].\-]+$')


def _validate_host_pattern(pattern):
    return bool(pattern) and not any(c.isspace() or ord(c) < 32
                                     for c in pattern)


def _validate_fact_filter(fact_filter):
    return bool(_FACT_FILTER.match(fact_filter))


def _render_facts(output):
    """Render 'ansible -m setup' output as per-host YAML.

    Ansible pretty-prints the facts JSON over multiple lines, so the
    payload of a '<host> | SUCCESS =>' line is accumulated until it
    parses as complete JSON. Unparseable blocks pass through unchanged.
    """
    rendered = []
    pending_host = None
    pending_lines = []

    def flush():
        nonlocal pending_host, pending_lines
        if pending_host is None:
            return
        payload = '\n'.join(pending_lines)
        try:
            facts = json.loads(payload)
        except ValueError:
            rendered.append('{} => {}'.format(pending_host, payload))
        else:
            text = yaml.safe_dump(facts, default_flow_style=False,
                                  width=1000)
            if len(text) > FACTS_HOST_LIMIT:
                text = (text[:FACTS_HOST_LIMIT]
                        + '\n… (facts for this host truncated)\n')
            rendered.append('## {}\n\n{}'.format(pending_host, text))
        pending_host = None
        pending_lines = []

    for line in output.splitlines():
        header = re.match(r'^(.*) \| \w+ => (\{.*)$', line)
        if header:
            # A new result block starts; flush any previous one
            flush()
            pending_host = header.group(1)
            pending_lines = [header.group(2)]
            try:
                json.loads(header.group(2))
            except ValueError:
                continue
            flush()
            continue
        if pending_host is not None:
            pending_lines.append(line)
            try:
                json.loads('\n'.join(pending_lines))
            except ValueError:
                continue
            flush()
            continue
        rendered.append(line)
    flush()
    return '\n\n'.join(rendered)


def _exec(project, policy, arguments, context, module, module_filter):
    hosts = str(arguments.get('hosts') or '')
    if not _validate_host_pattern(hosts):
        return error_result('invalid host pattern (a single word '
                            'expected): {!r}'.format(hosts))
    become = arguments.get('become', False)
    if not isinstance(become, bool):
        return error_result('the "become" argument must be a boolean')

    ansible = project.config.raw.get('binaries', {}).get('ansible',
                                                         'ansible')
    command = [ansible, hosts, '-m', module]
    if become:
        command.append('--become')
    if module_filter:
        if not _validate_fact_filter(module_filter):
            return error_result('invalid facts filter (glob characters '
                                'only): {!r}'.format(module_filter))
        command.extend(['-a', 'filter=' + module_filter])

    with _EXECUTION_LOCK:
        inventory = AnsibleInventory(project, name=project.view)
        unlocked = False
        try:
            unlocked = inventory.unlock()
            env = os.environ.copy()
            env.update(project.config._env_vars)

            # Resolve the pattern to concrete hosts first: the inventory
            # is the security boundary, and the resolution is the audit
            # trail and the policy enforcement point
            try:
                resolved = resolve_hosts(env, project.path,
                                         [ansible, hosts])
            except (ValueError, subprocess.TimeoutExpired,
                    FileNotFoundError) as errmsg:
                return error_result(str(errmsg))
            if not resolved:
                return error_result('the pattern matches no hosts in the '
                                    'inventory: {}'.format(hosts))
            rejected = [host for host in resolved
                        if policy.exec_hosts
                        and not any(fnmatch.fnmatch(host, allowed)
                                    for allowed in policy.exec_hosts)]
            if rejected:
                return error_result('not allowed by the project policy '
                                    '(project.mcp.exec_hosts): '
                                    + ', '.join(rejected))

            logger.notice('Querying hosts: %s → %s', hosts,
                          ', '.join(resolved))
            timeout = min(policy.execute_timeout if policy is not None
                          else 3600, EXEC_TIMEOUT_LIMIT)
            output, returncode, status = run_cancellable(
                    command, env=env, cwd=project.path, context=context,
                    timeout=timeout)
        finally:
            if unlocked:
                inventory.lock()

    if status == 'cancelled':
        raise RequestCancelled()
    if status == 'timeout':
        return error_result('timed out after {} seconds'.format(timeout))
    if status == 'disabled':
        return error_result('terminated: the MCP functionality was '
                            'disabled mid-run (the kill switch)')

    if module == 'ansible.builtin.ping':
        text = '# ping {}\n\nHosts: {}\n\n{}'.format(
                hosts, ', '.join(resolved), output)
    else:
        text = '# facts for {}\n\nHosts: {}\n\n{}'.format(
                hosts, ', '.join(resolved), _render_facts(output))
    # ansible returns non-zero when any host failed
    if returncode != 0:
        return error_result(text)
    return text_result(text)


def tool_exec_ping(project, policy, arguments, context):
    return _exec(project, policy, arguments, context,
                 'ansible.builtin.ping', None)


def tool_exec_facts(project, policy, arguments, context):
    return _exec(project, policy, arguments, context,
                 'ansible.builtin.setup',
                 str(arguments.get('filter') or '') or None)
