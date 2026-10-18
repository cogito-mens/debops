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

import fnmatch
import logging
import os
import re
import stat
import subprocess
import tempfile

from ..policy import READ_DENY_COMPONENTS, confine_under
from .common import (dump_default, error_result, exclusive_project,
                     shared_project, text_result)

logger = logging.getLogger(__name__)

# Files larger than this are not read into a tool result
READ_SIZE_LIMIT = 256 * 1024

# Contents larger than this are refused by the write tools
WRITE_SIZE_LIMIT = 1024 * 1024

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


def _authorized_write_path(policy, relative):
    """Resolve a view-relative write path, enforcing the write policy.

    Returns (absolute path, view-relative path). Raises ValueError with
    the refusal reason.
    """
    if not relative:
        raise ValueError('the "path" argument is required')
    try:
        path = confine_under(policy.view_root, relative)
    except ValueError:
        raise ValueError('path escapes the infrastructure view: '
                         + relative)
    canonical_relative = os.path.relpath(
            path, os.path.realpath(policy.view_root))
    # The deny list is unconditional, so that a misconfigured
    # 'write_allow' cannot open the secrets or the git metadata
    for part in canonical_relative.split('/'):
        if part in READ_DENY_COMPONENTS:
            raise ValueError('path is denied by policy: ' + relative)
    if not any(fnmatch.fnmatch(canonical_relative, pattern)
               for pattern in policy.write_allow):
        raise ValueError('path is not writable by the project policy '
                         '(project.mcp.write_allow): ' + relative)
    return path, canonical_relative


def _is_git_tracked_project(project):
    """Whether the project directory is git-tracked (work tree)."""
    return os.path.lexists(os.path.join(project.path, '.git'))


def tool_view_write(project, policy, arguments):
    relative = str(arguments.get('path') or '').strip('/')
    content = arguments.get('content')
    if not isinstance(content, str):
        return error_result('the "content" argument is required')
    size = len(content.encode('utf-8'))
    if size > WRITE_SIZE_LIMIT:
        return error_result('content is larger than {} bytes: {}'
                            .format(WRITE_SIZE_LIMIT, relative))
    mode = None
    if arguments.get('mode') is not None:
        raw_mode = str(arguments['mode'])
        if not re.fullmatch(r'0[0-7]{3}', raw_mode):
            return error_result('invalid mode: {!r} (an octal string '
                                'like "0755", permission bits only)'
                                .format(raw_mode))
        mode = int(raw_mode, 8)

    # Writes are recoverable and attributable only with git
    if not _is_git_tracked_project(project):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')

    try:
        path, view_relative = _authorized_write_path(policy, relative)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    with exclusive_project(project, policy,
                           note='view_write ' + view_relative):
        if os.path.lexists(path) and not os.path.isfile(path):
            return error_result('not a regular file: ' + view_relative)
        old_content = None
        old_mode = None
        if os.path.isfile(path):
            with open(path, 'rb') as fp:
                try:
                    old_content = fp.read().decode('utf-8')
                except UnicodeDecodeError:
                    return error_result('not a UTF-8 text file: '
                                        + view_relative)
            old_mode = stat.S_IMODE(os.stat(path).st_mode)

        if old_content == content:
            if mode is None or mode == old_mode:
                return text_result('unchanged: {} ({} bytes)'.format(
                        view_relative, size))
            os.chmod(path, mode)
            outcome = 'mode changed'
        else:
            # Atomic write: a temporary file in the same directory,
            # renamed
            directory = os.path.dirname(path)
            try:
                os.makedirs(directory, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=directory,
                                           prefix='.view-write-')
                done = False
                try:
                    with os.fdopen(fd, 'w') as fp:
                        fp.write(content)
                    if mode is not None:
                        os.chmod(tmp, mode)
                    elif old_mode is not None:
                        # The rename would otherwise reset the
                        # permissions
                        os.chmod(tmp, old_mode)
                    else:
                        os.chmod(tmp, 0o644)
                    os.replace(tmp, path)
                    done = True
                finally:
                    if not done:
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
            except OSError as errmsg:
                return error_result('cannot write {}: {}'.format(
                        view_relative, errmsg))
            outcome = 'overwritten' if old_content is not None \
                else 'created'

        # The commit is part of the tool's contract: an explicit,
        # attributable record of the change
        project.commit(message='DebOps MCP: view_write ' + view_relative)
        return text_result('{}: {} ({} bytes, mode {:04o})'.format(
                outcome, view_relative, size,
                stat.S_IMODE(os.stat(path).st_mode)))


def tool_view_delete(project, policy, arguments):
    relative = str(arguments.get('path') or '').strip('/')

    # Deletions are recoverable and attributable only with git
    if not _is_git_tracked_project(project):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')

    try:
        path, view_relative = _authorized_write_path(policy, relative)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    with exclusive_project(project, policy,
                           note='view_delete ' + view_relative):
        if not os.path.lexists(path):
            return error_result('file not found: ' + view_relative)
        if not os.path.isfile(path):
            return error_result('not a regular file: ' + view_relative)
        try:
            os.unlink(path)
        except OSError as errmsg:
            return error_result('cannot delete {}: {}'.format(
                    view_relative, errmsg))
        # The commit is part of the tool's contract, as for view_write
        project.commit(message='DebOps MCP: view_delete '
                       + view_relative)
        return text_result('deleted: {}'.format(view_relative))


