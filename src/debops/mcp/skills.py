# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The skills shipped with the DebOps package, served per SEP-2640.

A skill is a directory with a SKILL.md file (plus optional supporting
files), following the Agent Skills convention; skills may nest at any
depth, and the path segments up to the last are the organizational
prefix ('skill://roles/nginx/SKILL.md' is the skill 'nginx' under
'roles/'). The library lists them, reads them and enumerates the
directory tree; the server exposes them as resources and through the
'skills/list', 'skills/get' and 'resources/directory/read' methods of
the MCP skills extension.

Generated skill sources (the per-role briefs) plug in through the
'sources' parameter: each provides entries(), files() and read(); an
entry whose 'resources' is the string 'dynamic' publishes no digests.
A filesystem skill shadows a generated one at the same path, and a
generated prefix ('roles/') is reserved outside of the project tree:
an authored skill claiming one is refused.
"""

import hashlib
import logging
import mimetypes
import os
import re
import yaml

from ..constants import DEBOPS_PACKAGE_DATA
from .policy import confine_under

logger = logging.getLogger(__name__)

SKILL_URI_PREFIX = 'skill://'

# The skills extension identifier and its capability declaration
SKILLS_EXTENSION = 'io.modelcontextprotocol/skills'

# Skill name segments are lowercase words separated by dashes; the last
# segment is the skill's name and must match the frontmatter name
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


def _mime_type(filename):
    """The resource mimeType of a skill file."""
    if filename.endswith('.md'):
        return 'text/markdown'
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or 'text/plain'


def _split_uri(uri):
    """The path inside the skills namespace; ValueError when invalid.

    Where the skill directory ends and the file begins is not
    decidable from the URI alone ('roles/nginx/templates/x.md' could
    cut two ways) — the readers resolve it against the content.
    """
    if not uri.startswith(SKILL_URI_PREFIX):
        raise ValueError('unsupported URI scheme: ' + uri)
    rest = uri[len(SKILL_URI_PREFIX):]
    if rest.endswith('/'):
        raise ValueError('a directory URI is written without a '
                         'trailing slash: ' + uri)
    if rest.startswith('/') or '//' in rest:
        raise ValueError('empty path segment: ' + uri)
    return rest


class SkillLibrary(object):
    """List and read skills: the project's own, then the package's,
    then the generated sources.

    A project skill ('.debops/mcp/skills/<name>/') shadows a package
    skill of the same name, the way project roles shadow collection
    roles; any filesystem skill shadows a generated one at the same
    path.
    """

    def __init__(self, path=None, project_path=None, sources=None):
        self.path = path or os.path.join(DEBOPS_PACKAGE_DATA, 'skills')
        self.project_path = project_path
        self.sources = list(sources) if sources else []

    @property
    def _roots(self):
        roots = []
        if self.project_path is not None:
            roots.append(self.project_path)
        roots.append(self.path)
        return roots

    @property
    def _reserved(self):
        """The first segments the generated sources own."""
        reserved = set()
        for source in self.sources:
            reserved.update(source.prefixes)
        return reserved

    def _walk_root(self, root, is_project=False):
        """The valid skills under one root, as path → frontmatter."""
        skills = {}
        if not os.path.isdir(root):
            return skills
        for dirpath, _dirnames, filenames in os.walk(root):
            if 'SKILL.md' not in filenames:
                continue
            relpath = os.path.relpath(dirpath, root)
            segments = relpath.split(os.sep)
            name = segments[-1]
            if any(not SKILL_NAME_PATTERN.match(segment)
                   for segment in segments):
                logger.warning('Ignoring skill with an invalid name: %s',
                               relpath)
                continue
            if segments[0] in self._reserved and not is_project:
                logger.warning('Ignoring skill %s: the %s prefix belongs '
                               'to the generated corpus', relpath,
                               segments[0])
                continue
            meta = _parse_frontmatter(os.path.join(dirpath, 'SKILL.md'))
            if meta.get('name', name) != name:
                logger.warning('Ignoring skill %s: the frontmatter name '
                               'does not match the directory', relpath)
                continue
            if not meta.get('description'):
                # The Agent Skills convention requires it; it is also
                # the only thing an agent sees when choosing a skill
                logger.warning('Ignoring skill %s: no description in the '
                               'frontmatter', relpath)
                continue
            skills[relpath] = meta
        return skills

    def _filesystem_skills(self):
        """The filesystem skills as path → (root, frontmatter)."""
        merged = {}
        for root in self._roots:
            is_project = root == self.project_path
            for path, meta in self._walk_root(root, is_project).items():
                if path not in merged:
                    merged[path] = (root, meta)
                else:
                    logger.notice('Skill %s in %s shadows the packaged '
                                  'one', path, root)
        return merged

    def _entry(self, root, path, meta):
        """The Skill entry of one filesystem skill, with digests."""
        skill_dir = os.path.join(root, *path.split('/'))
        frontmatter = dict(meta)
        frontmatter.setdefault('name', path.split('/')[-1])
        resources = []
        for dirpath, _dirnames, filenames in os.walk(skill_dir):
            for filename in sorted(filenames):
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, skill_dir)
                with open(full, 'rb') as fp:
                    content = fp.read()
                resources.append({
                    'uri': '{}{}/{}'.format(
                            SKILL_URI_PREFIX, path, rel.replace(os.sep,
                                                                '/')),
                    'digest': 'sha256:' + hashlib.sha256(content)
                              .hexdigest(),
                    'size': len(content),
                })
        return {'uri': '{}{}/SKILL.md'.format(SKILL_URI_PREFIX, path),
                'frontmatter': frontmatter,
                'resources': resources}

    def entries(self):
        """The Skill entries of the library (SEP-2640 shape)."""
        merged = {}
        for source in self.sources:
            for entry in source.entries():
                merged[entry['uri']] = entry
        for path, (root, meta) in self._filesystem_skills().items():
            uri = '{}{}/SKILL.md'.format(SKILL_URI_PREFIX, path)
            merged[uri] = self._entry(root, path, meta)
        return [merged[uri] for uri in sorted(merged)]

    def list(self):
        """Every file of every skill, as MCP resource descriptors."""
        resources = []
        for entry in self.entries():
            if entry['resources'] == 'dynamic':
                resources.append({
                    'uri': entry['uri'],
                    'name': 'SKILL.md',
                    'mimeType': 'text/markdown',
                })
                continue
            for resource in entry['resources']:
                resources.append({
                    'uri': resource['uri'],
                    # The path tells skills apart; the bare filename
                    # is 'SKILL.md' for hundreds of them
                    'name': resource['uri'][len(SKILL_URI_PREFIX):],
                    'mimeType': _mime_type(resource['uri']),
                })
        return resources

    def _file_uris(self):
        """Every addressable file URI, from the filesystem and the
        generated sources."""
        uris = set()
        for entry in self.entries():
            if entry['resources'] == 'dynamic':
                uris.add(entry['uri'])
            else:
                for resource in entry['resources']:
                    uris.add(resource['uri'])
        return uris

    def directory(self, uri):
        """The direct children of a directory resource URI.

        Returns a list of resource descriptors (subdirectories carry
        'inode/directory'), or None when the URI is not a directory.
        """
        rest = _split_uri(uri)
        prefix = SKILL_URI_PREFIX + rest + ('/' if rest else '')
        children = {}
        for file_uri in self._file_uris():
            if not file_uri.startswith(prefix):
                continue
            tail = file_uri[len(prefix):]
            segment = tail.split('/')[0]
            child = prefix + segment
            if '/' in tail:
                children[child] = {
                    'uri': child,
                    'name': segment,
                    'mimeType': 'inode/directory',
                }
            else:
                children[child] = {
                    'uri': child,
                    'name': segment,
                    'mimeType': _mime_type(segment),
                }
        if not children and rest:
            return None
        return [children[child] for child in sorted(children)]

    def entry_for(self, uri):
        """The Skill entry for a SKILL.md URI, or None."""
        rest = _split_uri(uri)
        if not rest.endswith('/SKILL.md'):
            raise ValueError('a skill is named by the URI of its '
                             'SKILL.md: ' + uri)
        for entry in self.entries():
            if entry['uri'] == uri:
                return entry
        return None

    def project_fingerprint(self):
        """A {name: mtime} fingerprint of the project's own skills.

        Used to tell whether a git pull changed the served content.
        """
        fingerprint = {}
        if self.project_path is None or not os.path.isdir(
                self.project_path):
            return fingerprint
        for path in self._walk_root(self.project_path,
                                    is_project=True):
            skill_dir = os.path.join(self.project_path, path)
            newest = 0
            for root, _dirs, files in os.walk(skill_dir):
                for entry in files:
                    newest = max(newest, os.stat(os.path.join(
                            root, entry)).st_mtime_ns)
            fingerprint[path] = newest
        return fingerprint

    def read(self, uri):
        """Read a 'skill://' file URI (the SKILL.md is explicit).

        Returns the file contents, or None when not found. ValueError
        marks an invalid or escaping URI. The skill directory is the
        longest prefix which is a skill (a directory with a SKILL.md);
        the file is the remainder.
        """
        rest = _split_uri(uri)
        segments = rest.split('/')
        if len(segments) < 2:
            raise ValueError('a skill file is addressed as '
                             '<skill-path>/<file>: ' + uri)
        # The confinement below does the real work; traversal segments
        # fail fast with a clear message
        if '..' in segments:
            raise ValueError('invalid skill path: ' + uri)
        for cut in range(len(segments) - 1, 0, -1):
            relpath = '/'.join(segments[cut:])
            for root in self._roots:
                skill_dir = os.path.join(root, *segments[:cut])
                if not os.path.isfile(os.path.join(skill_dir,
                                                   'SKILL.md')):
                    continue
                target = confine_under(skill_dir, relpath)
                if os.path.isfile(target):
                    with open(target) as fp:
                        return fp.read()
        for source in self.sources:
            content = source.read(rest)
            if content is not None:
                return content
        return None
