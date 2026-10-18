# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Shared helpers for the DebOps MCP tools."""

import re
import yaml

# Fold markers used in DebOps defaults files
FOLD_MARKER = re.compile(r'^\s*#\s*(\[{3}|\]{3})\d?\s*$')


def text_result(text):
    return {'content': [{'type': 'text', 'text': text}], 'isError': False}


def error_result(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


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
