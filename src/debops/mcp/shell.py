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
import threading
import time

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

    from prompt_toolkit.filters import Condition

    # The console's own Ctrl-Z carries the job control of the custom
    # application (enable_suspend is a PromptSession knob)
    @bindings.add('c-z', filter=Condition(lambda: not session.confined))
    def _suspend(event):
        event.app.suspend_to_background()

    @bindings.add('c-c')
    def _ctrl_c(event):
        if session._gate is not None:
            session._gate_decline()
        elif (session.active_run is not None
                or time.monotonic() < session._abort_armed_until):
            now = time.monotonic()
            if now < session._abort_armed_until:
                # The second one stops the client
                event.app.exit(exception=EOFError())
                return
            # Cancel the run's task on a side connection — the worker
            # holds the main one
            task_id = session.active_run.get('task_id')
            try:
                side = session.client.clone()
                side.request('tasks/cancel', {'taskId': task_id})
                side.close()
            except Exception:
                session.client.close()
            # The window outlives the abort itself by a moment, so
            # the impatient second Ctrl-C still exits
            session._abort_armed_until = now + 3
            session._note('aborting the run (Ctrl-C again exits)')
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

    # Ctrl-D on an empty line leaves, the way it does everywhere;
    # with text on it, the default delete-char answers
    @bindings.add('c-d', filter=Condition(
            lambda: not getattr(session, '_input_buffer', None)
            or not session._input_buffer.text))
    def _eof(event):
        event.app.exit(exception=EOFError(), style='class:exiting')

    @bindings.add('escape')
    def _escape(event):
        gate = session._gate
        session._gate_decline()
        if gate is None or gate['secret']:
            # Do not leave a half-typed secret on the line
            event.app.current_buffer.reset()

    return bindings


def _build_shell(session):
    """The shell's own prompt_toolkit application, headless-buildable
    for the tests: the input line (gone while a run is active, back
    for a pending question), the toolbar with the run tally, the
    completion menu."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.layout import (BufferControl,
                                       ConditionalContainer,
                                       Float, FloatContainer,
                                       FormattedTextControl, HSplit,
                                       Layout, VSplit, Window)
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.processors import (DynamicProcessor,
                                                  PasswordProcessor)
    from prompt_toolkit.styles import Style
    # The questions are answered on the input line; the run progress
    # rides the toolbar
    session.gate_input = True
    session.run_display = 'shell'
    session.busy_note = ('a command is running; Ctrl-C aborts a run, '
                         'drops the rest\n')
    # The local commands echo inside the handoff (ordered against
    # their output), not through the worker's transcript echo
    session._inline_local_echo = True
    bindings = _shell_bindings(session)

    password = PasswordProcessor()

    def accept(buffer):
        line = buffer.text
        # Gate answers (the trust passphrase!) never enter the
        # history. The reset comes before the submit: the caller
        # ('validate_and_handle') appends to the history after the
        # handler returns, and an empty buffer stores nothing
        if session._gate is None and line.strip():
            buffer.append_to_history()
        buffer.reset()
        if session._gate is None and not line.strip() \
                and not session._busy:
            # A bare Enter line-feeds like a normal shell: the
            # prompt scrolls
            print(session._prompt_marker(), file=session._out)
            return
        session.submit(line)

    input_buffer = Buffer(
            completer=_Completer(session), multiline=False,
            accept_handler=accept,
            # The history file is shared with the other front ends
            # of this project and view
            history=session._new_history())
    session._input_buffer = input_buffer
    # A view switch moves the recall and the record to the view's
    # own history file
    session._history_changed = lambda: setattr(
            input_buffer, 'history', session._new_history())
    input_control = BufferControl(
            buffer=input_buffer,
            # A pending secret gate (the trust passphrase) masks the
            # input line
            input_processors=[DynamicProcessor(
                    lambda: (password if session._gate is not None
                             and session._gate['secret'] else None))])

    # The input line is gone while a run is active — the REPL's
    # blocking feel — and back for a question pending mid-run
    input_row = ConditionalContainer(
            VSplit([
                    Window(FormattedTextControl(
                            lambda: session._prompt_marker() + ' '),
                           dont_extend_width=True),
                    Window(input_control),
            ], height=1),
            filter=Condition(lambda: session.active_run is None
                             or session._gate is not None))

    panes = HSplit([
            input_row,
            Window(FormattedTextControl(lambda: _toolbar(session)),
                   height=1, style='class:status'),
    ])
    layout = Layout(
            FloatContainer(
                    content=panes,
                    floats=[Float(xcursor=True, ycursor=True,
                                  content=CompletionsMenu())]),
            focused_element=input_buffer)
    return Application(
            layout=layout, key_bindings=bindings, full_screen=False,
            # The transcript is the printed lines above; the chrome
            # leaves with the session
            erase_when_done=True,
            style=Style.from_dict({'status': 'reverse'}))


def run_shell(session, app=None):
    """Run the interpreter with the prompt_toolkit chrome: the status
    toolbar under the input line, the short prompt. A prebuilt app
    is the tests' seam."""
    try:
        import prompt_toolkit  # noqa: F401
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        # No library, no chrome; the plain reader carries the session
        return run_interactive_plain(session)
    if app is None:
        app = _build_shell(session)
    # The toolbar invalidations land on the app
    session._app = app
    print(_banner(session), file=session._err)
    # The shell streams the watch in the background: a reader thread
    # on its own connection, the lines printed above the prompt
    session.background_watch = True
    # The status poller: the runs started elsewhere ride the toolbar
    watcher = session.client.clone()
    poll_stop = threading.Event()
    poller = threading.Thread(
            target=session._poll_foreign_runs,
            args=(watcher, poll_stop), daemon=True)
    poller.start()
    with patch_stdout():
        # The session's channels were captured at construction time;
        # re-bind them so the prints inside the loop reach the proxy
        original_out, original_err = session._out, session._err
        session._out, session._err = sys.stdout, sys.stderr
        try:
            app.run()
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            poll_stop.set()
            watcher.close()
            session.stop_watch()
            session._out, session._err = original_out, original_err
    return 0
