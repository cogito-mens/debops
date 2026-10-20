# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Manage the project's Ansible Collections.

'collection.require' adds or updates an entry in the project
requirements file ('ansible/collections/requirements.yml') and installs
the collections; 'collection.remove' removes an entry together with the
installed content (which ansible-galaxy cannot do); 'collection.sync'
installs the committed state, for example after a pull. All three
commit their changes to git with provenance.
"""

import io
import logging
import os
import re
import shutil
import subprocess

import yaml

from ..policy import confine_under
from .common import (commit_or_report, error_result, exclusive_project,
                     mcp_environ, require_git_tracked,
                     text_result)
from .files import _atomic_write

logger = logging.getLogger(__name__)

# A collection name is 'namespace.name', both lowercase
COLLECTION_NAME = re.compile(r'^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$')

# The first line of an entry block names the collection
_BLOCK_NAME = re.compile(r'^\s*-\s*name:\s*[\'"]?([^\'"\s#]+)')

# The tail of the install output shown in the result
_OUTPUT_TAIL = 4000


def _requirements_path(project):
    return os.path.join(project.path, 'ansible', 'collections',
                        'requirements.yml')


def _lock_path(project):
    return os.path.join(project.path, 'ansible', 'collections',
                        'requirements.lock.yml')


def _collections_content(project):
    return os.path.join(project.path, 'ansible', 'collections',
                        'ansible_collections')


def _entry_blocks(lines):
    """The (start, end) line spans of the '- name:' entry blocks.

    A block runs from its '- name:' line to the next blank line (the
    files are blank-line separated by convention).
    """
    blocks = []
    start = None
    for index, line in enumerate(lines):
        if _BLOCK_NAME.match(line):
            if start is not None:
                blocks.append((start, index))
            start = index
        elif start is not None and not line.strip():
            blocks.append((start, index))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks


def _render_entry(entry):
    """A collection entry as list-item lines, in the project style."""
    lines = ["  - name: '{}'".format(entry['name'])]
    for key, value in entry.items():
        if key == 'name':
            continue
        lines.append("    {}: '{}'".format(key,
                                           str(value).replace("'",
                                                              "''")))
    return lines


def _upsert_requirement(text, entry):
    """Add or update a collection entry; returns (new text, changed).

    The file is parsed to find the entries but spliced as lines, so
    comments and the written order survive.
    """
    lines = text.splitlines()
    if not any(re.match(r'^collections:(\s|\[|$)', line)
               for line in lines):
        lines.extend(['', 'collections:'])
    name = entry['name']
    new_block = _render_entry(entry)
    for start, end in _entry_blocks(lines):
        if _BLOCK_NAME.match(lines[start]).group(1) == name:
            if lines[start:end] == new_block:
                return text, False
            lines[start:end] = new_block
            return '\n'.join(lines) + '\n', True
    # An empty-list marker becomes the list, then the entry is appended
    for index, line in enumerate(lines):
        if re.match(r'^\s*collections:\s*\[\s*\]\s*$', line):
            lines[index] = 'collections:\n\n' + '\n'.join(new_block)
            return '\n'.join(lines) + '\n', True
    blocks = _entry_blocks(lines)
    if blocks:
        lines[blocks[-1][1]:blocks[-1][1]] = [''] + new_block
    else:
        lines.extend([''] + new_block)
    return '\n'.join(lines) + '\n', True


def _remove_requirement(text, name):
    """Remove a collection entry; returns (new text, changed)."""
    lines = text.splitlines()
    for start, end in _entry_blocks(lines):
        if _BLOCK_NAME.match(lines[start]).group(1) == name:
            del lines[start:end]
            break
    else:
        return text, False
    # Collapse the double blank line the removal can leave
    out = []
    for line in lines:
        if line.strip() or (out and out[-1].strip()):
            out.append(line)
    return '\n'.join(out) + '\n', True


def _galaxy_install(project, context=None, enabled_check=None,
                    on_disabled=None):
    """Install the project collections; returns the captured output.

    ansible-galaxy installs into the first writable directory of the
    effective collections path, which may live outside of the project
    ('~/.ansible/collections', ...); the target is forced into the
    project with '-p'. The lock file is installed first when present,
    then the requirements file, mirroring the project virtual
    environment flow. The run goes through the shared cancellable
    subprocess machinery, so a task cancellation terminates it.
    """
    from .execute import run_cancellable
    galaxy = os.path.expanduser(
            project.config.raw.get('binaries', {})
            .get('ansible-galaxy', 'ansible-galaxy'))
    if shutil.which(galaxy) is None:
        raise FileNotFoundError('the ansible-galaxy command was not '
                                'found')
    target = os.path.join(project.path, 'ansible', 'collections')
    os.makedirs(target, exist_ok=True)
    env = mcp_environ(project)
    files = []
    lock = _lock_path(project)
    if os.path.isfile(lock):
        files.append(lock)
    files.append(_requirements_path(project))
    output = io.StringIO()
    for requirements in files:
        out, returncode, status = run_cancellable(
                [galaxy, 'collection', 'install', '-r', requirements,
                 '-p', target],
                env=env, cwd=project.path, context=context, timeout=300,
                enabled_check=enabled_check, on_disabled=on_disabled)
        print(out, file=output, end='')
        if status == 'cancelled':
            from ..server import RequestCancelled
            raise RequestCancelled()
        if status == 'disabled':
            raise ChildProcessError('terminated: the MCP functionality '
                                    'was disabled mid-run (the kill '
                                    'switch)')
        if status == 'timeout':
            raise ChildProcessError('ansible-galaxy timed out')
        if returncode != 0:
            raise ChildProcessError(
                    'cannot install the Ansible Collections from '
                    + os.path.basename(requirements))
    return output.getvalue()


def _validate_name(arguments):
    name = str(arguments.get('name') or '')
    if not COLLECTION_NAME.match(name):
        raise ValueError('not a collection name ("namespace.name"): '
                         + repr(name))
    return name


def _read_text(path):
    with open(path) as fp:
        return fp.read()


def tool_collection_require(project, policy, arguments, context,
                            enabled_check=None, on_disabled=None):
    try:
        name = _validate_name(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))
    entry = {'name': name}
    for key in ('version', 'source', 'type'):
        if arguments.get(key) is not None:
            entry[key] = str(arguments[key])

    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    requirements = _requirements_path(project)
    with exclusive_project(project, policy,
                           note='collection.require ' + name):
        try:
            if os.path.isfile(requirements):
                text = _read_text(requirements)
            else:
                text = ('---\n\n# Ansible Collections required by this '
                        'project\n\ncollections: []\n')
            text, changed = _upsert_requirement(text, entry)
            if not changed:
                return text_result(
                        'unchanged: {} is already required\n'.format(
                            name))
            _atomic_write(requirements, text, None, None)
        except OSError as errmsg:
            return error_result('cannot write the requirements file: {}'
                                .format(errmsg))

        install_error = None
        try:
            output = _galaxy_install(
                    project, context, enabled_check=enabled_check,
                    on_disabled=on_disabled)
        except (FileNotFoundError, ChildProcessError) as errmsg:
            output = ''
            install_error = str(errmsg)
        # The requirements entry is the intended state; it is committed
        # even when the install fails, and the failure is reported
        commit_error = commit_or_report(
                project, 'collection.require ' + name,
                'the requirement was written')
        if commit_error is not None:
            return commit_error

    if install_error is not None:
        return error_result('the requirement was written and committed, '
                            'but the installation failed: {} (retry with '
                            'collection.sync)'.format(install_error))
    tail = output.strip()[-_OUTPUT_TAIL:]
    lines = ['# Collection required: {}'.format(name), '',
             'The requirements file and the installation are committed.']
    if tail:
        lines.extend(['', '## Installation output', '', tail])
    return text_result('\n'.join(lines) + '\n')


def tool_collection_remove(project, policy, arguments):
    try:
        name = _validate_name(arguments)
    except ValueError as errmsg:
        return error_result(str(errmsg))

    refused = require_git_tracked(project)
    if refused is not None:
        return refused

    removed = []
    with exclusive_project(project, policy,
                           note='collection.remove ' + name):
        for path in (_requirements_path(project), _lock_path(project)):
            if not os.path.isfile(path):
                continue
            text, changed = _remove_requirement(_read_text(path), name)
            if changed:
                _atomic_write(path, text, None, None)
                removed.append(os.path.basename(path))

        # The installed content: the collection directory and the
        # install marker ansible-galaxy leaves next to it
        try:
            base = confine_under(_collections_content(project), '.')
        except ValueError as errmsg:
            return error_result(str(errmsg))
        namespace = name.split('.')[0]
        collection_dir = confine_under(base, name.replace('.', '/'))
        if os.path.isdir(collection_dir):
            shutil.rmtree(collection_dir)
            removed.append(name.replace('.', '/') + '/')
        for entry in os.listdir(base) if os.path.isdir(base) else []:
            if entry.startswith(name + '-') and entry.endswith('.info'):
                os.unlink(confine_under(base, entry))
                removed.append(entry)
        # A namespace left empty by the removal goes away too
        namespace_dir = os.path.join(base, namespace)
        if os.path.isdir(namespace_dir) and not os.listdir(namespace_dir):
            os.rmdir(namespace_dir)

        if not removed:
            return text_result('unchanged: {} is not required nor '
                               'installed\n'.format(name))
        commit_error = commit_or_report(
                project, 'collection.remove ' + name,
                'the collection was removed')
        if commit_error is not None:
            return commit_error

    return text_result('# Collection removed: {}\n\n## Removed\n\n{}\n'
                       .format(name, '\n'.join('- {}'.format(entry)
                                               for entry in removed)))


def tool_collection_sync(project, policy, arguments, context,
                         enabled_check=None, on_disabled=None):
    """Install the committed collection requirements."""
    with exclusive_project(project, policy, note='collection.sync'):
        try:
            output = _galaxy_install(
                    project, context, enabled_check=enabled_check,
                    on_disabled=on_disabled)
        except (FileNotFoundError, ChildProcessError) as errmsg:
            return error_result(str(errmsg))
    tail = output.strip()[-_OUTPUT_TAIL:]
    lines = ['# Collections synchronized']
    if tail:
        lines.extend(['', '## Installation output', '', tail])
    return text_result('\n'.join(lines) + '\n')
