# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The shell front end: the interpreter with a status bar.

Same engine as the plain REPL; the session state (the place, the
trust, the elevation countdown) lives in a bottom toolbar instead of
the prompt, and output prints through 'patch_stdout', so it never
tramples the input line. The prompt application lives for the whole
session and the commands run on a worker thread: the toolbar stays
at the bottom during a run and carries the run's progress, and the
questions are answered on the input line. Without prompt_toolkit the
shell degrades to the plain reader.
"""

import sys

from .repl import (_banner, _Completer, _key_bindings,
                   run_interactive_plain)


def _toolbar(session):
    """The bottom toolbar text: the place on the left, the run state
    and the trust state on the right, padded across the terminal
    width."""
    from prompt_toolkit.application import get_app
    left, right = session.status_line()
    width = get_app().output.get_size().columns
    if len(left) + 1 + len(right) > width:
        room = width - len(left) - 1
        right = right[:max(0, room - 1)] + '…'
    padding = max(1, width - len(left) - len(right))
    return left + ' ' * padding + right


def _shell_bindings(session):
    """The shell's keys, on top of the shared ones: Ctrl-C declines a
    pending question, aborts the tracked run (a human's run stops
    when the human stops it), drops another busy command's
    connection, or clears the line; Escape declines a question."""
    bindings = _key_bindings(session)

    @bindings.add('c-c')
    def _ctrl_c(event):
        if session._gate is not None:
            session._gate_decline()
        elif session.active_run is not None:
            # Cancel the run's task on a side connection — the worker
            # holds the main one
            task_id = session.active_run.get('task_id')
            try:
                side = session.client.clone()
                side.request('tasks/cancel', {'taskId': task_id})
                side.close()
            except Exception:
                session.client.close()
            session._note('the run is cancelled')
        elif session._follow_client is not None:
            # A 'log --follow' tail ends; the run is untouched
            session._follow_client.close()
            session._note('the tail ends')
        elif session._busy:
            # The command's connection drops; its worker errors out
            session.client.close()
            session._note('the command lost its connection')
        else:
            event.app.current_buffer.reset()

    @bindings.add('escape')
    def _escape(event):
        gate = session._gate
        session._gate_decline()
        if gate is None or gate['secret']:
            # Do not leave a half-typed secret on the line
            event.app.current_buffer.reset()

    return bindings


def _build_shell(session):
    """The shell's prompt_toolkit session, headless-buildable for the
    tests: the persistent app, the worker-submitted commands, the
    bindings."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.layout.processors import (DynamicProcessor,
                                                  PasswordProcessor)
    from prompt_toolkit.shortcuts import CompleteStyle
    from prompt_toolkit.styles import Style
    # The questions are answered on the input line; the run progress
    # rides the toolbar and the dotted completion marker
    session.gate_input = True
    session.run_display = 'dots'
    session.busy_note = ('a command is running; Ctrl-C aborts a run, '
                         'drops the rest\n')
    bindings = _shell_bindings(session)

    password = PasswordProcessor()
    prompt_session = PromptSession(
            message=lambda: session._prompt_marker() + ' ',
            completer=_Completer(session),
            # The shell habit: Tab inserts the completion, a double
            # Tab lists the candidates
            complete_style=CompleteStyle.READLINE_LIKE,
            # Job control, unless the account is confined
            enable_suspend=not session.confined,
            key_bindings=bindings,
            history=session._new_history(),
            # A pending secret gate (the trust passphrase) masks the
            # input line
            input_processors=[DynamicProcessor(
                    lambda: (password if session._gate is not None
                             and session._gate['secret'] else None))],
            bottom_toolbar=lambda: _toolbar(session),
            style=Style.from_dict({'bottom-toolbar': 'reverse'}))

    def accept(buffer):
        line = buffer.text
        # Gate answers (the trust passphrase!) never enter the
        # history. The reset comes before the submit: the caller
        # ('validate_and_handle') appends to the history after the
        # handler returns, and an empty buffer stores nothing
        if session._gate is None and line.strip():
            buffer.append_to_history()
        buffer.reset()
        session.submit(line)

    prompt_session.default_buffer.accept_handler = accept
    return prompt_session


def run_shell(session):
    """Run the interpreter with the prompt_toolkit chrome: the status
    toolbar under the input line, the short prompt."""
    try:
        import prompt_toolkit  # noqa: F401
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        # No library, no chrome; the plain reader carries the session
        return run_interactive_plain(session)
    prompt_session = _build_shell(session)
    # The toolbar invalidations land on the app
    session._app = prompt_session.app
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
            prompt_session.prompt()
        except EOFError:
            pass
        finally:
            session.stop_watch()
            session._out, session._err = original_out, original_err
    return 0
