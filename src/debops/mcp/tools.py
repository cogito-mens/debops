# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The tools exposed by the DebOps MCP server.

Each tool is a closure over the project and the policy, registered with
the server at startup. Tool logic errors are reported in the result with
'isError', not as JSON-RPC errors, per the MCP specification.
"""

import logging
import os
import re
import yaml

from ..constants import DEBOPS_PACKAGE_DATA

logger = logging.getLogger(__name__)

# Roles shipped in the debops.debops Ansible Collection
COLLECTION_ROLES = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections',
        'ansible_collections', 'debops', 'debops', 'roles')

# Role directory names are restricted, which also prevents path traversal
ROLE_NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')

# Fold markers used in DebOps defaults files
_FOLD_MARKER = re.compile(r'^\s*#\s*(\[{3}|\]{3})\d?\s*$')

# Start of a documented variable: '.. envvar:: <name>', optionally with a
# fold marker on the same line ('# .. envvar:: <name> [[[')
_ENVVAR_DIRECTIVE = re.compile(r'^\s*#\s*\.\. envvar::\s*([^\s\[]+)')


def _text_result(text):
    return {'content': [{'type': 'text', 'text': text}], 'isError': False}


def _error_result(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def _resolve_role(roles_path, name):
    """Validate a role name and return (name, path), or None if unknown."""
    # Accept both 'nginx' and the fully qualified 'debops.nginx'
    name = str(name or '').split('.')[-1]
    if not ROLE_NAME_PATTERN.match(name):
        return None
    path = os.path.join(roles_path, name)
    if not os.path.isdir(path):
        return None
    return name, path


def _extract_envvar_docs(text):
    """Extract documented variables from a DebOps defaults file.

    Returns an ordered list of (name, description, value_text) tuples,
    parsed from the '.. envvar::' comment blocks which precede each
    documented variable definition. The value text is only a fallback for
    files which do not parse as YAML.
    """
    entries = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _ENVVAR_DIRECTIVE.match(lines[index])
        if not match:
            index += 1
            continue
        name = match.group(1)
        index += 1

        description_lines = []
        while index < len(lines):
            line = lines[index]
            if _FOLD_MARKER.match(line):
                index += 1
                break
            if line.startswith('#'):
                description_lines.append(
                        re.sub(r'^#\s?', '', line).rstrip())
                index += 1
            elif not line.strip():
                index += 1
            else:
                break

        value_lines = []
        while index < len(lines):
            line = lines[index]
            if line.startswith('#') or not line.strip():
                break
            value_lines.append(line)
            index += 1

        entries.append((name, '\n'.join(description_lines).strip(),
                        '\n'.join(value_lines).strip()))
    return entries


def _tool_project_status(project, policy, arguments):
    views = sorted(project.config.raw.get('views', {}).keys())
    if project.view not in views:
        views.append(project.view)

    inventory_path = os.path.join(policy.view_root, 'inventory')
    secret_path = os.path.join(policy.view_root, 'secret')
    if os.path.exists(os.path.join(policy.view_root, '.encfs.secret')):
        encryption = 'encfs'
    elif os.path.exists(os.path.join(project.path, '.git', 'git-crypt',
                                     'keys')):
        encryption = 'git-crypt'
    else:
        encryption = 'none'

    lines = [
            'Project: {}'.format(project.name),
            'Path: {}'.format(project.path),
            'Type: {}'.format(project.project_type),
            'View: {}'.format(project.view),
            'Views available: {}'.format(', '.join(views)),
            'View root: {}{}'.format(
                policy.view_root,
                '' if os.path.isdir(policy.view_root) else ' (missing)'),
            'Inventory: {}'.format(
                'present' if os.path.isdir(inventory_path) else 'absent'),
            'Secrets: {}'.format(
                'present' if os.path.exists(secret_path) else 'absent'),
            'Encryption: {}'.format(encryption),
    ]
    return _text_result('\n'.join(lines))


def _tool_role_info(roles_path, arguments):
    resolved = _resolve_role(roles_path, arguments.get('name'))
    if resolved is None:
        return _error_result('role not found or invalid: {}'
                             .format(arguments.get('name')))
    name, path = resolved

    description = ''
    tags = []
    dependencies = []
    meta_file = os.path.join(path, 'meta', 'main.yml')
    if os.path.isfile(meta_file):
        try:
            with open(meta_file) as fp:
                meta = yaml.safe_load(fp) or {}
            galaxy_info = meta.get('galaxy_info', {})
            description = galaxy_info.get('description', '')
            tags = galaxy_info.get('galaxy_tags', [])
            dependencies = meta.get('dependencies', [])
        except yaml.YAMLError as errmsg:
            logger.warning('Cannot parse %s: %s', meta_file, errmsg)

    defaults_file = os.path.join(path, 'defaults', 'main.yml')
    variables = 0
    if os.path.isfile(defaults_file):
        try:
            with open(defaults_file) as fp:
                variables = len(yaml.safe_load(fp) or {})
        except yaml.YAMLError:
            variables = -1

    lines = ['# debops.{}'.format(name), '']
    if description:
        lines.extend([description, ''])
    lines.extend([
            '- variables: {}'.format('unparseable defaults file'
                                     if variables < 0 else variables),
            '- tasks: {}'.format(
                'yes' if os.path.isfile(os.path.join(
                    path, 'tasks', 'main.yml')) else 'no'),
            '- templates: {}'.format(
                'yes' if os.path.isdir(os.path.join(
                    path, 'templates')) else 'no'),
            '- handlers: {}'.format(
                'yes' if os.path.isfile(os.path.join(
                    path, 'handlers', 'main.yml')) else 'no'),
    ])
    if tags:
        lines.append('- galaxy tags: {}'.format(', '.join(tags)))
    if dependencies:
        lines.append('- dependencies: {}'.format(', '.join(
                dep if isinstance(dep, str) else dep.get('role', '?')
                for dep in dependencies)))
    return _text_result('\n'.join(lines))


def _tool_role_variables(roles_path, arguments):
    resolved = _resolve_role(roles_path, arguments.get('name'))
    if resolved is None:
        return _error_result('role not found or invalid: {}'
                             .format(arguments.get('name')))
    name, path = resolved

    defaults_file = os.path.join(path, 'defaults', 'main.yml')
    if not os.path.isfile(defaults_file):
        return _error_result('role debops.{} has no defaults/main.yml'
                             .format(name))
    with open(defaults_file) as fp:
        text = fp.read()

    try:
        values = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        values = None

    # Documented variables, in file order, then any undocumented ones
    entries = []
    seen = set()
    for var_name, description, value_text in _extract_envvar_docs(text):
        entries.append((var_name, description, value_text))
        seen.add(var_name)
    if values is not None:
        for var_name in values:
            if var_name not in seen:
                entries.append((var_name, '', ''))

    name_filter = str(arguments.get('filter') or '').lower()
    if name_filter:
        entries = [entry for entry in entries
                   if name_filter in entry[0].lower()]

    if not entries:
        return _text_result('no matching variables in debops.{} '
                            'defaults'.format(name))

    lines = ['# debops.{} default variables'.format(name), '']
    for var_name, description, value_text in entries:
        lines.append('## {}'.format(var_name))
        lines.append('')
        if description:
            lines.extend([description, ''])
        default = None
        if values is not None and var_name in values:
            default = yaml.safe_dump(values[var_name],
                                     default_flow_style=None,
                                     width=1000).strip()
        elif value_text:
            default = value_text
        if default:
            lines.extend(['```yaml', default, '```', ''])
    return _text_result('\n'.join(lines))


def register_project_tools(server, roles_path=COLLECTION_ROLES):
    """Register the built-in tools, honoring the server policy."""
    project = server.project
    policy = server.policy

    tools = [
        ('project_status',
         'Report the status of the DebOps project: type, current '
         'infrastructure view, inventory and secrets presence, encryption.',
         {'type': 'object', 'properties': {}},
         lambda arguments: _tool_project_status(project, policy, arguments)),
        ('role_info',
         'Describe a DebOps role: purpose, tags, dependencies, and which '
         'parts (defaults, tasks, templates) it provides.',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'Role name, with or '
                                  'without the "debops." prefix'}},
          'required': ['name']},
         lambda arguments: _tool_role_info(roles_path, arguments)),
        ('role_variables',
         'List the default inventory variables of a DebOps role, with '
         'their documentation and default values. Use this to learn how '
         'to configure a role through the Ansible inventory.',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'Role name, with or '
                                  'without the "debops." prefix'},
                         'filter': {'type': 'string',
                                    'description': 'Only include variables '
                                    'whose name contains this string'}},
          'required': ['name']},
         lambda arguments: _tool_role_variables(roles_path, arguments)),
    ]

    for name, description, schema, handler in tools:
        if policy is None or policy.is_tool_allowed(name):
            server.register_tool(name, description, schema, handler)
