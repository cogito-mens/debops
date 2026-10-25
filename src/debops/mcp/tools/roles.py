# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tools which describe the roles of the bundled DebOps Collection."""

import logging
import os
import re
import yaml

from ...constants import DEBOPS_PACKAGE_DATA
from ..policy import confine_under
from .common import (document_result, dump_default, error_result,
                     FOLD_MARKER, parse_output_format, render_task,
                     task_module, text_result)

logger = logging.getLogger(__name__)

# Roles shipped in the debops.debops Ansible Collection
COLLECTION_ROLES = os.path.join(
        DEBOPS_PACKAGE_DATA, 'ansible', 'collections',
        'ansible_collections', 'debops', 'debops', 'roles')

# Role directory names are restricted, which also prevents path traversal
ROLE_NAME_PATTERN = re.compile(r'^[a-z0-9_]+$')

# A fully qualified role name: 'namespace.collection.role'
ROLE_FQCN_PATTERN = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$')

# Role documentation lives on Read the Docs
DOCS_BASE_URL = 'https://docs.debops.org/en'


def _docs_version_slug(version):
    """The docs.debops.org version slug for a DebOps version.

    Development and local builds get 'master'; a release build gets
    'stable-<major>.<minor>'.
    """
    version = str(version or '')
    if '.dev' in version or '+' in version:
        return 'master'
    parts = version.split('.')
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return 'stable-{}.{}'.format(parts[0], parts[1])
    return 'master'


def role_docs_url(role_name, version, page=None):
    """The documentation URL of a bundled DebOps role."""
    slug = _docs_version_slug(version)
    base = '{}/{}/ansible/roles/{}'.format(DOCS_BASE_URL, slug, role_name)
    if page:
        return '{}/{}.html'.format(base, page)
    return '{}/index.html#debops-{}'.format(
            base, role_name.replace('_', '-'))


def _is_bundled_role(path, bundled_path):
    """Whether the role was resolved from the bundled collection."""
    if not bundled_path:
        return False
    real = os.path.realpath(bundled_path)
    return os.path.realpath(path).startswith(real + os.sep)


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


def resolve_role(roles_paths, name, index_provider=None):
    """Resolve a role name to (name, path), or None if unknown.

    Short names ('nginx', 'debops.nginx') resolve over the given
    directories, first match wins, so earlier paths shadow later ones.
    A fully qualified name ('namespace.collection.role') resolves through
    the collection index; the content policy applies there.
    """
    name = str(name or '')
    if name.count('.') >= 2:
        if index_provider is None:
            return None
        if not ROLE_FQCN_PATTERN.match(name):
            return None
        namespace, collection, role = name.split('.')
        entry = index_provider().get('{}.{}'.format(namespace, collection))
        if entry is None:
            return None
        role_path = os.path.join(entry['path'], 'roles', role)
        if not os.path.isdir(role_path):
            return None
        return role, role_path

    # Accept both 'nginx' and the fully qualified 'debops.nginx'
    name = name.split('.')[-1]
    if not ROLE_NAME_PATTERN.match(name):
        return None
    for roles_path in roles_paths:
        path = os.path.join(roles_path, name)
        if os.path.isdir(path):
            return name, path


def resolve_role_or_error(roles_paths, arguments, index_provider=None):
    """Resolve the 'name' argument of a role tool to (name, path).

    Raises ValueError with the user-facing reason when the role cannot
    be resolved; the tools turn that into their error result. The one
    preamble every role tool shares.
    """
    try:
        resolved = resolve_role(roles_paths, arguments.get('name'),
                                index_provider)
    except RuntimeError as errmsg:
        raise ValueError(str(errmsg))
    if resolved is None:
        raise ValueError('role not found or invalid: {}'.format(
                arguments.get('name')))
    return resolved
    return None


def load_role_sources(role_path, subdir):
    """Load a role's data sources in the order Ansible reads them.

    A role defines its data in '<subdir>/main.yml', in a
    '<subdir>/main/' directory whose files are read in alphabetical
    order, or both. Returns an ordered list of (source_name, text)
    pairs.
    """
    sources = []
    main_file = os.path.join(role_path, subdir, 'main.yml')
    if os.path.isfile(main_file):
        with open(main_file) as fp:
            sources.append(('main.yml', fp.read()))
    main_dir = os.path.join(role_path, subdir, 'main')
    if os.path.isdir(main_dir):
        for filename in sorted(os.listdir(main_dir)):
            if (filename.endswith(('.yml', '.yaml'))
                    and not filename.startswith('.')):
                with open(os.path.join(main_dir, filename)) as fp:
                    sources.append((filename, fp.read()))
    return sources


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


