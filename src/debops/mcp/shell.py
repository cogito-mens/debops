# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The shell front end: the interpreter with a status bar.

Same engine as the plain REPL; the session state (the place, the
trust, the elevation countdown) lives in a bottom toolbar instead of
the prompt, and output prints through 'patch_stdout', so it never
tramples the input line. Without prompt_toolkit the shell degrades
to the plain reader.
"""

import sys

from .repl import (_banner, _Completer, _execute_line,
                   run_interactive_plain)


def _toolbar(session):
    """The bottom toolbar text: the place on the left, the trust
    state on the right, padded across the terminal width."""
    from prompt_toolkit.application import get_app
    left, right = session.status_line()
    width = get_app().output.get_size().columns
    padding = max(1, width - len(left) - len(right))
    return left + ' ' * padding + right


def run_shell(session):
    """Run the interpreter with the prompt_toolkit chrome: the status
    toolbar under the input line, the short prompt."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.shortcuts import CompleteStyle
        from prompt_toolkit.styles import Style
    except ImportError:
        # No library, no chrome; the plain reader carries the session
        return run_interactive_plain(session)
    prompt_session = PromptSession(
            completer=_Completer(session),
            # The shell habit: Tab inserts the completion, a double
            # Tab lists the candidates
            complete_style=CompleteStyle.READLINE_LIKE,
            history=session._new_history(),
            bottom_toolbar=lambda: _toolbar(session),
            style=Style.from_dict({'bottom-toolbar': 'reverse'}))
    # A view switch moves the recall and the record to the view's
    # own history file
    session._history_changed = lambda: setattr(
            prompt_session.default_buffer, 'history',
            session._new_history())
    print(_banner(session), file=session._err)
    # The shell streams the watch in the background: a reader thread
    # on its own connection, the lines printed above the prompt
    session.background_watch = True
    with patch_stdout():
        # The session's channels were captured at construction time;
        # re-bind them so the prints inside the loop reach the proxy
        original_out, original_err = session._out, session._err
        session._out, session._err = sys.stdout, sys.stderr
        try:
            while True:
                try:
                    line = prompt_session.prompt(
                            'debops!> ' if session.elevated
                            else 'debops> ')
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                try:
                    if _execute_line(session, line):
                        break
                except KeyboardInterrupt:
                    # A command died on the interrupt; the session
                    # survives. The connection may hold a half-read
                    # answer — drop it
                    print('^C', file=session._err)
                    session.client.close()
        finally:
            session.stop_watch()
            session._out, session._err = original_out, original_err
    return 0
