# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Client-facing presentation of the server-local paths.

A stdio server runs where the harness is, so its paths are
meaningful to the client; a socket server may live in an isolated
environment the harness cannot reach, where an absolute path only
leaks the account's layout. The presenter masks the account's home
directory everywhere ('~'), and in the socket mode replaces the
known server-side roots — the run-log cache, the bundled content,
the collection roots outside the project — with URI-style monikers
('run-logs:/...', shell-inert and never a real relative path) which
stay recognizable to a human reading the output. The audit log and
the on-disk files keep the real paths; this is presentation only.
"""

import os

from ..constants import DEBOPS_USER_HOME_DIR


def make_path_presenter(monikers, home=DEBOPS_USER_HOME_DIR):
    """Build the path presenter for a server: a result dict in, a
    masked one out.

    'monikers' is the (prefix, replacement) table of the server-side
    roots, applied longest-prefix-first; a prefix only matches when
    followed by a path separator, so '/srv/collections2' never trips
    the '/srv/collections' entry. The replacement is the URI-style
    marker ('run-logs:') followed by the separator. The home
    directory is masked to '~' after the monikers, in every
    transport mode; paths nobody recognizes (inside the ansible
    output, on the managed hosts) pass through untouched.
    """
    pairs = sorted(((os.path.normpath(prefix), moniker)
                    for prefix, moniker in monikers),
                   key=lambda pair: -len(pair[0]))
    home_prefix = os.path.normpath(home) + os.sep

    def present_text(text):
        for prefix, moniker in pairs:
            text = text.replace(prefix + os.sep, moniker + os.sep)
        return text.replace(home_prefix, '~' + os.sep)

    def present(value):
        if isinstance(value, str):
            return present_text(value)
        if isinstance(value, dict):
            return dict((present_text(key) if isinstance(key, str)
                         else key, present(item))
                        for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return [present(item) for item in value]
        return value

    return present
