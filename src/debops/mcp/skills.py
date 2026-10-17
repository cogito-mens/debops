# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The skills shipped with the DebOps package, served as MCP resources.

A skill is a directory with a SKILL.md file (plus optional supporting
files), following the Agent Skills convention. The library lists them
and reads them; the server exposes them under 'skill://' URIs.
"""

import logging
import os
import re
import yaml

from ..constants import DEBOPS_PACKAGE_DATA
from .policy import confine_under

logger = logging.getLogger(__name__)

SKILL_URI_PREFIX = 'skill://'

# Skill names are lowercase words separated by dashes; the directory
# name must match the frontmatter name
SKILL_NAME_PATTERN = re.compile(r'^[a-z0-9-]+$')


def _parse_frontmatter(path):
    """Parse the YAML frontmatter of a SKILL.md file."""
    try:
        with open(path) as fp:
            text = fp.read(8192)
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return {}
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == '---':
            end = index
            break
    if end is None:
        return {}
    try:
        data = yaml.safe_load('\n'.join(lines[1:end]))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


class SkillLibrary(object):
    """List and read the skills shipped with the package."""

    def __init__(self, path=None):
        self.path = path or os.path.join(DEBOPS_PACKAGE_DATA, 'skills')

    def list(self):
        """The skills as MCP resource descriptors."""
        skills = []
        if not os.path.isdir(self.path):
            return skills
        for entry in sorted(os.listdir(self.path)):
            skill_dir = os.path.join(self.path, entry)
            skill_file = os.path.join(skill_dir, 'SKILL.md')
            if not os.path.isdir(skill_dir):
                continue
            if not SKILL_NAME_PATTERN.match(entry):
                logger.warning('Ignoring skill with an invalid name: %s',
                               entry)
                continue
            if not os.path.isfile(skill_file):
                logger.warning('Ignoring skill without SKILL.md: %s',
                               entry)
                continue
            meta = _parse_frontmatter(skill_file)
            if meta.get('name', entry) != entry:
                logger.warning('Ignoring skill %s: the frontmatter name '
                               'does not match the directory', entry)
                continue
            if not meta.get('description'):
                # The Agent Skills convention requires it; it is also
                # the only thing an agent sees when choosing a skill
                logger.warning('Ignoring skill %s: no description in the '
                               'frontmatter', entry)
                continue
            skills.append({
                'uri': SKILL_URI_PREFIX + entry,
                'name': entry,
                'description': str(meta.get('description', '')),
                'mimeType': 'text/markdown',
            })
        return skills

    def read(self, uri):
        """Read a 'skill://' URI: a SKILL.md or a file under its skill.

        Returns the file contents, or None when not found. ValueError
        marks an invalid or escaping URI.
        """
        if not uri.startswith(SKILL_URI_PREFIX):
            raise ValueError('unsupported URI scheme: ' + uri)
        parts = uri[len(SKILL_URI_PREFIX):].split('/')
        name = parts[0]
        if not SKILL_NAME_PATTERN.match(name):
            raise ValueError('invalid skill name: ' + name)
        skill_dir = os.path.join(self.path, name)
        relpath = 'SKILL.md' if len(parts) == 1 else '/'.join(parts[1:])
        target = confine_under(skill_dir, relpath)
        if not os.path.isfile(target):
            return None
        with open(target) as fp:
            return fp.read()