def _inventory_spec_report(result, inventory_path, dry_run):
    """Render the result of an inventory spec application as text."""
    def rel(paths):
        return [os.path.relpath(path, inventory_path)
                for path in paths]

    lines = []
    if dry_run:
        lines.append('# Inventory specification (dry run, '
                     'nothing was written)')
        if result['planned']:
            lines.append('')
            lines.append('## Planned writes ({})'.format(
                    len(result['planned'])))
            lines.extend('- {}'.format(path)
                         for path in rel(result['planned']))
        if result['planned_removed']:
            lines.append('')
            lines.append('## Planned removals ({})'.format(
                    len(result['planned_removed'])))
            lines.extend('- {}'.format(path)
                         for path in rel(result['planned_removed']))
    else:
        lines.append('# Inventory specification applied')
        for title, key in (('Written', 'written'), ('Removed', 'removed'),
                           ('Skipped (already present)', 'skipped'),
                           ('Kept', 'kept')):
            if result[key]:
                lines.append('')
                lines.append('## {} ({})'.format(title,
                                                 len(result[key])))
                lines.extend('- {}'.format(path) for path in rel(
                        result[key]))
    if len(lines) == 1:
        lines.append('')
        lines.append('No changes.')
    return '\n'.join(lines)


def tool_inventory_spec(project, policy, arguments):
    """Apply an inventory specification to the view's inventory."""
    # Imported here so that the read tools in this module do not pull
    # the inventory machinery in
    from ...ansible.inventory import AnsibleInventory
    from ...exceptions import InventorySpecError
    from ...inventoryspec import (CONTENT_RENDERED_VERSIONS,
                                  PATH_RENDERED_VERSIONS, merge_specs,
                                  parse_spec, render_keep, render_spec)

    document = arguments.get('spec')
    if not isinstance(document, str) or not document.strip():
        return error_result('the "spec" argument is required')
    dry_run = bool(arguments.get('dry_run'))
    overwrite = bool(arguments.get('overwrite'))

    try:
        spec_policy = project._inventory_spec_config()
    except (AttributeError, ValueError) as errmsg:
        return error_result(str(errmsg))
    if not spec_policy['enabled']:
        return error_result('inventory specifications are disabled in '
                            'this project (project.inventory_spec.enabled '
                            'is false)')

    inventory = AnsibleInventory(project, name=project.view)

    source = 'tool input'
    try:
        spec = parse_spec(document, source)
    except InventorySpecError as errmsg:
        return error_result(str(errmsg))
    if spec['version'] > spec_policy['max_version']:
        return error_result(
                'the document declares inventory specification version '
                '{}, but this project allows at most version {} (the '
                'project.inventory_spec.version option)'.format(
                    spec['version'], spec_policy['max_version']))

    # Render before merging, so that the document's own version decides
    # which of its paths and contents are templates
    if spec['version'] in PATH_RENDERED_VERSIONS:
        context = project._spec_render_context()
        try:
            spec = {'version': spec['version'],
                    'files': render_spec(
                        spec['files'], context, source,
                        version=spec['version'],
                        allow_io=spec_policy['allow_io'],
                        include_base=inventory.path,
                        render_contents=(spec['version']
                                         in CONTENT_RENDERED_VERSIONS)),
                    'keep': render_keep(spec['keep'], context, source,
                                        version=spec['version'])}
        except InventorySpecError as errmsg:
            return error_result(str(errmsg))
    files, keep, notices = merge_specs([(source, spec)])
    for notice in notices:
        logger.notice('%s', notice)

    if dry_run:
        # A preview reads the inventory; it changes nothing. The
        # overwrite flag is passed so the preview matches the real run
        with shared_project(project, policy):
            result = inventory.apply_spec(
                    files, pre_existing=inventory.existing_paths(),
                    overwrite=overwrite, dry_run=True, keep=keep)
        return text_result(_inventory_spec_report(
                result, inventory.path, dry_run=True))

    # Writes are recoverable and attributable only with git
    if not _is_git_tracked_project(project):
        return error_result('the project directory is not a git '
                            'repository; initialize it ("debops project '
                            'init") so that changes can be committed')

    with exclusive_project(project, policy, note='inventory_spec'):
        # Everything present is user content: without 'overwrite' the
        # specification may only add paths, not modify or remove them
        result = inventory.apply_spec(
                files, pre_existing=inventory.existing_paths(),
                overwrite=overwrite, keep=keep)
        if result['written'] or result['removed']:
            # The commit is part of the tool's contract, as for
            # view_write
            project.commit(message='DebOps MCP: inventory_spec')
        return text_result(_inventory_spec_report(
                result, inventory.path, dry_run=False))
