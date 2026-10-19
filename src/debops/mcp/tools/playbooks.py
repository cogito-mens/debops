# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe project and bundled DebOps playbooks."""

import os
import re
import yaml

from ...constants import DEBOPS_PACKAGE_DATA
from ..policy import confine_under
from .common import error_result, render_task, text_result

# Playbooks shipped in the debops.debops Ansible Collection
COLLECTION_PLAYBOOKS = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections',
        'ansible_collections', 'debops', 'debops', 'playbooks')

# Subdirectories of a playbooks/ directory which hold supporting files
# (variable files, task files, ...) rather than runnable playbooks
PLAYBOOK_SUPPORT_DIRS = ('files', 'roles', 'tasks', 'templates', 'vars')


def parse_playbook(text):
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
            play_vars = entry.get('vars') or {}
            sections = {}
            for section in ('pre_tasks', 'tasks', 'post_tasks'):
                section_tasks = [task for task in entry.get(section) or []
                                 if isinstance(task, dict)]
                sections[section] = section_tasks
            plays.append({
                'name': str(entry.get('name', '')),
                'hosts': hosts,
                'become': bool(entry.get('become', False)),
                'vars': sorted(play_vars.keys())
                if isinstance(play_vars, dict) else [],
                'sections': sections,
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


def list_playbooks(root):
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


def playbook_summary(path):
    """A one-line summary of a playbook: plays, host groups, imports.

    A multi-play playbook is named by its first play with a play count;
    a playbook of pure 'import_playbook' statements is described by its
    imports.
    """
    try:
        with open(path) as fp:
            plays, _roles, imports, groups = parse_playbook(fp.read())
    except (OSError, ValueError):
        return ''
    parts = []
    if plays:
        name = plays[0]['name']
        if len(plays) > 1:
            if name:
                parts.append('{} (+{} plays)'.format(name,
                                                     len(plays) - 1))
            else:
                parts.append('{} plays'.format(len(plays)))
        elif name:
            parts.append(name)
    if groups:
        parts.append('(hosts: {})'.format(', '.join(groups)))
    if not plays and imports:
        parts.append('imports {} playbook{}'.format(
                len(imports), '' if len(imports) == 1 else 's'))
    return ' '.join(parts)


def target_form(relative_path):
    """The name 'run'/'check' resolve: the path without the extension."""
    return os.path.splitext(relative_path)[0]


def project_playbook_dirs(project, policy):
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


# A fully qualified playbook name: 'namespace.collection.playbook'
# (playbook names may contain dashes)
PLAYBOOK_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_-]+$')

# The collection-prefixed path form: 'namespace.collection/path/to/pb'
PLAYBOOK_COLLECTION_PATH = re.compile(r'^([a-z0-9_]+\.[a-z0-9_]+)/(.*)$')


def resolve_playbook(project, policy, playbooks_path, name,
                     index_provider=None):
    """Resolve a playbook target to (path, target form).

    Explicit collection references win: the 'namespace.collection/path'
    form and the dotted fully qualified form resolve through the
    collection index (where the content policy applies). Otherwise the
    project playbook directories are searched first, then the bundled
    DebOps collection, mirroring the playbook runner; unlike the command
    line, direct filesystem paths outside these roots are not followed.
    """
    name = str(name or '')

    if index_provider is not None:
        collection_path = None
        relpath = None
        # The dotted form is used verbatim as the execution target
        target = name
        if PLAYBOOK_FQCN_PATTERN.match(name):
            namespace, collection, playbook = name.split('.')
            relpath = playbook
            entry = index_provider().get('{}.{}'.format(namespace,
                                                        collection))
            if entry is not None:
                collection_path = os.path.join(entry['path'], 'playbooks')
        else:
            match = PLAYBOOK_COLLECTION_PATH.match(name)
            if match:
                entry = index_provider().get(match.group(1))
                if entry is not None:
                    collection_path = os.path.join(entry['path'],
                                                   'playbooks')
                    relpath = match.group(2)
                    target = target_form(name)
        if collection_path is not None and relpath is not None:
            if not relpath.endswith(('.yml', '.yaml')):
                relpath += '.yml'
            try:
                candidate = confine_under(collection_path, relpath)
            except ValueError:
                return None
            if os.path.isfile(candidate):
                return candidate, target
            # An explicit collection reference does not fall through
            return None

    if not name.endswith(('.yml', '.yaml')):
        name += '.yml'
    try:
        for _label, directory in project_playbook_dirs(project, policy):
            candidate = confine_under(directory, name)
            if os.path.isfile(candidate):
                return candidate, target_form(name)
        if playbooks_path is not None:
            candidate = confine_under(playbooks_path, name)
            if os.path.isfile(candidate):
                return candidate, target_form(name)
    except ValueError:
        pass
    return None


def render_playbook_info(title, target, plays, roles, imports, groups,
                         detail='brief'):
    lines = ['# {}'.format(title), '',
             'Execution target: `{}`'.format(target), '']
    if plays:
        lines.extend(['## Plays ({})'.format(len(plays)), ''])
        for play in plays:
            line = '- {}'.format(play['name'] or '(unnamed)')
            if play['hosts']:
                line += ' — hosts: {}'.format(', '.join(play['hosts']))
            if play['become']:
                line += ' (become)'
            lines.append(line)
            if detail == 'full':
                if play['vars']:
                    lines.append('    vars: {}'.format(
                            ', '.join(play['vars'])))
                for section in ('pre_tasks', 'tasks', 'post_tasks'):
                    section_tasks = play['sections'].get(section, [])
                    if not section_tasks:
                        continue
                    lines.append('    {} ({}):'.format(
                            section, len(section_tasks)))
                    for task in section_tasks:
                        for task_line in render_task(task, detail):
                            lines.append('    ' + task_line)
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


def tool_playbooks(project, policy, playbooks_path, arguments):
    name_filter = str(arguments.get('filter') or '').lower()
    # The entries, flattened: each is (target, section, summary)
    entries = []

    # Project playbooks resolve first, mirroring the playbook runner
    for label, directory in project_playbook_dirs(project, policy):
        section = 'Project playbooks ({})'.format(label)
        for relpath in list_playbooks(directory):
            if name_filter and name_filter not in relpath.lower():
                continue
            entries.append((target_form(relpath), section,
                            playbook_summary(
                                os.path.join(directory, relpath))))

    # The bundled DebOps collection, grouped by subdirectory; absent
    # when the policy disables bundled content
    if playbooks_path is not None:
        grouped = {}
        for relpath in list_playbooks(playbooks_path):
            if name_filter and name_filter not in relpath.lower():
                continue
            group = os.path.dirname(relpath)
            grouped.setdefault(group, []).append(relpath)
        for group in sorted(grouped):
            section = 'DebOps collection' if not group \
                else 'DebOps collection: {}/'.format(group)
            for relpath in grouped[group]:
                entries.append((target_form(relpath), section,
                                playbook_summary(
                                    os.path.join(playbooks_path,
                                                 relpath))))

    # Playbook sets from the view configuration are also valid targets
    playbook_sets = (project.config.raw.get('views', {})
                     .get(project.view, {}).get('playbook_sets', {}))
    for name in sorted(playbook_sets):
        members = playbook_sets[name]
        entries.append((name, 'Playbook sets (view configuration)',
                        'expands to: {}'.format(', '.join(
                            str(member) for member in members))))

    if not entries:
        return text_result('no playbooks found', structured=[])

    structured = [{'target': target, 'section': section,
                   'summary': summary}
                  for target, section, summary in entries]

    lines = []
    section = None
    for target, section_title, summary in entries:
        if section_title != section:
            if section is not None:
                lines.append('')
            lines.append('## {}'.format(section_title))
            section = section_title
        line = '- `{}`'.format(target)
        if summary.startswith('expands to: '):
            line = '- `{}` → {}'.format(target,
                                        summary[len('expands to: '):])
        elif summary:
            line += ' — {}'.format(summary)
        lines.append(line)

    header = '# Playbook targets ({})'.format(len(entries))
    return text_result('\n'.join([header, ''] + lines),
                       structured=structured)


def tool_playbook_info(project, policy, playbooks_path, arguments,
                       index_provider=None):
    resolved = resolve_playbook(project, policy, playbooks_path,
                                arguments.get('playbook'), index_provider)
    if resolved is None:
        return error_result('playbook not found or invalid: {}'.format(
                arguments.get('playbook')))
    path, target = resolved
    detail = arguments.get('detail', 'brief')
    if detail not in ('brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))
    try:
        with open(path) as fp:
            plays, roles, imports, groups = parse_playbook(fp.read())
    except (OSError, ValueError) as errmsg:
        return error_result(str(errmsg))
    return text_result(render_playbook_info(target, target, plays, roles,
                                            imports, groups, detail))
