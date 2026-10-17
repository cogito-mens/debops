# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe roles and playbooks of external Collections.

External collections are enumerated with 'ansible-galaxy collection
list' executed in the project environment, which resolves the generated
ansible.cfg, the project virtual environment's implicit site-packages
path and '~' expansion.
"""

import json
import logging
import os
import re
import subprocess
import yaml

from ..policy import confine_under
from .common import (FOLD_MARKER, dump_default, error_result, text_result)
from .playbooks import (list_playbooks, parse_playbook, playbook_summary,
                        render_playbook_info, target_form)
from .roles import load_role_defaults, role_default_values

logger = logging.getLogger(__name__)

# A fully qualified role name: 'namespace.collection.role'
ROLE_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$')

# A fully qualified collection name: 'namespace.collection'
COLLECTION_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+$')

# How long the collection index is cached in the server process;
# collections change only when the user installs or removes them
COLLECTION_INDEX_TTL = 60  # seconds


def galaxy_collection_list(project):
    """Run 'ansible-galaxy collection list' in the project environment.

    The environment computed by DebOps points PATH at the project's
    virtual environment when one is enabled, which is what makes the
    collections installed there visible. No hooks run and the inventory
    stays locked: listing collections needs neither.
    """
    env = os.environ.copy()
    env.update(project.config._env_vars)
    try:
        result = subprocess.run(
                ['ansible-galaxy', 'collection', 'list', '--format', 'json'],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError('the ansible-galaxy command was not found')
    except subprocess.TimeoutExpired:
        raise RuntimeError('ansible-galaxy collection list timed out')
    if result.returncode != 0:
        raise RuntimeError('ansible-galaxy collection list failed: '
                           + result.stderr.strip())
    return json.loads(result.stdout)


def index_from_galaxy(data):
    """Turn 'ansible-galaxy collection list' JSON into {fqcn: entry}.

    When the same collection is installed in multiple paths, the first
    reported location wins.
    """
    index = {}
    for path, collections in data.items():
        for fqcn, info in collections.items():
            index.setdefault(fqcn, {
                'path': os.path.join(path, *fqcn.split('.')),
                'version': str(info.get('version', '')),
            })
    return index


def restrict_content(provider, project, policy):
    """Wrap a collection index provider with the content policy.

    With 'bundled_content: false' the DebOps collection is dropped even
    when it lives inside the project; with 'external_content: false',
    anything which is neither DebOps nor project-local is dropped. The
    project's own collections are always visible.
    """
    bundled = policy.bundled_content if policy is not None else True
    external = policy.external_content if policy is not None else True
    if bundled and external:
        return provider
    project_root = os.path.realpath(os.path.join(
            project.path, 'ansible', 'collections',
            'ansible_collections'))

    def restricted():
        index = provider()
        visible = {}
        for fqcn, entry in index.items():
            if fqcn == 'debops.debops':
                if bundled:
                    visible[fqcn] = entry
                continue
            path = os.path.realpath(entry['path'])
            if (os.path.commonpath((project_root, path))
                    == project_root):
                visible[fqcn] = entry
                continue
            if external:
                visible[fqcn] = entry
        dropped = len(index) - len(visible)
        if dropped:
            logger.debug('Content policy dropped %d collection(s)',
                         dropped)
        return visible

    return restricted


def list_collection_roles(collection_path):
    """Return the role names of a collection, sorted."""
    roles_dir = os.path.join(collection_path, 'roles')
    roles = []
    if os.path.isdir(roles_dir):
        for entry in sorted(os.listdir(roles_dir)):
            if (os.path.isdir(os.path.join(roles_dir, entry))
                    and not entry.startswith('.')):
                roles.append(entry)
    return roles


def resolve_collection_role(index, fqcn_role):
    """Resolve 'namespace.collection.role' to (name, path)."""
    if not ROLE_FQCN_PATTERN.match(str(fqcn_role or '')):
        return None
    namespace, collection, role = fqcn_role.split('.')
    entry = index.get('{}.{}'.format(namespace, collection))
    if entry is None:
        return None
    role_path = os.path.join(entry['path'], 'roles', role)
    if not os.path.isdir(role_path):
        return None
    return role, role_path


def resolve_collection(index, fqcn):
    """Resolve 'namespace.collection' to its index entry, or None."""
    if not COLLECTION_FQCN_PATTERN.match(str(fqcn or '')):
        return None
    return index.get(fqcn)


def load_argument_specs(role_path):
    """Load a role's meta/argument_specs.yml, or None when absent."""
    specs_file = os.path.join(role_path, 'meta', 'argument_specs.yml')
    if not os.path.isfile(specs_file):
        return None
    try:
        with open(specs_file) as fp:
            data = yaml.safe_load(fp) or {}
    except yaml.YAMLError:
        return None
    specs = data.get('argument_specs')
    return specs if isinstance(specs, dict) else None


