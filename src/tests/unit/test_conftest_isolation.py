# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for the process-state isolation fixture.

The autouse fixture in the shared conftest restores ``os.environ`` and
the working directory after each test; without it, a mutation such as
the ``ANSIBLE_CONFIG`` export done by ``Configuration.set_env`` — or a
``chdir`` into a temporary tree which is then removed — leaks into
every later test of the run. The canary pairs pin the behavior:
unittest runs methods in alphabetical order, so each second method
observes its predecessor's mutation only if the isolation is broken.
"""

import os
import unittest


class EnvironIsolationTestCase(unittest.TestCase):

    def test_a_mutation_happens(self):
        os.environ['DEBOPS_TEST_ISOLATION_CANARY'] = 'canary'
        self.assertEqual(
                os.environ.get('DEBOPS_TEST_ISOLATION_CANARY'),
                'canary')

    def test_b_mutation_is_gone(self):
        self.assertIsNone(
                os.environ.get('DEBOPS_TEST_ISOLATION_CANARY'))

    def test_c_chdir_happens(self):
        os.chdir(os.path.sep)
        self.assertEqual(os.getcwd(), os.path.sep)

    def test_d_chdir_is_gone(self):
        self.assertNotEqual(os.getcwd(), os.path.sep)
