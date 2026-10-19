# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Custom script tools from '.debops/mcp/tools/'.

A custom tool is an executable script (a shebang decides): the call's
arguments arrive as one JSON object on standard input, the text on
standard output is the tool result, and a non-zero exit code with the
stderr tail is a tool error. The scripts run as the server's account
on the controller, under the exclusive lock, confirmed by the user on
every call, and only when the project enables them
('project.mcp.custom_tools').
"""

import json
import logging
import os

from ..server import RequestCancelled
from ..skills import SKILL_NAME_PATTERN
from .common import (confirmation_flow, error_result,
                     exclusive_project, mcp_environ, text_result)
from .execute import run_cancellable

logger = logging.getLogger(__name__)

# How long a custom tool may run
CUSTOM_TOOL_TIMEOUT = 300  # seconds


def custom_tools_dir(project):
    return os.path.join(project.path, '.debops', 'mcp', 'tools')


def _tool_description(path):
    """The description from the script's '# description:' line."""
    try:
        with open(path, 'rb') as fp:
            head = fp.read(4096).decode('utf-8', errors='replace')
    except OSError:
        return ''
    for line in head.splitlines():
        stripped = line.lstrip('#').strip()
        if stripped.lower().startswith('description:'):
            return stripped.split(':', 1)[1].strip()
    return ''


def discover_custom_tools(project):
    """The valid custom tools of the project: name → (path,
    description)."""
    tools_dir = custom_tools_dir(project)
    found = {}
    if not os.path.isdir(tools_dir):
        return found
    for entry in sorted(os.listdir(tools_dir)):
        path = os.path.join(tools_dir, entry)
        if not SKILL_NAME_PATTERN.match(entry):
            logger.warning('Ignoring a custom tool with an invalid '
                           'name: %s', entry)
            continue
        if (os.path.islink(path) or not os.path.isfile(path)
                or not os.access(path, os.X_OK)):
            logger.warning('Ignoring a custom tool which is not an '
                           'executable regular file: %s', entry)
            continue
        found[entry] = (path, _tool_description(path))
    return found


def make_custom_tool(project, policy, name, path, enabled_check=None,
                     on_disabled=None):
    """Build the handler for one custom script tool."""
    def handler(arguments, context):
        # Every call of a custom tool asks first; the feature being
        # enabled must not open an unattended code execution path
        verdict = confirmation_flow(
                context, 'Run the custom tool "{}" of this project?'
                .format(name))
        if verdict is not True:
            if verdict is False:
                return error_result('the call was declined by the user')
            return verdict  # the elicitation request

        with exclusive_project(project, policy,
                               note='custom tool ' + name):
            output, returncode, status = run_cancellable(
                    [path], env=mcp_environ(project), cwd=project.path,
                    context=context, timeout=CUSTOM_TOOL_TIMEOUT,
                    enabled_check=enabled_check, on_disabled=on_disabled,
                    input_data=json.dumps(arguments))
        if status == 'cancelled':
            raise RequestCancelled()
        if status == 'timeout':
            return error_result('the tool timed out after {} seconds'
                                .format(CUSTOM_TOOL_TIMEOUT))
        if status == 'disabled':
            return error_result('terminated: the MCP functionality was '
                                'disabled mid-run (the kill switch)')
        if returncode != 0:
            return error_result('the tool failed (exit code {}):\n\n{}'
                                .format(returncode,
                                        output.strip()[-2000:]))
        return text_result(output.strip() + '\n')
    return handler
