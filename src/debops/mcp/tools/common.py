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
