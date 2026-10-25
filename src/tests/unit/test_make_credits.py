# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the lib/make-credits generator.

Written with 'unittest' so that the suite runs under pytest without extra
dependencies.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GENERATOR = os.path.join(REPO_ROOT, 'lib', 'make-credits')


class MakeCreditsTestCase(unittest.TestCase):

    def setUp(self):
        if shutil.which('git') is None:
            self.skipTest('git is not installed')
        self.tmpdir = tempfile.mkdtemp(prefix='debops-credits-')
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.repo = os.path.join(self.tmpdir, 'repo')
        os.makedirs(os.path.join(self.repo, 'lib'))
        # The generator roots itself at the parent of its lib/
        shutil.copy(GENERATOR, os.path.join(self.repo, 'lib',
                                            'make-credits'))

    def git(self, *args, author=None):
        env = dict(os.environ)
        if author is not None:
            env['GIT_AUTHOR_NAME'] = author
            env['GIT_AUTHOR_EMAIL'] = '{}@test'.format(
                    author.lower().replace(' ', '.'))
        subprocess.run(['git', '-C', self.repo] + list(args),
                       env=env, check=True, capture_output=True)

    def commit(self, name, author, path='file.txt'):
        with open(os.path.join(self.repo, path), 'w') as fp:
            fp.write(name)
        self.git('add', path)
        self.git('-c', 'user.name=Tests', '-c',
                 'user.email=tests@test', 'commit', '-m', name,
                 author=author)

    def test_the_credits_file_sections(self):
        self.git('init', '-q')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@test',
                 'commit', '-q', '--allow-empty', '-m', 'root')
        self.commit('one', 'Jane Hacker')
        self.commit('two', 'Jane Hacker')
        self.commit('three', 'John Doe')
        result = subprocess.run(
                [os.path.join(self.repo, 'lib', 'make-credits')],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = os.path.join(self.repo, 'src', 'debops', '_data',
                              'debops', 'credits')
        with open(target) as fp:
            text = fp.read()
        self.assertIn('The project’s main authors', text)
        self.assertIn('Jane Hacker (2)', text)
        self.assertIn('John Doe (1)', text)
        self.assertIn('Contributors', text)
        # The header and the special thanks ride along
        self.assertTrue(text.startswith('DebOps\n'
                'Your Debian-based data center in a box\n'))
        self.assertIn('-present', text)
        self.assertIn('Kimi K3 as Cogito Mens', text)
        self.assertIn('Special thanks', text)
        self.assertIn('Debian', text)
        # The project's tooling ignores the generated file
        self.assertIn('credits', open(os.path.join(
                REPO_ROOT, '.gitignore')).read())