def merge_role_values(sources):
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


def _count_tree_files(directory):
    """The number of non-hidden files in a directory tree."""
    count = 0
    for _base, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        count += len([f for f in files if not f.startswith('.')])
    return count


def _count_tasks(directory):
    """The number of tasks and task files in a tasks/handlers dir."""
    total = 0
    files = 0
    if not os.path.isdir(directory):
        return 0, 0
    for entry in sorted(os.listdir(directory)):
        if (entry.endswith(('.yml', '.yaml'))
                and not entry.startswith('.')
                and os.path.isfile(os.path.join(directory, entry))):
            files += 1
            total += len(_load_tasks(os.path.join(directory, entry)))
    return total, files


def tool_role_info(roles_paths, arguments, index_provider=None,
                   bundled_path=None, docs_version=None):
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    description = ''
    tags = []
    dependencies = []
    author = ''
    license_name = ''
    min_ansible = ''
    platforms = []
    meta_file = os.path.join(path, 'meta', 'main.yml')
    if os.path.isfile(meta_file):
        try:
            with open(meta_file) as fp:
                meta = yaml.safe_load(fp) or {}
            galaxy_info = meta.get('galaxy_info', {})
            description = galaxy_info.get('description', '')
            tags = galaxy_info.get('galaxy_tags', [])
            author = galaxy_info.get('author', '')
            license_name = galaxy_info.get('license', '')
            min_ansible = str(galaxy_info.get('min_ansible_version', ''))
            for platform in galaxy_info.get('platforms', []) or []:
                platforms.append('{} ({})'.format(
                        platform.get('name', ''),
                        ', '.join(str(v)
                                  for v in platform.get('versions', []))))
            dependencies = meta.get('dependencies', [])
        except yaml.YAMLError as errmsg:
            logger.warning('Cannot parse %s: %s', meta_file, errmsg)

    defaults, defaults_partial = merge_role_values(
            load_role_sources(path, 'defaults'))
    variables, vars_partial = merge_role_values(
            load_role_sources(path, 'vars'))
    specs = load_argument_specs(path)
    task_count, task_files = _count_tasks(os.path.join(path, 'tasks'))
    handler_count, handler_files = _count_tasks(os.path.join(path,
                                                             'handlers'))
    templates = _count_tree_files(os.path.join(path, 'templates'))
    files = _count_tree_files(os.path.join(path, 'files'))

    def _counted(label, count, partial=False):
        if not count and not partial:
            return '- {}: none'.format(label)
        if not count:
            return '- {}: unparseable'.format(label)
        return '- {}: {} variable{}'.format(
                label, count, '' if count == 1 else 's')

    def _across(count, files):
        if not count:
            return 'none'
        return '{} across {} file{}'.format(
                count, files, '' if files == 1 else 's')

    lines = ['# debops.{}'.format(name), '']
    if description:
        lines.extend([description, ''])
    lines.append('- resolved at: {}'.format(path))
    if _is_bundled_role(path, bundled_path) and docs_version:
        lines.append('- documentation: {}'.format(
                role_docs_url(name, docs_version)))
    lines.append(_counted('defaults', len(defaults), defaults_partial))
    lines.append(_counted('vars', len(variables), vars_partial))
    if specs:
        options = sum(len((spec or {}).get('options') or {})
                      for spec in specs.values())
        lines.append('- argument specs: {} ({} option{})'.format(
                ', '.join(sorted(specs)), options,
                '' if options == 1 else 's'))
    else:
        lines.append('- argument specs: none')
    lines.append('- tasks: {}'.format(_across(task_count, task_files)))
    lines.append('- handlers: {}'.format(
            _across(handler_count, handler_files)))
    lines.append('- templates: {}'.format(
            '{} file{}'.format(templates, '' if templates == 1 else 's')
            if templates else 'none'))
    lines.append('- files: {}'.format(
            '{} file{}'.format(files, '' if files == 1 else 's')
            if files else 'none'))
    lines.append('- README: {}'.format(
            'yes' if os.path.isfile(os.path.join(path, 'README.md'))
            else 'no'))
    lines.append('- COPYRIGHT: {}'.format(
            'yes' if os.path.isfile(os.path.join(path, 'COPYRIGHT'))
            else 'no'))
    if author:
        lines.append('- author: {}'.format(author))
    if license_name:
        lines.append('- license: {}'.format(license_name))
    if min_ansible:
        lines.append('- min ansible version: {}'.format(min_ansible))
    if platforms:
        lines.append('- platforms: {}'.format('; '.join(platforms)))
    if tags:
        lines.append('- galaxy tags: {}'.format(', '.join(tags)))
    if dependencies:
        lines.append('- dependencies: {}'.format(', '.join(
                dep if isinstance(dep, str) else dep.get('role', '?')
                for dep in dependencies)))
    structured = {
        'name': name,
        'path': path,
        'description': description,
        'author': author,
        'license': license_name,
        'minAnsible': min_ansible,
        'platforms': platforms,
        'tags': tags,
        'dependencies': [dep if isinstance(dep, str)
                         else dep.get('role', '?')
                         for dep in dependencies],
        'counts': {'defaults': len(defaults),
                   'variables': len(variables),
                   'tasks': task_count,
                   'handlers': handler_count,
                   'templates': templates,
                   'files': files},
        'readme': os.path.isfile(os.path.join(path, 'README.md')),
        'copyright': os.path.isfile(os.path.join(path, 'COPYRIGHT')),
    }
    if _is_bundled_role(path, bundled_path) and docs_version:
        structured['docsUrl'] = role_docs_url(name, docs_version)
    return text_result('\n'.join(lines), structured=structured)


