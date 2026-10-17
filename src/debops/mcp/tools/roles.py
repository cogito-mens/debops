# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe the roles of the bundled DebOps Collection."""

import logging
import os
import re
import yaml

from ...constants import DEBOPS_PACKAGE_DATA
from .common import (FOLD_MARKER, dump_default, error_result, text_result)

logger = logging.getLogger(__name__)

# Roles shipped in the debops.debops Ansible Collection
COLLECTION_ROLES = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections',
        'ansible_collections', 'debops', 'debops', 'roles')

# Role directory names are restricted, which also prevents path traversal
ROLE_NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')

# Start of a documented variable: '.. envvar:: <name>', optionally with a
# fold marker on the same line ('# .. envvar:: <name> [[[')
_ENVVAR_DIRECTIVE = re.compile(r'^\s*#\s*\.\. envvar::\s*([^\s\[]+)')


def project_role_paths(project):
    """The project's own role directories, in resolution order.

    Loose roles live in 'ansible/playbooks/roles' (the project roles
    path in the generated ansible.cfg), collection roles in
    'ansible/collections/ansible_collections/<namespace>/<name>/roles'.
    No ansible-galaxy call: a direct scan, so the role tools do not
    depend on it.
    """
    paths = []
    loose = os.path.join(project.path, 'ansible', 'playbooks', 'roles')
    if os.path.isdir(loose):
        paths.append(loose)
    collections_root = os.path.join(project.path, 'ansible',
                                    'collections', 'ansible_collections')
    if os.path.isdir(collections_root):
        for namespace in sorted(os.listdir(collections_root)):
            ns_path = os.path.join(collections_root, namespace)
            if not os.path.isdir(ns_path) or namespace.startswith('.'):
                continue
            for collection in sorted(os.listdir(ns_path)):
                roles = os.path.join(ns_path, collection, 'roles')
                if os.path.isdir(roles):
                    paths.append(roles)
    return paths


def resolve_role(roles_paths, name):
    """Validate a role name and return (name, path), or None if unknown.

    roles_paths is a list of directories searched in order; the first
    match wins, so earlier paths shadow later ones.
    """
    # Accept both 'nginx' and the fully qualified 'debops.nginx'
    name = str(name or '').split('.')[-1]
    if not ROLE_NAME_PATTERN.match(name):
        return None
    for roles_path in roles_paths:
        path = os.path.join(roles_path, name)
        if os.path.isdir(path):
            return name, path
    return None


def load_role_defaults(role_path):
    """Load a role's defaults sources in the order Ansible reads them.

    A role defines its defaults in 'defaults/main.yml', in a
    'defaults/main/' directory whose files are read in alphabetical
    order, or both. Returns an ordered list of (source_name, text) pairs.
    """
    sources = []
    main_file = os.path.join(role_path, 'defaults', 'main.yml')
    if os.path.isfile(main_file):
        with open(main_file) as fp:
            sources.append(('main.yml', fp.read()))
    main_dir = os.path.join(role_path, 'defaults', 'main')
    if os.path.isdir(main_dir):
        for filename in sorted(os.listdir(main_dir)):
            if (filename.endswith(('.yml', '.yaml'))
                    and not filename.startswith('.')):
                with open(os.path.join(main_dir, filename)) as fp:
                    sources.append((filename, fp.read()))
    return sources


def role_default_values(sources):
    """Merge the default values of all sources; later files win.

    Returns (values, partial): 'partial' marks sources which did not
    parse as YAML.
    """
    values = {}
    partial = False
    for _source, text in sources:
        try:
            values.update(yaml.safe_load(text) or {})
        except yaml.YAMLError:
            partial = True
    return values, partial


def extract_envvar_docs(text):
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
            if FOLD_MARKER.match(line):
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


def tool_role_info(roles_paths, arguments):
    resolved = resolve_role(roles_paths, arguments.get('name'))
    if resolved is None:
        return error_result('role not found or invalid: {}'
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

    variables, partial = role_default_values(load_role_defaults(path))

    lines = ['# debops.{}'.format(name), '']
    if description:
        lines.extend([description, ''])
    variables_line = str(len(variables))
    if partial:
        variables_line += ' (partial, a defaults file did not parse)'
    lines.extend([
            '- variables: {}'.format(variables_line),
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
    return text_result('\n'.join(lines))


def tool_role_variables(roles_paths, arguments):
    resolved = resolve_role(roles_paths, arguments.get('name'))
    if resolved is None:
        return error_result('role not found or invalid: {}'
                            .format(arguments.get('name')))
    name, path = resolved

    sources = load_role_defaults(path)
    if not sources:
        return error_result('role debops.{} has no defaults'.format(name))

    values = {}
    per_file_values = []
    for source, text in sources:
        try:
            file_values = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            file_values = None
        if file_values:
            values.update(file_values)
        per_file_values.append((source, file_values))

    # Documented variables first, in file order, then undocumented ones;
    # the first documentation of a variable wins, while values of later
    # files override earlier ones, as Ansible does
    entries = []
    seen = set()
    for source, text in sources:
        for var_name, description, value_text in extract_envvar_docs(text):
            if var_name not in seen:
                entries.append((var_name, description, value_text, source))
                seen.add(var_name)
    for source, file_values in per_file_values:
        if not file_values:
            continue
        for var_name in file_values:
            if var_name not in seen:
                entries.append((var_name, '', '', source))
                seen.add(var_name)

    name_filter = str(arguments.get('filter') or '').lower()
    if name_filter:
        entries = [entry for entry in entries
                   if name_filter in entry[0].lower()]

    if not entries:
        return text_result('no matching variables in debops.{} '
                           'defaults'.format(name))

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))

    header = '# debops.{} default variables ({})'.format(name,
                                                         len(entries))
    if detail == 'names':
        return text_result('\n'.join(
                [header, ''] + [entry[0] for entry in entries]))

    if detail == 'brief':
        lines = [header, '']
        for var_name, description, value_text, _source in entries:
            summary = ''
            for description_line in description.splitlines():
                if description_line.strip():
                    summary = description_line.strip()
                    break
            if var_name in values:
                default = dump_default(values[var_name], flow=True)
            else:
                default = ' '.join(value_text.split())
            if len(default) > 100:
                default = default[:97] + '…'
            line = '- **{}**'.format(var_name)
            if summary:
                line += ' — {}'.format(summary)
            if default:
                line += ' (default: `{}`)'.format(default)
            lines.append(line)
        return text_result('\n'.join(lines))

    lines = [header]
    if len(sources) > 1:
        lines[0] += ' ({} files)'.format(len(sources))
    lines.append('')
    for source, _text in sources:
        group = [entry for entry in entries if entry[3] == source]
        if not group:
            continue
        if len(sources) > 1:
            lines.extend(['**File: {}**'.format(source), ''])
        for var_name, description, value_text, _source in group:
            lines.append('## {}'.format(var_name))
            lines.append('')
            if description:
                lines.extend([description, ''])
            default = None
            if var_name in values:
                default = dump_default(values[var_name])
            elif value_text:
                default = value_text
            if default:
                lines.extend(['```yaml', default, '```', ''])
    return text_result('\n'.join(lines))
