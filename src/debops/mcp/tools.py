# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The tools exposed by the DebOps MCP server.

Each tool is a closure over the project and the policy, registered with
the server at startup. Tool logic errors are reported in the result with
'isError', not as JSON-RPC errors, per the MCP specification.
"""

import json
import logging
import os
import re
import subprocess
import time
import yaml

from ..constants import DEBOPS_PACKAGE_DATA
from .policy import confine_under

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


# A fully qualified role name: 'namespace.collection.role'
ROLE_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$')

# How long the collection index is cached in the server process;
# collections change only when the user installs or removes them
COLLECTION_INDEX_TTL = 60  # seconds


def _galaxy_collection_list(project):
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


def _index_from_galaxy(data):
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


def _list_collection_roles(collection_path):
    """Return the role names of a collection, sorted."""
    roles_dir = os.path.join(collection_path, 'roles')
    roles = []
    if os.path.isdir(roles_dir):
        for entry in sorted(os.listdir(roles_dir)):
            if (os.path.isdir(os.path.join(roles_dir, entry))
                    and not entry.startswith('.')):
                roles.append(entry)
    return roles


def _resolve_collection_role(index, fqcn_role):
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


def _load_argument_specs(role_path):
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


def _extract_plain_docs(text):
    """Associate top-level YAML keys with the comment block above them.

    A comment block documents the key it directly precedes; a blank line
    breaks the association. Used for roles which do not document their
    variables with '.. envvar::' directives.
    """
    entries = []
    comments = []
    for line in text.splitlines():
        if _FOLD_MARKER.match(line):
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


def _load_role_defaults(role_path):
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


def _dump_default(value, flow=None):
    """Render a default value as YAML, without the trailing
    end-of-document marker PyYAML adds for scalar documents."""
    text = yaml.safe_dump(value, default_flow_style=flow, width=1000)
    lines = text.rstrip().splitlines()
    if lines and lines[-1] == '...':
        lines.pop()
    return '\n'.join(lines)


def _role_default_values(sources):
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


# Playbooks shipped in the debops.debops Ansible Collection
COLLECTION_PLAYBOOKS = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections',
        'ansible_collections', 'debops', 'debops', 'playbooks')

# Subdirectories of a playbooks/ directory which hold supporting files
# (variable files, task files, ...) rather than runnable playbooks
PLAYBOOK_SUPPORT_DIRS = ('files', 'roles', 'tasks', 'templates', 'vars')


def _parse_playbook(text):
    """Parse a playbook into its plays, roles, imports and host groups.

    Returns (plays, roles, imports, groups). Role references are read
    both from the 'roles' section of a play and from task-level
    'include_role'/'import_role' statements; short role names are
    normalized to fully qualified ones using the play's 'collections'
    declaration.
    """
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as errmsg:
        raise ValueError('cannot parse the playbook: {}'.format(errmsg))

    plays = []
    roles = []
    imports = []
    groups = []

    def add_role(name, collections):
        if not name:
            return
        name = str(name)
        if '.' not in name and collections:
            name = str(collections[0]) + '.' + name
        if name not in roles:
            roles.append(name)

    for document in documents:
        if not isinstance(document, list):
            continue
        for entry in document:
            if not isinstance(entry, dict):
                continue
            if 'import_playbook' in entry:
                imports.append(str(entry['import_playbook']))
                continue

            hosts = entry.get('hosts') or []
            if isinstance(hosts, str):
                hosts = [hosts]
            hosts = [str(host) for host in hosts]
            collections = entry.get('collections') or []
            plays.append({
                'name': str(entry.get('name', '')),
                'hosts': hosts,
                'become': bool(entry.get('become', False)),
            })
            for host in hosts:
                if host not in groups:
                    groups.append(host)

            for role_entry in entry.get('roles') or []:
                if isinstance(role_entry, str):
                    add_role(role_entry, collections)
                elif isinstance(role_entry, dict):
                    add_role(role_entry.get('role')
                             or role_entry.get('name'), collections)

            # Roles may also be pulled in dynamically, from tasks
            for section in ('pre_tasks', 'tasks', 'post_tasks'):
                for task in entry.get(section) or []:
                    if not isinstance(task, dict):
                        continue
                    for key in ('include_role', 'import_role',
                                'ansible.builtin.include_role',
                                'ansible.builtin.import_role'):
                        statement = task.get(key)
                        if isinstance(statement, dict):
                            add_role(statement.get('name'), collections)

    return plays, roles, imports, groups


def _list_playbooks(root):
    """Return playbook paths under a root, relative, in stable order.

    Subdirectories which hold playbook support files ('files', 'vars',
    ...) instead of runnable playbooks are excluded.
    """
    playbooks = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [directory for directory in sorted(dirs)
                   if directory not in PLAYBOOK_SUPPORT_DIRS
                   and not directory.startswith('.')]
        for filename in sorted(files):
            if (filename.endswith(('.yml', '.yaml'))
                    and not filename.startswith('.')):
                playbooks.append(os.path.relpath(
                        os.path.join(base, filename), root))
    return sorted(playbooks)


def _playbook_summary(path):
    """A one-line summary of a playbook: first play name, host groups."""
    try:
        with open(path) as fp:
            plays, _roles, _imports, groups = _parse_playbook(fp.read())
    except (OSError, ValueError):
        return ''
    name = plays[0]['name'] if plays else ''
    if name and groups:
        return '{} (hosts: {})'.format(name, ', '.join(groups))
    return name or ', '.join(groups)


def _target_form(relative_path):
    """The name 'run'/'check' resolve: the path without the extension."""
    return os.path.splitext(relative_path)[0]


def _project_playbook_dirs(project, policy):
    """The project's playbook directories, in resolution order.

    Mirrors the playbook runner: the view's playbooks, then
    'ansible/playbooks', then 'playbooks' in the project root.
    """
    candidates = [
        ('view "{}"'.format(project.view),
         os.path.join(policy.view_root, 'playbooks')),
        ('ansible/playbooks',
         os.path.join(project.path, 'ansible', 'playbooks')),
        ('playbooks', os.path.join(project.path, 'playbooks')),
    ]
    seen = set()
    result = []
    for label, path in candidates:
        path = os.path.realpath(path)
        if path in seen or not os.path.isdir(path):
            continue
        seen.add(path)
        result.append((label, path))
    return result


def _resolve_debops_playbook(project, policy, playbooks_path, name):
    """Resolve a playbook target to (path, target form).

    Project playbook directories are searched first, then the bundled
    DebOps collection, mirroring the playbook runner; unlike the command
    line, direct filesystem paths outside these roots are not followed.
    """
    name = str(name or '')
    if not name.endswith(('.yml', '.yaml')):
        name += '.yml'
    try:
        for _label, directory in _project_playbook_dirs(project, policy):
            candidate = confine_under(directory, name)
            if os.path.isfile(candidate):
                return candidate, _target_form(name)
        candidate = confine_under(playbooks_path, name)
        if os.path.isfile(candidate):
            return candidate, _target_form(name)
    except ValueError:
        pass
    return None


def _render_playbook_info(title, target, plays, roles, imports, groups):
    lines = ['# {}'.format(title), '',
             'Run with: debops run {}'.format(target), '']
    if plays:
        lines.extend(['## Plays ({})'.format(len(plays)), ''])
        for play in plays:
            line = '- {}'.format(play['name'] or '(unnamed)')
            if play['hosts']:
                line += ' — hosts: {}'.format(', '.join(play['hosts']))
            if play['become']:
                line += ' (become)'
            lines.append(line)
        lines.append('')
    if roles:
        lines.extend(['## Roles ({})'.format(len(roles)), ''])
        lines.extend('- {}'.format(role) for role in roles)
        lines.append('')
    if groups:
        lines.extend(['## Inventory groups in use', ''])
        lines.extend('- {}'.format(group) for group in groups)
        lines.append('')
    if imports:
        lines.extend(['## Imported playbooks', ''])
        lines.extend('- {}'.format(item) for item in imports)
        lines.append('')
    return '\n'.join(lines)


def _tool_playbooks(project, policy, playbooks_path, arguments):
    name_filter = str(arguments.get('filter') or '').lower()
    sections = []

    # Project playbooks resolve first, mirroring the playbook runner
    for label, directory in _project_playbook_dirs(project, policy):
        entries = []
        for relpath in _list_playbooks(directory):
            if name_filter and name_filter not in relpath.lower():
                continue
            entries.append((relpath, _playbook_summary(
                    os.path.join(directory, relpath))))
        if entries:
            sections.append(('Project playbooks ({})'.format(label),
                             entries))

    # The bundled DebOps collection, grouped by subdirectory
    grouped = {}
    for relpath in _list_playbooks(playbooks_path):
        if name_filter and name_filter not in relpath.lower():
            continue
        group = os.path.dirname(relpath)
        grouped.setdefault(group, []).append(
                (relpath,
                 _playbook_summary(os.path.join(playbooks_path, relpath))))
    for group in sorted(grouped):
        title = 'DebOps collection' if not group \
            else 'DebOps collection: {}/'.format(group)
        sections.append((title, grouped[group]))

    lines = []
    total = 0
    for title, entries in sections:
        lines.append('## {}'.format(title))
        for relpath, summary in entries:
            line = '- `{}`'.format(_target_form(relpath))
            if summary:
                line += ' — {}'.format(summary)
            lines.append(line)
            total += 1
        lines.append('')

    # Playbook sets from the view configuration are also valid targets
    playbook_sets = (project.config.raw.get('views', {})
                     .get(project.view, {}).get('playbook_sets', {}))
    if playbook_sets:
        lines.append('## Playbook sets (view configuration)')
        for name in sorted(playbook_sets):
            members = playbook_sets[name]
            lines.append('- `{}` → {}'.format(
                    name, ', '.join(str(member) for member in members)))
        lines.append('')

    if not lines:
        return _text_result('no playbooks found')
    header = '# Playbook targets ({})'.format(total)
    return _text_result('\n'.join([header, ''] + lines))


def _tool_playbook_info(project, policy, playbooks_path, arguments):
    resolved = _resolve_debops_playbook(project, policy, playbooks_path,
                                        arguments.get('playbook'))
    if resolved is None:
        return _error_result('playbook not found or invalid: {}'.format(
                arguments.get('playbook')))
    path, target = resolved
    try:
        with open(path) as fp:
            plays, roles, imports, groups = _parse_playbook(fp.read())
    except (OSError, ValueError) as errmsg:
        return _error_result(str(errmsg))
    return _text_result(_render_playbook_info(target, target, plays, roles,
                                              imports, groups))


def _tool_collection_playbooks(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return _error_result(str(errmsg))
    collection = str(arguments.get('collection') or '')
    if not re.match(r'^[a-z0-9_]+\.[a-z0-9_]+$', collection):
        return _error_result('invalid collection name: {}'.format(collection))
    entry = index.get(collection)
    if entry is None:
        return _error_result('collection not found: {}'.format(collection))

    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    if not os.path.isdir(playbooks_dir):
        return _text_result('collection {} provides no playbooks'
                            .format(collection))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = ['# Playbooks in {} ({})'.format(
            collection, entry['version'] or 'unknown version'), '']
    total = 0
    for relpath in _list_playbooks(playbooks_dir):
        if name_filter and name_filter not in relpath.lower():
            continue
        line = '- `{}/{}`'.format(collection, _target_form(relpath))
        summary = _playbook_summary(os.path.join(playbooks_dir, relpath))
        if summary:
            line += ' — {}'.format(summary)
        lines.append(line)
        total += 1
    if not total:
        return _text_result('collection {} provides no playbooks'
                            .format(collection))
    return _text_result('\n'.join(lines))


def _tool_collection_playbook_info(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return _error_result(str(errmsg))
    collection = str(arguments.get('collection') or '')
    if not re.match(r'^[a-z0-9_]+\.[a-z0-9_]+$', collection):
        return _error_result('invalid collection name: {}'.format(collection))
    entry = index.get(collection)
    if entry is None:
        return _error_result('collection not found: {}'.format(collection))

    name = str(arguments.get('playbook') or '')
    if not name.endswith(('.yml', '.yaml')):
        name += '.yml'
    playbooks_dir = os.path.join(entry['path'], 'playbooks')
    try:
        path = confine_under(playbooks_dir, name)
    except ValueError:
        return _error_result('playbook not found or invalid: {}'.format(
                arguments.get('playbook')))
    if not os.path.isfile(path):
        return _error_result('playbook not found: {}'.format(
                arguments.get('playbook')))
    try:
        with open(path) as fp:
            plays, roles, imports, groups = _parse_playbook(fp.read())
    except (OSError, ValueError) as errmsg:
        return _error_result(str(errmsg))
    target = '{}/{}'.format(collection, _target_form(name))
    return _text_result(_render_playbook_info(
            '{}/{}'.format(collection, name), target, plays, roles,
            imports, groups))


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

    variables, partial = _role_default_values(_load_role_defaults(path))

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
    return _text_result('\n'.join(lines))


def _tool_role_variables(roles_path, arguments):
    resolved = _resolve_role(roles_path, arguments.get('name'))
    if resolved is None:
        return _error_result('role not found or invalid: {}'
                             .format(arguments.get('name')))
    name, path = resolved

    sources = _load_role_defaults(path)
    if not sources:
        return _error_result('role debops.{} has no defaults'.format(name))

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
        for var_name, description, value_text in _extract_envvar_docs(text):
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
        return _text_result('no matching variables in debops.{} '
                            'defaults'.format(name))

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return _error_result('invalid detail level: {}'.format(detail))

    header = '# debops.{} default variables ({})'.format(name,
                                                         len(entries))
    if detail == 'names':
        return _text_result('\n'.join(
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
                default = _dump_default(values[var_name], flow=True)
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
        return _text_result('\n'.join(lines))

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
                default = _dump_default(values[var_name])
            elif value_text:
                default = value_text
            if default:
                lines.extend(['```yaml', default, '```', ''])
    return _text_result('\n'.join(lines))


def _tool_collection_roles(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return _error_result(str(errmsg))

    name_filter = str(arguments.get('filter') or '').lower()
    lines = []
    total = 0
    for fqcn in sorted(index):
        if name_filter and name_filter not in fqcn.lower():
            continue
        roles = _list_collection_roles(index[fqcn]['path'])
        if not roles:
            continue
        total += len(roles)
        version = index[fqcn]['version']
        lines.append('## {}{}'.format(
                fqcn, ' ({})'.format(version) if version else ''))
        lines.extend('- {}'.format(role) for role in roles)
        lines.append('')
    if not lines:
        return _text_result('no collections with roles found')
    header = '# Collections with roles visible to Ansible ({})'.format(
            total)
    return _text_result('\n'.join([header, ''] + lines))


def _tool_collection_role_info(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return _error_result(str(errmsg))
    resolved = _resolve_collection_role(index, arguments.get('role'))
    if resolved is None:
        return _error_result('role not found or invalid: {}'
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

    specs = _load_argument_specs(role_path)
    values, partial = _role_default_values(_load_role_defaults(role_path))

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
    return _text_result('\n'.join(lines))


def _render_variable_entries(fqcn_role, entries, detail):
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
                default = _dump_default(info['default'], flow=True)
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
            lines.extend(['```yaml', _dump_default(info['default']),
                          '```', ''])
    return '\n'.join(lines)


def _tool_collection_role_variables(index_provider, arguments):
    try:
        index = index_provider()
    except RuntimeError as errmsg:
        return _error_result(str(errmsg))
    resolved = _resolve_collection_role(index, arguments.get('role'))
    if resolved is None:
        return _error_result('role not found or invalid: {}'
                             .format(arguments.get('role')))
    role, role_path = resolved
    fqcn_role = arguments.get('role')

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return _error_result('invalid detail level: {}'.format(detail))

    entries = []
    seen = set()

    # Structured argument specs are authoritative when present
    specs = _load_argument_specs(role_path)
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
    sources = _load_role_defaults(role_path)
    values, _partial = _role_default_values(sources)
    for _source, text in sources:
        for var_name, description in _extract_plain_docs(text):
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
        return _text_result('no matching variables in the {} role'
                            .format(fqcn_role))
    return _text_result(_render_variable_entries(fqcn_role, entries,
                                                 detail))


def register_project_tools(server, roles_path=COLLECTION_ROLES,
                           playbooks_path=COLLECTION_PLAYBOOKS,
                           collection_index_provider=None):
    """Register the built-in tools, honoring the server policy."""
    project = server.project
    policy = server.policy

    if collection_index_provider is None:
        cache = {'timestamp': 0.0, 'index': None}

        def collection_index_provider():
            if (cache['index'] is None
                    or time.time() - cache['timestamp']
                    > COLLECTION_INDEX_TTL):
                cache['index'] = _index_from_galaxy(
                        _galaxy_collection_list(project))
                cache['timestamp'] = time.time()
            return cache['index']

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
         'to configure a role through the Ansible inventory. Survey a role '
         'with the "brief" or "names" detail level, then use "filter" '
         'with the "full" detail level to read the complete documentation '
         'of specific variables.',
         {'type': 'object',
          'properties': {'name': {'type': 'string',
                                  'description': 'Role name, with or '
                                  'without the "debops." prefix'},
                         'filter': {'type': 'string',
                                    'description': 'Only include variables '
                                    'whose name contains this string'},
                         'detail': {'type': 'string',
                                    'enum': ['names', 'brief', 'full'],
                                    'description': 'Level of detail: '
                                    '"names" lists variable names only, '
                                    '"brief" (default) gives a one-line '
                                    'summary and a truncated default per '
                                    'variable, "full" gives the complete '
                                    'documentation'}},
          'required': ['name']},
         lambda arguments: _tool_role_variables(roles_path, arguments)),
        ('playbooks',
         'List the playbook targets which "run" and "check" can execute '
         'in this project: the project\'s own playbooks, the bundled '
         'DebOps collection playbooks, and playbook sets from the view '
         'configuration.',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose path contains '
                                    'this string'}}},
         lambda arguments: _tool_playbooks(project, policy, playbooks_path,
                                           arguments)),
        ('playbook_info',
         'Describe a project or bundled DebOps playbook: its plays, the '
         'inventory groups it targets, the roles it applies, and the '
         'playbooks it imports. Use this to learn what a playbook does '
         'before running it.',
         {'type': 'object',
          'properties': {'playbook': {'type': 'string',
                                      'description': 'Playbook target, '
                                      'e.g. "service/nginx" or "site"'}},
          'required': ['playbook']},
         lambda arguments: _tool_playbook_info(project, policy,
                                               playbooks_path, arguments)),
        ('collection_playbooks',
         'List the playbooks provided by an Ansible Collection visible '
         'to this project.',
         {'type': 'object',
          'properties': {'collection': {'type': 'string',
                                        'description': 'Collection, e.g. '
                                        '"community.sops"'},
                         'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'playbooks whose path contains '
                                    'this string'}},
          'required': ['collection']},
         lambda arguments: _tool_collection_playbooks(
             collection_index_provider, arguments)),
        ('collection_playbook_info',
         'Describe a playbook from an Ansible Collection: its plays, '
         'inventory groups, roles and imported playbooks.',
         {'type': 'object',
          'properties': {'collection': {'type': 'string',
                                        'description': 'Collection, e.g. '
                                        '"community.sops"'},
                         'playbook': {'type': 'string',
                                      'description': 'Playbook name '
                                      'inside the collection, e.g. '
                                      '"install"'}},
          'required': ['collection', 'playbook']},
         lambda arguments: _tool_collection_playbook_info(
             collection_index_provider, arguments)),
        ('collection_roles',
         'List the Ansible Collections visible to this project and the '
         'roles they contain. Use this to find roles outside of the '
         'bundled DebOps collection.',
         {'type': 'object',
          'properties': {'filter': {'type': 'string',
                                    'description': 'Only include '
                                    'collections whose name contains '
                                    'this string'}}},
         lambda arguments: _tool_collection_roles(
             collection_index_provider, arguments)),
        ('collection_role_info',
         'Describe a role from any Ansible Collection visible to this '
         'project: purpose, argument specs, provided parts, and where it '
         'was resolved from.',
         {'type': 'object',
          'properties': {'role': {'type': 'string',
                                  'description': 'Fully qualified role '
                                  'name, e.g. "grafana.grafana.grafana"'}},
          'required': ['role']},
         lambda arguments: _tool_collection_role_info(
             collection_index_provider, arguments)),
        ('collection_role_variables',
         'List the variables of a role from any Ansible Collection '
         'visible to this project, from its argument specs when present '
         'or its defaults otherwise. Survey with the "brief" or "names" '
         'detail level, then use "filter" with "full" for details.',
         {'type': 'object',
          'properties': {'role': {'type': 'string',
                                  'description': 'Fully qualified role '
                                  'name, e.g. "grafana.grafana.grafana"'},
                         'filter': {'type': 'string',
                                    'description': 'Only include variables '
                                    'whose name contains this string'},
                         'detail': {'type': 'string',
                                    'enum': ['names', 'brief', 'full'],
                                    'description': 'Level of detail: '
                                    '"names" lists variable names only, '
                                    '"brief" (default) gives a one-line '
                                    'summary and a truncated default per '
                                    'variable, "full" gives the complete '
                                    'documentation'}},
          'required': ['role']},
         lambda arguments: _tool_collection_role_variables(
             collection_index_provider, arguments)),
    ]

    for name, description, schema, handler in tools:
        if policy is None or policy.is_tool_allowed(name):
            server.register_tool(name, description, schema, handler)