def extract_plain_docs(text):
    """Associate top-level YAML keys with the comment block above them.

    A comment block documents the key it directly precedes; a blank line
    breaks the association. Used for roles which do not document their
    variables with '.. envvar::' directives.
    """
    entries = []
    comments = []
    for line in text.splitlines():
        if FOLD_MARKER.match(line):
            comments = []
        elif line.startswith('#'):
            comments.append(re.sub(r'^#\s?', '', line).rstrip())
        elif not line.strip():
            comments = []
        elif re.match(r'^[A-Za-z0-9_]+:', line):
            entries.append((re.match(r'^([A-Za-z0-9_]+):', line).group(1),
                            '\n'.join(comments).strip()))
            comments = []
        else:
            comments = []
    return entries


def tool_collection_roles(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = []
    total = 0
    for fqcn in sorted(index):
        if name_filter and name_filter not in fqcn.lower():
            continue
        roles = list_collection_roles(index[fqcn]['path'])
        if not roles:
            continue
        total += len(roles)
        version = index[fqcn]['version']
        lines.append('## {}{}'.format(
                fqcn, ' ({})'.format(version) if version else ''))
        lines.extend('- {}'.format(role) for role in roles)
        lines.append('')
    if not lines:
        return text_result('no collections with roles found')
    header = '# Collections with roles visible to Ansible ({})'.format(
            total)
    return text_result('\n'.join([header, ''] + lines))


def tool_collection_role_info(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))
    resolved = resolve_collection_role(index, arguments.get('role'))
    if resolved is None:
        return error_result('role not found or invalid: {}'
                            .format(arguments.get('role')))
    role, role_path = resolved
    fqcn_role = arguments.get('role')

    description = ''
    tags = []
    meta_file = os.path.join(role_path, 'meta', 'main.yml')
    if os.path.isfile(meta_file):
        try:
            with open(meta_file) as fp:
                meta = yaml.safe_load(fp) or {}
            galaxy_info = meta.get('galaxy_info', {})
            description = galaxy_info.get('description', '')
            tags = galaxy_info.get('galaxy_tags', [])
        except yaml.YAMLError as errmsg:
            logger.warning('Cannot parse %s: %s', meta_file, errmsg)

    specs = load_argument_specs(role_path)
    values, partial = role_default_values(load_role_defaults(role_path))

    lines = ['# {}'.format(fqcn_role), '']
    if description:
        lines.extend([description, ''])
    lines.append('- resolved at: {}'.format(role_path))
    variables_line = str(len(values))
    if partial:
        variables_line += ' (partial, a defaults file did not parse)'
    lines.append('- variables: {}'.format(variables_line))
    if specs:
        lines.append('- argument specs: {}'.format(', '.join(
                sorted(specs.keys()))))
    else:
        lines.append('- argument specs: none')
    lines.append('- README: {}'.format(
            'yes' if os.path.isfile(os.path.join(
                role_path, 'README.md')) else 'no'))
    lines.append('- tasks: {}'.format(
            'yes' if os.path.isfile(os.path.join(
                role_path, 'tasks', 'main.yml')) else 'no'))
    if tags:
        lines.append('- galaxy tags: {}'.format(', '.join(tags)))
    return text_result('\n'.join(lines))


def render_variable_entries(fqcn_role, entries, detail):
    """Render (name, info) variable entries at the given detail level.

    Each entry's info is a dict with 'description', 'type', 'required',
    'choices', 'default' and 'has_default' keys, any of which may be
    missing or empty.
    """
    header = '# {} role variables ({})'.format(fqcn_role, len(entries))
    if detail == 'names':
        return '\n'.join([header, ''] + [name for name, _info in entries])

    if detail == 'brief':
        lines = [header, '']
        for name, info in entries:
            summary = ''
            for description_line in info['description'].splitlines():
                if description_line.strip():
                    summary = description_line.strip()
                    break
            line = '- **{}**'.format(name)
            traits = []
            if info.get('type'):
                traits.append(info['type'])
            if info.get('required'):
                traits.append('required')
            if traits:
                line += ' ({})'.format(', '.join(traits))
            if summary:
                line += ' — {}'.format(summary)
            if info.get('has_default'):
                default = dump_default(info['default'], flow=True)
                if len(default) > 100:
                    default = default[:97] + '…'
                line += ' (default: `{}`)'.format(default)
            lines.append(line)
        return '\n'.join(lines)

    lines = [header, '']
    for name, info in entries:
        lines.append('## {}'.format(name))
        lines.append('')
        traits = []
        if info.get('type'):
            traits.append('type: {}'.format(info['type']))
        if info.get('required'):
            traits.append('required')
        if info.get('choices'):
            traits.append('choices: {}'.format(', '.join(
                    str(choice) for choice in info['choices'])))
        if traits:
            lines.extend(['({})'.format('; '.join(traits)), ''])
        if info['description']:
            lines.extend([info['description'], ''])
        if info.get('has_default'):
            lines.extend(['```yaml', dump_default(info['default']),
                          '```', ''])
    return '\n'.join(lines)