def _load_tasks(tasks_file):
    """Parse a tasks file, returning a list of task dicts."""
    with open(tasks_file) as fp:
        tasks = yaml.safe_load(fp)
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def _tool_role_tasklist(roles_paths, arguments, subdir,
                        index_provider=None):
    """Shared implementation of role.tasks and role.handlers."""
    kind = 'handler' if subdir == 'handlers' else 'task'
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    try:
        output_format = parse_output_format(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    tasks_dir = os.path.join(path, subdir)
    if not os.path.isdir(tasks_dir):
        return error_result('role {} has no {} directory'.format(
                name, subdir))

    task_file = str(arguments.get('file') or '')
    if not task_file:
        entries = [entry for entry in sorted(os.listdir(tasks_dir))
                   if entry.endswith(('.yml', '.yaml'))
                   and not entry.startswith('.')]
        # The overview earns its keep with several task files; a role
        # with exactly one answers with its brief listing directly
        if len(entries) == 1:
            task_file = entries[0]
    if not task_file:
        # Overview: the task files and their task counts
        lines = ['# debops.{} {} files'.format(name, kind), '']
        counts = {}
        total = 0
        for entry in entries:
            count = len(_load_tasks(os.path.join(tasks_dir, entry)))
            counts[os.path.splitext(entry)[0]] = count
            lines.append('- `{}` — {} {}{}'.format(
                    os.path.splitext(entry)[0], count, kind,
                    '' if count == 1 else 's'))
            total += count
        lines.insert(1, '({} {}s)'.format(total, kind))
        structured = {'role': 'debops.{}'.format(name),
                      'files': counts,
                      'tasks': total}
        if output_format != 'text':
            result = document_result(counts, output_format)
            if not result.get('isError'):
                result['structuredContent'] = structured
            return result
        return text_result('\n'.join(lines) + '\n',
                           structured=structured)

    if not task_file.endswith(('.yml', '.yaml')):
        task_file += '.yml'
    try:
        target = confine_under(tasks_dir, task_file)
    except ValueError:
        return error_result('invalid {} file: {}'.format(
                kind, arguments.get('file')))
    if not os.path.isfile(target):
        return error_result('{} file not found: {}'.format(kind,
                                                           task_file))
    tasks = _load_tasks(target)

    name_filter = str(arguments.get('filter') or '').lower()
    if output_format != 'text':
        return document_result(
                [task for task in tasks
                 if not name_filter
                 or name_filter in str(task.get('name', '')).lower()],
                output_format)

    detail = arguments.get('detail', 'brief')

    lines = ['# debops.{} {}/{} ({} {}{})'.format(
            name, subdir, task_file, len(tasks), kind,
            '' if len(tasks) == 1 else 's'), '']
    shown = 0
    for task in tasks:
        task_name = str(task.get('name', '(unnamed)'))
        if name_filter and name_filter not in task_name.lower():
            continue
        lines.extend(render_task(task, detail))
        shown += 1
    if name_filter:
        lines.insert(1, '({} matching)'.format(shown))
    # The structured form: the tasks' summaries — the name, the
    # module, the tags — parsed from the YAML
    structured = {'role': 'debops.{}'.format(name),
                  'file': task_file,
                  'tasks': [{'name': str(task.get('name',
                                                 '(unnamed)')),
                             'module': task_module(task),
                             'tags': task.get('tags') or []}
                            for task in tasks
                            if not name_filter
                            or name_filter in str(
                                    task.get('name', '')).lower()]}
    return text_result('\n'.join(lines) + '\n',
                       structured=structured)


def tool_role_tasks(roles_paths, arguments, index_provider=None):
    return _tool_role_tasklist(roles_paths, arguments, 'tasks',
                               index_provider=index_provider)


def tool_role_handlers(roles_paths, arguments, index_provider=None):
    return _tool_role_tasklist(roles_paths, arguments, 'handlers',
                               index_provider=index_provider)


def _tool_role_tree(roles_paths, arguments, subdir,
                    index_provider=None):
    """List or read files of a role's 'templates/' or 'files/' tree."""
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    tree_dir = os.path.join(path, subdir)
    if not os.path.isdir(tree_dir):
        return error_result('role {} has no {} directory'.format(name,
                                                                 subdir))

    relative = str(arguments.get('path') or '').strip('/')
    if not relative:
        lines = ['# debops.{} {}'.format(name, subdir), '']
        paths = []
        for base, dirs, files in os.walk(tree_dir):
            dirs[:] = sorted(directory for directory in dirs
                             if not directory.startswith('.'))
            for filename in sorted(files):
                if filename.startswith('.'):
                    continue
                entry = os.path.relpath(os.path.join(base, filename),
                                        tree_dir)
                lines.append('- {}'.format(entry))
                paths.append(entry)
        # The structured form of the listing
        structured = {'role': 'debops.{}'.format(name),
                      subdir: paths}
        if len(lines) == 2:
            return text_result('role {} has an empty {} directory'
                               .format(name, subdir),
                               structured=structured)
        return text_result('\n'.join(lines) + '\n',
                           structured=structured)

    try:
        target = confine_under(tree_dir, relative)
    except ValueError:
        return error_result('invalid path: ' + relative)
    if not os.path.isfile(target):
        return error_result('file not found: ' + relative)
    if os.path.getsize(target) > 256 * 1024:
        return error_result('file is larger than {} bytes: {}'.format(
                256 * 1024, relative))
    try:
        with open(target, 'rb') as fp:
            content = fp.read().decode('utf-8')
    except UnicodeDecodeError:
        return error_result('not a UTF-8 text file: ' + relative)
    if content and not content.endswith('\n'):
        content += '\n'
    return text_result('# {}\n\n```\n{}```'.format(relative, content),
                       structured={'role': 'debops.{}'.format(name),
                                   'path': relative,
                                   'content': content})


def tool_role_templates(roles_paths, arguments, index_provider=None):
    return _tool_role_tree(roles_paths, arguments, 'templates',
                           index_provider=index_provider)


def tool_role_files(roles_paths, arguments, index_provider=None):
    return _tool_role_tree(roles_paths, arguments, 'files',
                           index_provider=index_provider)


def argspec_entries(spec):
    """Build (name, info) entries from one argument spec entry point."""
    entries = []
    for opt_name, option in (spec.get('options') or {}).items():
        if not isinstance(option, dict):
            continue
        description = option.get('description', '')
        if isinstance(description, list):
            description = '\n'.join(str(item) for item in description)
        entries.append((opt_name, {
            'description': description,
            'type': option.get('type'),
            'required': option.get('required', False),
            'choices': option.get('choices'),
            'default': option.get('default'),
            'has_default': 'default' in option,
        }))
    return entries


def render_spec_entries(label, entries, detail):
    """Render (name, info) argument entries at the given detail level.

    Each entry's info is a dict with 'description', 'type', 'required',
    'choices', 'default' and 'has_default' keys, any of which may be
    missing or empty.
    """
    header = '# {} ({})'.format(label, len(entries))
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


def tool_role_argspec(roles_paths, arguments, index_provider=None):
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    # Display the requested name so external roles keep their prefix
    display = str(arguments.get('name') or '')
    if not display.count('.'):
        display = 'debops.' + name
    specs = load_argument_specs(path)
    if not specs:
        return error_result('role {} has no argument specs '
                            '(meta/argument_specs.yml)'.format(display))
    # The structured form is the parsed spec document itself
    structured = {'role': display, 'argumentSpecs': specs}

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))
    name_filter = str(arguments.get('filter') or '').lower()

    lines = []
    for entry_point in sorted(specs):
        entries = argspec_entries(specs[entry_point])
        if name_filter:
            entries = [entry for entry in entries
                       if name_filter in entry[0].lower()]
        if not entries:
            continue
        lines.append(render_spec_entries(
                '{} argument spec: {}'.format(display, entry_point),
                entries, detail))
        lines.append('')
    if not lines:
        return text_result('no matching options in the debops.{} '
                           'argument specs'.format(name),
                           structured=structured)
    return text_result('\n'.join(lines), structured=structured)


