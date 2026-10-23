# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The trust root of a project's MCP daemon: a human-oriented
passphrase.

The passphrase lives at '.debops/mcp/secret/trust' — a path the MCP
tools never serve (the built-in deny list) and git never sees (the
'.gitignore' written next to it). The words come from the bundled
EFF large wordlist; four space-separated words read and type like a
sentence fragment, which is the point: it is not a password. The
server re-reads the file per authentication attempt, so rotation is
replacing the file.
"""

import logging
import os
import secrets

from ..constants import DEBOPS_PACKAGE_DATA

logger = logging.getLogger(__name__)

# The passphrase of a project daemon, relative to the project
TRUST_PATH = os.path.join('.debops', 'mcp', 'secret', 'trust')

# The bundled EFF large wordlist (CC-BY-3.0)
_WORDLIST = os.path.join(DEBOPS_PACKAGE_DATA, 'mcp', 'wordlists',
                         'eff_large_wordlist.txt')

_WORDS = None


def _words():
    """The bundled wordlist, loaded once per process."""
    global _WORDS
    if _WORDS is None:
        with open(_WORDLIST) as fp:
            _WORDS = [line.strip() for line in fp if line.strip()]
    return _WORDS


def word_passphrase(count=4):
    """A fresh human-oriented passphrase: four words, space-joined."""
    return ' '.join(secrets.choice(_words()) for _ in range(count))


def normalize_passphrase(text):
    """The compare form: lowercase, whitespace and hyphen runs folded
    to single spaces (a hyphenated paste matches the spaced file)."""
    import re
    return re.sub(r'[\s-]+', ' ', str(text).strip().lower())


def passphrase_matches(presented, stored):
    """Constant-time comparison of the normalized forms."""
    return secrets.compare_digest(normalize_passphrase(presented),
                                  normalize_passphrase(stored))


def ensure_passphrase(project):
    """Create the trust passphrase of a project when it has none.

    The directory is 0700 and the file 0600. The '.gitignore' next to
    it marks the whole directory out of git — the MCP commit machinery
    included — and carries no self-exception: even the marker stays
    invisible, so 'git status' never mentions the directory. Returns
    True when a passphrase was generated. On a project shared between
    accounts the directory may belong to another account's daemon;
    the failure is logged and this daemon serves without the trust.
    """
    directory = os.path.join(project.path, os.path.dirname(TRUST_PATH))
    gitignore = os.path.join(directory, '.gitignore')
    path = os.path.join(project.path, TRUST_PATH)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        write = True
        try:
            with open(gitignore) as fp:
                write = fp.read() != '*\n'
        except OSError:
            pass
        if write:
            with open(gitignore, 'w') as fp:
                fp.write('*\n')
        if os.path.exists(path):
            return False
        with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600),
                  'w') as fp:
            fp.write(word_passphrase() + '\n')
    except OSError as errmsg:
        # Serve without the trust rather than not at all
        logger.notice('The trust passphrase cannot be created here '
                      '(%s); the trust prompts stay unavailable',
                      errmsg)
        return False
    logger.notice('The trust passphrase of this project was generated: '
                  '%s (unreadable to the MCP tools and invisible to '
                  'git; replace it to rotate)', path)
    return True


def read_passphrase(project):
    """The stored passphrase of a project, or None."""
    try:
        with open(os.path.join(project.path, TRUST_PATH)) as fp:
            return fp.read()
    except OSError:
        return None