def tool_collection_role_variables(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))
    resolved = resolve_collection_role(index, arguments.get('role'))
    if resolved is None:
        return error_result('role not found or invalid: {}'
                            .format(arguments.get('role')))
    role, role_path = resolved
    fqcn_role = arguments.get('role')

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))

    entries = []
    seen = set()

    # Structured argument specs are authoritative when present
    specs = load_argument_specs(role_path)
    if specs:
        entry_point = specs.get('main')
        if entry_point is None:
            entry_point = next(iter(specs.values()), {})
        for opt_name, spec in (entry_point.get('options') or {}).items():
            if not isinstance(spec, dict):
                continue
            description = spec.get('description', '')
            if isinstance(description, list):
                description = '\n'.join(str(item) for item in description)
            entries.append((opt_name, {
                'description': description,
                'type': spec.get('type'),
                'required': spec.get('required', False),
                'choices': spec.get('choices'),
                'default': spec.get('default'),
                'has_default': 'default' in spec,
            }))
            seen.add(opt_name)

    # Defaults cover the rest, with their comment blocks as documentation
    sources = load_role_defaults(role_path)
    values, _partial = role_default_values(sources)
    for _source, text in sources:
        for var_name, description in extract_plain_docs(text):
            if var_name in seen:
                continue
            entries.append((var_name, {
                'description': description,
                'default': values.get(var_name),
                'has_default': var_name in values,
            }))
            seen.add(var_name)

    name_filter = str(arguments.get('filter') or '').lower()
    if name_filter:
        entries = [entry for entry in entries
                   if name_filter in entry[0].lower()]
    if not entries:
        return text_result('no matching variables in the {} role'
                           .format(fqcn_role))
    return text_result(render_variable_entries(fqcn_role, entries,
                                               detail))


def tool_collection_playbooks(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))
    collection = str(arguments.get('collection') or '')
    entry = resolve_collection(index, collection)
    if entry is None:
        return error_result('collection not found or invalid: {}'
                            .format(collection))

    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    if not os.path.isdir(playbooks_dir):
        return text_result('collection {} provides no playbooks'
                           .format(collection))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = ['# Playbooks in {} ({})'.format(
            collection, entry['version'] or 'unknown version'), '']
    total = 0
    for relpath in list_playbooks(playbooks_dir):
        if name_filter and name_filter not in relpath.lower():
            continue
        line = '- `{}/{}`'.format(collection, target_form(relpath))
        summary = playbook_summary(os.path.join(playbooks_dir, relpath))
        if summary:
            line += ' — {}'.format(summary)
        lines.append(line)
        total += 1
    if not total:
        return text_result('collection {} provides no playbooks'
                           .format(collection))
    return text_result('\n'.join(lines))


def tool_collection_playbook_info(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return error_result(str(errmsg))
    collection = str(arguments.get('collection') or '')
    entry = resolve_collection(index, collection)
    if entry is None:
        return error_result('collection not found or invalid: {}'
                            .format(collection))

    name = str(arguments.get('playbook') or '')
    if not name.endswith(('.yml', '.yaml')):
        name += '.yml'
    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    try:
        path = confine_under(playbooks_dir, name)
    except ValueError:
        return error_result('playbook not found or invalid: {}'.format(
                arguments.get('playbook')))
    if not os.path.isfile(path):
        return error_result('playbook not found: {}'.format(
                arguments.get('playbook')))
    try:
        with open(path) as fp:
            plays, roles, imports, groups = parse_playbook(fp.read())
    except (OSError, ValueError) as errmsg:
        return error_result(str(errmsg))
    target = '{}/{}'.format(collection, target_form(name))
    return text_result(render_playbook_info(
            '{}/{}'.format(collection, name), target, plays, roles,
            imports, groups))
