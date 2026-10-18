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


def _parse_frontmatter_text(text):
    """Parse the YAML frontmatter of a document string."""
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


def _parse_frontmatter(path):
    """Parse the YAML frontmatter of a SKILL.md file."""
    try:
        with open(path) as fp:
            text = fp.read(8192)
    except OSError:
        return {}
    return _parse_frontmatter_text(text)


class SkillLibrary(object):
    """List and read skills: the project's own, then the package's.

    A project skill ('.debops/mcp/skills/<name>/') shadows a package
    skill of the same name, the way project roles shadow collection
    roles.
    """

    def __init__(self, path=None, project_path=None):
        self.path = path or os.path.join(DEBOPS_PACKAGE_DATA, 'skills')
        self.project_path = project_path

    @property
    def _roots(self):
        roots = []
        if self.project_path is not None:
            roots.append(self.project_path)
        roots.append(self.path)
        return roots

    def _list_dir(self, path):
        """The valid skills of one directory, as name → descriptor."""
        skills = {}
        if not os.path.isdir(path):
            return skills
        for entry in sorted(os.listdir(path)):
            skill_dir = os.path.join(path, entry)
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
            skills[entry] = {
                'uri': SKILL_URI_PREFIX + entry,
                'name': entry,
                'description': str(meta.get('description', '')),
                'mimeType': 'text/markdown',
            }
        return skills

    def list(self):
        """The skills as MCP resource descriptors, project first."""
        skills = {}
        for root in self._roots:
            for name, descriptor in self._list_dir(root).items():
                if name not in skills:
                    skills[name] = descriptor
                else:
                    logger.notice('Skill %s in %s shadows the packaged '
                                  'one', name, root)
        return [skills[name] for name in sorted(skills)]

    def project_fingerprint(self):
        """A {name: mtime} fingerprint of the project's own skills.

        Used to tell whether a git pull changed the served content.
        """
        fingerprint = {}
        if self.project_path is None or not os.path.isdir(
                self.project_path):
            return fingerprint
        for name in self._list_dir(self.project_path):
            skill_dir = os.path.join(self.project_path, name)
            newest = 0
            for root, _dirs, files in os.walk(skill_dir):
                for entry in files:
                    newest = max(newest, os.stat(os.path.join(
                            root, entry)).st_mtime_ns)
            fingerprint[name] = newest
        return fingerprint

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
        relpath = 'SKILL.md' if len(parts) == 1 else '/'.join(parts[1:])
        for root in self._roots:
            skill_dir = os.path.join(root, name)
            if not os.path.isdir(skill_dir):
                continue
            target = confine_under(skill_dir, relpath)
            if os.path.isfile(target):
                with open(target) as fp:
                    return fp.read()
        return None