def tool_role_variables(roles_paths, arguments, index_provider=None):
    """The role's own variables from vars/ (higher precedence than
    defaults, not meant to be overridden from the inventory)."""
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    try:
        output_format = parse_output_format(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    sources = load_role_sources(path, 'vars')
    if not sources:
        return error_result('role debops.{} has no variables '
                            '(vars/main.yml)'.format(name))

    values, _partial = merge_role_values(sources)
    entries = []
    seen = set()
    for _source, text in sources:
        for var_name, description in extract_plain_docs(text):
            if var_name in seen:
                continue
            entries.append((var_name, description))
            seen.add(var_name)
    for var_name in values:
        if var_name not in seen:
            entries.append((var_name, ''))
            seen.add(var_name)

    name_filter = str(arguments.get('filter') or '').lower()
    if name_filter:
        entries = [entry for entry in entries
                   if name_filter in entry[0].lower()]

    # The structured form rides every answer: the YAML parse typed
    # the values, the comments the documentation
    structured = {'role': 'debops.{}'.format(name),
                  'vars': [{'name': var_name,
                            'value': values.get(var_name),
                            'documentation': description,
                            'documented': bool(description)}
                           for var_name, description in entries]}

    if output_format != 'text':
        document = {key: value for key, value in values.items()
                    if not name_filter or name_filter in key.lower()}
        result = document_result(document, output_format)
        if not result.get('isError'):
            result['structuredContent'] = structured
        return result

    if not entries:
        return text_result('no matching variables in debops.{} vars'
                           .format(name), structured=structured)

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))

    header = '# debops.{} variables ({})'.format(name, len(entries))
    if detail == 'names':
        return text_result('\n'.join([header, '']
                                     + [entry[0] for entry in entries]),
                           structured=structured)

    lines = [header, '']
    for var_name, description in entries:
        if detail == 'brief':
            summary = ''
            for description_line in description.splitlines():
                if description_line.strip():
                    summary = description_line.strip()
                    break
            default = ''
            if var_name in values:
                default = dump_default(values[var_name], flow=True)
                if len(default) > 100:
                    default = default[:97] + '…'
            line = '- **{}**'.format(var_name)
            if summary:
                line += ' — {}'.format(summary)
            if default:
                line += ' (default: `{}`)'.format(default)
            lines.append(line)
        else:
            lines.append('## {}'.format(var_name))
            lines.append('')
            if description:
                lines.extend([description, ''])
            if var_name in values:
                lines.extend(['```yaml', dump_default(values[var_name]),
                              '```', ''])
    return text_result('\n'.join(lines), structured=structured)


