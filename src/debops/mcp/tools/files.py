# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only tools for project files and the computed inventory.

Read tools are confined to the project directory, minus a deny list of
sensitive paths (secrets, key material, virtual environments); write
tools stay confined to the infrastructure view. Inventory queries run
'ansible-inventory', which inspects the inventory the same way the
'project init --graph/--list/--host' options do.
"""

import logging
import os
import subprocess

from ..policy import confine_under
from .common import dump_default, error_result, text_result

logger = logging.getLogger(__name__)

# Files larger than this are not read into a tool result
READ_SIZE_LIMIT = 256 * 1024

# Inventory output larger than this is truncated; the full '--list' dump
# of a big environment can be enormous
INVENTORY_OUTPUT_LIMIT = 64 * 1024

# An inventory listing is capped against pathological cases
LIST_ENTRY_LIMIT = 500

INVENTORY_QUERY_ACTIONS = {
    'graph': '--graph',
    'list': '--list',
    'host': '--host',
}


def tool_project_files(project, policy, arguments):
    subdir = str(arguments.get('path') or '').strip('/')
    name_filter = str(arguments.get('filter') or '').lower()

    try:
        base = confine_under(project.path, subdir or '.')
    except ValueError as errmsg:
        return error_result(str(errmsg))
    if not os.path.isdir(base):
        return error_result('directory not found or invalid: {}'
                            .format(subdir))

    lines = []
    truncated = False
    for root, dirs, files in os.walk(base):
        relative_root = os.path.relpath(root, project.path)

        def keep(entry):
            if relative_root == '.':
                relative = entry
            else:
                relative = os.path.join(relative_root, entry)
            return not policy.is_read_denied(relative)

        dirs[:] = [directory for directory in sorted(dirs) if
                   keep(directory)]
        for filename in sorted(files):
            if not keep(filename):
                continue
            relative = filename if relative_root == '.' \
                else os.path.join(relative_root, filename)
            if name_filter and name_filter not in relative.lower():
                continue
            lines.append(relative)
            if len(lines) >= LIST_ENTRY_LIMIT:
                truncated = True
                break
        if truncated:
            break

    lines = sorted(lines)
    header = '# Project files under \'{}\' ({}{})'.format(
            subdir or '.', len(lines),
            ', truncated at {} entries'.format(LIST_ENTRY_LIMIT)
            if truncated else '')
    if not lines:
        return text_result(header + ' — none found')
    return text_result('\n'.join([header, ''] +
                                 ['- {}'.format(line) for line in lines]))


def tool_project_read(project, policy, arguments):
    relative = str(arguments.get('path') or '').strip('/')
    if not relative:
        return error_result('the "path" argument is required')
    try:
        path = confine_under(project.path, relative)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    # Check the canonical path against the deny list, so that symlinks
    # cannot smuggle in denied content
    canonical_relative = os.path.relpath(
            path, os.path.realpath(project.path))
    if policy.is_read_denied(canonical_relative):
        return error_result('path is denied by policy: ' + relative)

    if not os.path.isfile(path):
        return error_result('file not found: ' + relative)
    if os.path.getsize(path) > READ_SIZE_LIMIT:
        return error_result('file is larger than {} bytes: {}'.format(
                READ_SIZE_LIMIT, relative))
    try:
        with open(path, 'rb') as fp:
            content = fp.read().decode('utf-8')
    except UnicodeDecodeError:
        return error_result('not a UTF-8 text file: ' + relative)
    except OSError as errmsg:
        return error_result(str(errmsg))

    if content and not content.endswith('\n'):
        content += '\n'
    return text_result('# {}\n\n```\n{}```'.format(relative, content))


def tool_config_get(project, arguments):
    key = str(arguments.get('key') or '').strip('.')
    if not key:
        return error_result('the "key" argument is required')
    node = project.config.raw
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return error_result('configuration key not found: ' + key)
        node = node[part]
    return text_result('{}:\n\n```yaml\n{}\n```'.format(
            key, dump_default(node)))


def tool_inventory_query(project, arguments):
    action = str(arguments.get('action') or 'graph')
    if action not in INVENTORY_QUERY_ACTIONS:
        return error_result('invalid action: {} (choose from {})'.format(
                action, ', '.join(sorted(INVENTORY_QUERY_ACTIONS))))

    command = ['ansible-inventory', INVENTORY_QUERY_ACTIONS[action]]
    if action == 'host':
        host = str(arguments.get('host') or '')
        if not host or host.startswith('-'):
            return error_result('the "host" argument is required and '
                                'must be a host name')
        command.append(host)

    env = os.environ.copy()
    env.update(project.config._env_vars)
    try:
        result = subprocess.run(
                command, env=env, cwd=project.path,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=60)
    except FileNotFoundError:
        return error_result('the ansible-inventory command was not found')
    except subprocess.TimeoutExpired:
        return error_result('ansible-inventory timed out')
    if result.returncode != 0:
        return error_result('ansible-inventory failed: '
                            + result.stderr.strip()[-2000:])

    output = result.stdout
    if len(output) > INVENTORY_OUTPUT_LIMIT:
        output = (output[:INVENTORY_OUTPUT_LIMIT]
                  + '\n… (truncated; use the "graph" action or query a '
                    'single host for a narrower view)')
    return text_result(output)
