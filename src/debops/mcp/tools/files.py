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

from ..policy import confine_under, is_denied_component
from .common import (commit_or_report, dump_default, error_result,
                     exclusive_project, mcp_environ, require_git_tracked,
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
        return text_result(header + ' — none found', structured=[])
    return text_result('\n'.join([header, ''] +
                                 ['- {}'.format(line) for line in lines]),
                       structured=lines)


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


# Configuration keys whose segments name a secret value are never
# served: the configuration tree is not for secrets (the secret/
# directories and the MCP .env files exist for those), and a value
# referencing an environment variable inlines it at load time. The
# pattern is anchored so that paths ('vault_password_file') and
# toggles ('become_ask_pass') still serve
_SECRET_KEY_SEGMENT = re.compile(
        r'(?:^|_)(?:passwords?|passwd|passphrases?|secrets?|tokens?|'
        r'credentials?|api[_-]?keys?|private[_-]?keys?)$', re.IGNORECASE)


def _secret_named_key(key):
    """Whether a dotted configuration key names a secret value."""
    return any(_SECRET_KEY_SEGMENT.search(segment)
               for segment in key.split('.'))


def _withhold_secret_values(node):
    """Copy a configuration subtree, withholding the values of keys
    which name secrets (a subtree query would carry them otherwise)."""
    if isinstance(node, dict):
        return {key: ('<withheld>' if _SECRET_KEY_SEGMENT.search(
                        str(key))
                      else _withhold_secret_values(value))
                for key, value in node.items()}
    if isinstance(node, list):
        return [_withhold_secret_values(value) for value in node]
    return node


def tool_config_get(project, arguments):
    key = str(arguments.get('key') or '').strip('.')
    if not key:
        return error_result('the "key" argument is required')
    if _secret_named_key(key):
        return error_result('the key names a secret value; the '
                            'configuration tree is not served for '
                            'those (the secret/ directory and the MCP '
                            '.env files are the places for secrets)')
    node = project.config.raw
    for part in key.split('.'):
        if not isinstance(node, dict) or part not in node:
            return error_result('configuration key not found: ' + key)
        node = node[part]
    node = _withhold_secret_values(node)
    return text_result('{}:\n\n```yaml\n{}\n```'.format(
            key, dump_default(node)),
            structured={'key': key, 'value': node})


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

    env = mcp_environ(project)
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


def _authorized_write_path(project, policy, relative):
    """Resolve a write path, enforcing the write policy.

    Anchors: a leading '/' resolves at the project directory (spelled
    as 'project.read' shows them); '.debops/' and 'ansible/' paths
    resolve at the project directory too, so they reach the same place
    in legacy and modern projects; anything else resolves at the view
    root. Patterns follow the same anchoring: a pattern only ever
    applies to paths anchored the same way.

    Returns (absolute path, display path). Raises ValueError with the
    refusal reason.
    """
    if not relative:
        raise ValueError('the "path" argument is required')
    if relative.startswith('/'):
        root = project.path
        relative = relative.lstrip('/')
        patterns = [pattern.lstrip('/') for pattern in policy.write_allow
                    if pattern.startswith('/')]
        if not relative:
            raise ValueError('the "path" argument is required')
    elif relative.startswith(('.debops/', 'ansible/')):
        root = project.path
        patterns = [pattern for pattern in policy.write_allow
                    if pattern.startswith(('.debops/', 'ansible/'))]
    else:
        root = policy.view_root
        patterns = [pattern for pattern in policy.write_allow
                    if not pattern.startswith(('/', '.debops/',
                                               'ansible/'))]
    try:
        path = confine_under(root, relative)
    except ValueError:
        raise ValueError('path escapes its allowed directory: '
                         + relative)
    canonical_relative = os.path.relpath(
            path, os.path.realpath(root))
    # The deny list is unconditional, so that a misconfigured
    # 'write_allow' cannot open the secrets or the git metadata
    for part in canonical_relative.split('/'):
        if is_denied_component(part):
            raise ValueError('path is denied by policy: ' + relative)
    # The project machinery is never writable: under '.debops/' only the
    # MCP content directories exist for the write tools, so that no
    # allowlist can open the configuration or the hooks; and no anchor
    # opens 'ansible.cfg', which decides where Ansible loads code from
    if canonical_relative.startswith('.debops/'):
        parts = canonical_relative.split('/')
        writable = ['.debops/mcp/skills/', '.debops/mcp/prompts/']
        allowed = (len(parts) >= 4 and parts[1:3] in (['mcp', 'skills'],
                                                      ['mcp', 'prompts']))
        # Custom script tools are writable only when the policy enabled
        # them; a tool is a single file directly in the directory
        if policy.custom_tools:
            writable.append('.debops/mcp/tools/')
            if len(parts) == 4 and parts[1:3] == ['mcp', 'tools']:
                allowed = True
        if not allowed:
            raise ValueError('only the MCP content directories ({}) '
                             'are writable: {}'.format(
                                 ', '.join(writable), relative))
    if os.path.basename(canonical_relative) == 'ansible.cfg':
        raise ValueError('path is denied by policy: ' + relative)
    if not any(fnmatch.fnmatch(canonical_relative, pattern)
               for pattern in patterns):
        raise ValueError('path is not writable by the project policy '
                         '(project.mcp.write_allow): ' + relative)
    if root == project.path and not relative.startswith(
            ('.debops/', 'ansible/')):
        # Display the project-anchored path with its leading slash
        return path, '/' + canonical_relative
    return path, canonical_relative


def _validate_mcp_content(relative, content):
    """Validate project MCP content at write time.

    Returns an error string, or None when the content is not MCP
    content or is valid.
    """
    parts = relative.split('/')
    if parts[0:2] != ['.debops', 'mcp'] or len(parts) < 4:
        return None
    from ..skills import SKILL_NAME_PATTERN, _parse_frontmatter_text
    kind = parts[2]
    if kind == 'skills':
        name = parts[3]
        if not SKILL_NAME_PATTERN.match(name):
            return 'invalid skill name: ' + name
        if len(parts) == 5 and parts[4] == 'SKILL.md':
            meta = _parse_frontmatter_text(content)
            if meta.get('name', name) != name:
                return ('the frontmatter name does not match the '
                        'directory: ' + name)
            if not meta.get('description'):
                return 'the skill has no description in the frontmatter'
    elif kind == 'prompts':
        if len(parts) != 4 or not parts[3].endswith('.md'):
            return 'prompts are <name>.md files: ' + relative
        name = parts[3][:-3]
        if not SKILL_NAME_PATTERN.match(name):
            return 'invalid prompt name: ' + name
        meta = _parse_frontmatter_text(content)
        if not meta.get('description'):
            return 'the prompt has no description in the frontmatter'
    elif kind == 'tools':
        if len(parts) != 4:
            return ('a custom tool is a single file directly in '
                    '.debops/mcp/tools/: ' + relative)
        if not SKILL_NAME_PATTERN.match(parts[3]):
            return 'invalid tool name: ' + parts[3]
        if not content.startswith('#!'):
            return 'a custom tool must start with a shebang (#!/...)'
    else:
        return 'unknown MCP content kind: {} (expected skills/, ' \
               'prompts/ or tools/)'.format(kind)
    return None


def _parse_write_mode(arguments):
    """The octal 'mode' argument as an int, or None; ValueError if bad."""
    if arguments.get('mode') is None:
        return None
    raw_mode = str(arguments['mode'])
    if not re.fullmatch(r'0[0-7]{3}', raw_mode):
        raise ValueError('invalid mode: {!r} (an octal string like '
                         '"0755", permission bits only)'
                         .format(raw_mode))
    return int(raw_mode, 8)


def tool_file_write(project, policy, arguments, server=None):
    # A leading slash anchors at the project root; keep it
    relative = str(arguments.get('path') or '').rstrip('/')
    content = arguments.get('content')
    if not isinstance(content, str):
        return error_result('the "content" argument is required')
    size = len(content.encode('utf-8'))
    if size > WRITE_SIZE_LIMIT:
        return error_result('content is larger than {} bytes: {}'
                            .format(WRITE_SIZE_LIMIT, relative))
    try:
        mode = _parse_write_mode(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    # Writes are recoverable and attributable only with git
    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    try:
        path, view_relative = _authorized_write_path(project, policy,
                                                     relative)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    if (view_relative.startswith('.debops/mcp/')
            and not policy.project_content):
        return error_result('project MCP content is disabled by the '
                            'policy (project.mcp.project_content)')

    # Content served to the model is validated at write time
    invalid = _validate_mcp_content(view_relative, content)
    if invalid is not None:
        return error_result(invalid)

    if (mode is None and content.startswith('#!')
            and view_relative.startswith('.debops/mcp/tools/')
            and view_relative.count('/') == 3):
        # A custom script tool is only discovered when it is executable
        mode = 0o755

    def announce():
        _announce_mcp_content_change(server, view_relative)
    return _write_committed(project, policy, path, view_relative,
                            content, mode, 'file.write ' + view_relative,
                            after=announce)


def _atomic_write(path, content, mode, old_mode):
    """Write content to path atomically (a temporary file, renamed).

    The mode is applied when given; an existing file keeps its mode; a
    new file gets 0644. OSError propagates to the caller.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.mcp-write-')
    done = False
    try:
        with os.fdopen(fd, 'w') as fp:
            fp.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        elif old_mode is not None:
            # The rename would otherwise reset the permissions
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


def _write_committed(project, policy, path, display, content, mode,
                     note, after=None):
    """Write content to an authorized path atomically and commit it.

    Returns the tool result. The caller authorizes the path; this
    handles the file mechanics: the unchanged short-circuit, the mode
    handling (preserved on overwrite, 0644 for new files), the atomic
    rename, and the commit with MCP provenance. 'after' runs once the
    change is committed.
    """
    size = len(content.encode('utf-8'))
    with exclusive_project(project, policy, note=note, paths=[path]):
        if os.path.lexists(path) and not os.path.isfile(path):
            return error_result('not a regular file: ' + display)
        old_content = None
        old_mode = None
        if os.path.isfile(path):
            with open(path, 'rb') as fp:
                try:
                    old_content = fp.read().decode('utf-8')
                except UnicodeDecodeError:
                    return error_result('not a UTF-8 text file: '
                                        + display)
            old_mode = stat.S_IMODE(os.stat(path).st_mode)

        if old_content == content:
            if mode is None or mode == old_mode:
                return text_result('unchanged: {} ({} bytes)'.format(
                        display, size))
            os.chmod(path, mode)
            outcome = 'mode changed'
        else:
            try:
                _atomic_write(path, content, mode, old_mode)
            except OSError as errmsg:
                return error_result('cannot write {}: {}'.format(
                        display, errmsg))
            outcome = 'overwritten' if old_content is not None \
                else 'created'

        # The commit is part of the contract: an explicit, attributable
        # record of the change. When it fails, the change has landed
        # but is NOT recorded — the result must say so
        commit_error = commit_or_report(
                project, note, '{}: {}'.format(outcome, display),
                paths=[path])
        if commit_error is not None:
            return commit_error
        if after is not None:
            after()
        return text_result('{}: {} ({} bytes, mode {:04o})'.format(
                outcome, display, size,
                stat.S_IMODE(os.stat(path).st_mode)))


def _announce_mcp_content_change(server, view_relative):
    """Emit the notifications for a change under '.debops/mcp/'."""
    if server is None:
        return
    parts = view_relative.split('/')
    if parts[0:2] != ['.debops', 'mcp'] or len(parts) < 3:
        return
    if parts[2] == 'skills':
        server.emit_list_changed('resources')
        if len(parts) > 3:
            # The resource URI of the changed file itself
            server.emit_resource_updated(
                    'skill://' + '/'.join(parts[3:]))
    elif parts[2] == 'prompts':
        server.emit_list_changed('prompts')
    elif parts[2] == 'tools':
        # A custom script tool appeared or went away; the reload
        # announces a changed tool surface itself. The write lock is
        # already held here, as the reload expects
        server.reload()


def tool_file_delete(project, policy, arguments, server=None):
    # A leading slash anchors at the project root; keep it
    relative = str(arguments.get('path') or '').rstrip('/')

    # Deletions are recoverable and attributable only with git
    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    try:
        path, view_relative = _authorized_write_path(project, policy,
                                                     relative)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    if (view_relative.startswith('.debops/mcp/')
            and not policy.project_content):
        return error_result('project MCP content is disabled by the '
                            'policy (project.mcp.project_content)')

    with exclusive_project(project, policy,
                           note='file.delete ' + view_relative,
                           paths=[path]):
        if not os.path.lexists(path):
            return error_result('file not found: ' + view_relative)
        if not os.path.isfile(path):
            return error_result('not a regular file: ' + view_relative)
        try:
            os.unlink(path)
        except OSError as errmsg:
            return error_result('cannot delete {}: {}'.format(
                    view_relative, errmsg))
        _announce_mcp_content_change(server, view_relative)
        # The commit is part of the tool's contract, as for file.write;
        # when it fails the file is gone but NOT recorded in git
        commit_error = commit_or_report(
                project, 'file.delete ' + view_relative,
                'deleted: {}'.format(view_relative), paths=[path])
        if commit_error is not None:
            return commit_error
        return text_result('deleted: {}'.format(view_relative))


def _inventory_spec_report(result, inventory_path, dry_run):
    """Render the result of an inventory spec application as text,
    plus its machine-readable form."""
    def rel(paths):
        return [os.path.relpath(path, inventory_path)
                for path in paths]

    structured = {key: rel(result[key])
                  for key in ('written', 'removed', 'skipped', 'kept',
                              'planned', 'planned_removed')}

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
    return '\n'.join(lines), structured


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
        report_text, structured = _inventory_spec_report(
                result, inventory.path, dry_run=True)
        return text_result(report_text, structured=structured)

    # Writes are recoverable and attributable only with git
    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    with exclusive_project(project, policy, note='inventory.spec'):
        # Everything present is user content: without 'overwrite' the
        # specification may only add paths, not modify or remove them
        result = inventory.apply_spec(
                files, pre_existing=inventory.existing_paths(),
                overwrite=overwrite, keep=keep)
        if result['written'] or result['removed']:
            # The commit is part of the tool's contract, as for
            # file.write; when it fails the changes are NOT recorded.
            # Scoped to the paths the spec touched
            touched = result['written'] + result['removed']
            try:
                project.commit(message='DebOps MCP: inventory.spec',
                               paths=touched)
            except Exception as errmsg:
                report_text, _structured = _inventory_spec_report(
                        result, inventory.path, dry_run=False)
                return error_result(
                        report_text
                        + '\nThe commit failed: {} (the changes are not '
                        'recorded in git)\n'.format(errmsg))
        report_text, structured = _inventory_spec_report(
                result, inventory.path, dry_run=False)
        return text_result(report_text, structured=structured)
