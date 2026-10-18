# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Project-local MCP prompts from '.debops/mcp/prompts/'.

A prompt is a Markdown file '<name>.md' with YAML frontmatter giving a
description and the arguments, and a body rendered with Jinja2 when the
prompt is fetched:

    ---
    description: Deploy a service
    arguments:
      - name: service
        description: The service playbook to run
        required: true
    ---

    Deploy the {{ service }} service: ...

"""

import logging
import os

from ..constants import DEBOPS_PACKAGE_DATA
from ..utils import strtobool
from .skills import SKILL_NAME_PATTERN, _parse_frontmatter
import jinja2

logger = logging.getLogger(__name__)


class PromptLibrary(object):
    """List and render prompts: the project's own, then the package's.

    A project prompt ('.debops/mcp/prompts/<name>.md') shadows a package
    prompt of the same name, the way project skills shadow package
    skills.
    """

    def __init__(self, path, package_path=None):
        self.path = path
        self.package_path = (package_path if package_path is not None
                             else None)

    @property
    def _roots(self):
        roots = [self.path]
        if self.package_path is not None:
            roots.append(self.package_path)
        return roots

    def available(self):
        """Whether any prompts directory exists at all."""
        return any(os.path.isdir(root) for root in self._roots)

    def _prompts(self):
        """The valid prompts, as name → (path, frontmatter)."""
        prompts = {}
        for root in self._roots:
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                if not entry.endswith('.md'):
                    continue
                name = entry[:-3]
                if not SKILL_NAME_PATTERN.match(name):
                    logger.warning('Ignoring prompt with an invalid name: '
                                   '%s', entry)
                    continue
                path = os.path.join(root, entry)
                if not os.path.isfile(path):
                    continue
                if name in prompts:
                    logger.notice('Prompt %s in %s shadows the packaged '
                                  'one', name, root)
                    continue
                meta = _parse_frontmatter(path)
                if not meta.get('description'):
                    # The only thing an agent sees when choosing a prompt
                    logger.warning('Ignoring prompt %s: no description in '
                                   'the frontmatter', name)
                    continue
                prompts[name] = (path, meta)
        return prompts

    def list(self):
        """The prompts as MCP prompt descriptors."""
        prompts = []
        for name, (_path, meta) in self._prompts().items():
            descriptor = {'name': name,
                          'description': str(meta['description'])}
            arguments = []
            for argument in meta.get('arguments') or []:
                if not isinstance(argument, dict) or not argument.get('name'):
                    continue
                entry = {'name': str(argument['name'])}
                if argument.get('description'):
                    entry['description'] = str(argument['description'])
                required = argument.get('required', False)
                if isinstance(required, str):
                    required = strtobool(required)
                entry['required'] = bool(required)
                arguments.append(entry)
            if arguments:
                descriptor['arguments'] = arguments
            prompts.append(descriptor)
        return prompts

    def render(self, name, arguments):
        """Render a prompt with the given arguments.

        Returns a dict with the 'description' and the rendered 'text'.
        ValueError marks an unknown prompt or missing required
        arguments.
        """
        prompts = self._prompts()
        if name not in prompts:
            raise ValueError('prompt not found: ' + str(name))
        path, meta = prompts[name]
        declared = {str(argument['name']): argument
                    for argument in meta.get('arguments') or []
                    if isinstance(argument, dict) and argument.get('name')}
        missing = [argname for argname, argument in declared.items()
                   if argument.get('required')
                   and argname not in (arguments or {})]
        if missing:
            raise ValueError('missing required argument(s): '
                             + ', '.join(sorted(missing)))
        with open(path) as fp:
            text = fp.read()
        # The body is what follows the frontmatter
        lines = text.splitlines()
        if lines and lines[0].strip() == '---':
            for index in range(1, len(lines)):
                if lines[index].strip() == '---':
                    text = '\n'.join(lines[index + 1:]).strip() + '\n'
                    break
        return {'description': str(meta['description']),
                'text': jinja2.Template(text, keep_trailing_newline=True)
                .render(**(arguments or {}))}