def tool_role_defaults(roles_paths, arguments, index_provider=None,
                       bundled_path=None, docs_version=None):
    try:
        name, path = resolve_role_or_error(roles_paths, arguments,
                                           index_provider)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    try:
        output_format = parse_output_format(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    sources = load_role_sources(path, 'defaults')
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

    # The structured form rides every answer: the entries with the
    # typed values from the YAML parse and the documentation from
    # the comment blocks the YAML parser would drop
    structured_entries = []
    for var_name, description, value_text, source in entries:
        structured_entries.append({
                'name': var_name,
                'value': values.get(var_name),
                'documentation': description,
                'source': source,
                'documented': bool(description)})
    structured = {'role': 'debops.{}'.format(name),
                  'defaults': structured_entries}

    if output_format != 'text':
        document = {key: value for key, value in values.items()
                    if not name_filter or name_filter in key.lower()}
        result = document_result(document, output_format)
        if not result.get('isError'):
            result['structuredContent'] = structured
        return result

    if not entries:
        return text_result('no matching variables in debops.{} '
                           'defaults'.format(name),
                           structured=structured)

    detail = arguments.get('detail', 'brief')
    if detail not in ('names', 'brief', 'full'):
        return error_result('invalid detail level: {}'.format(detail))

    header = '# debops.{} default variables ({})'.format(name,
                                                         len(entries))
    if detail == 'names':
        return text_result('\n'.join(
                [header, ''] + [entry[0] for entry in entries]),
                structured=structured)

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
        return text_result('\n'.join(lines), structured=structured)

    lines = [header]
    if len(sources) > 1:
        lines[0] += ' ({} files)'.format(len(sources))
    lines.append('')
    # Point at the detailed external documentation for bundled roles
    if _is_bundled_role(path, bundled_path) and docs_version:
        lines.append('Detailed documentation: {}'.format(
                role_docs_url(name, docs_version,
                              page='defaults-detailed')))
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
    return text_result('\n'.join(lines), structured=structured)
