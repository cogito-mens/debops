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

    def build_repo(self):
        """A synthetic history: two commits by one author, one by
        another, the root by the test suite."""
        self.git('init', '-q')
        self.git('-c', 'user.name=Tests', '-c', 'user.email=tests@test',
                 'commit', '-q', '--allow-empty', '-m', 'root')
        self.commit('one', 'Jane Hacker')
        self.commit('two', 'Jane Hacker')
        self.commit('three', 'John Doe')

    def generate(self):
        """Run the generator on the synthetic repo; return the
        file's text."""
        result = subprocess.run(
                [os.path.join(self.repo, 'lib', 'make-credits')],
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = os.path.join(self.repo, 'src', 'debops', '_data',
                              'debops', 'credits')
        with open(target) as fp:
            return fp.read()

    def test_the_credits_file_sections(self):
        self.build_repo()
        text = self.generate()
        self.assertIn('The project’s main authors', text)
        self.assertIn('Jane Hacker (2)', text)
        self.assertIn('John Doe (1)', text)
        self.assertIn('Contributors', text)
        # The header and the special thanks ride along
        self.assertTrue(text.startswith(
                'DebOps\n'
                'Your Debian-based data center in a box\n'))
        self.assertIn('-present', text)
        self.assertIn('Kimi K3 as Cogito Mens', text)
        self.assertIn('Special thanks', text)
        self.assertIn('Debian', text)
        # The easter egg closes the roll, one of the footers
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader(
                'mc', GENERATOR)
        spec = importlib.util.spec_from_file_location(
                'mc', GENERATOR, loader=loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        self.assertIn(text.rstrip().splitlines()[-1].strip(),
                      module.FOOTERS)
        # The project's tooling ignores the generated file
        with open(os.path.join(REPO_ROOT, '.gitignore')) as fp:
            self.assertIn('credits', fp.read())

    def test_nobody_is_lost_to_the_cuts(self):
        # The main lists cut at ten; whoever misses the cuts stands
        # in the Contributors — the file's names are the history's
        self.build_repo()
        text = self.generate()
        for name in ('Jane Hacker', 'John Doe', 'Tests'):
            self.assertIn(name, text)
