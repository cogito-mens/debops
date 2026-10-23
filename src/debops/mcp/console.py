# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The console front end: the interpreter as a fullscreen cockpit.

The same engine as the plain REPL and the shell, with the screen
divided into regions: the scrollback of the session, the input line,
a status bar (the place and the trust state), and a side pane (F2
cycles: off, the directory tree of the working directory, the
activity feed of the watch). Commands run on a worker thread, so the
interface stays alive during a run; questions (the confirmation
gates, the passphrase prompt) are answered on the input line, Escape
declines. Without prompt_toolkit the console degrades to the plain
reader.
"""

import threading
import traceback

from .repl import (Repl, ReplError, _banner, _execute_line,
                   run_interactive_plain)

# The scrollback keeps this many lines; the activity pane this many
_SCROLLBACK_LINES = 5000
_ACTIVITY_LINES = 500


class _BufferWriter(object):
    """A file-like sink appending printed text to a prompt_toolkit
    buffer (the scrollback panes of the console)."""

    def __init__(self, session, buffer):
        self._session = session
        self._buffer = buffer

    def write(self, text):
        self._buffer.insert_text(text)
        self._session._invalidate()

    def flush(self):
        pass


class ConsoleRepl(Repl):
    """The fullscreen session: the scrollback, the side pane and the
    input gate live here; the prompt_toolkit wiring lives in
    run_console(). The buffers attach when the interface is built,
    so the session works headless (the tests drive it directly)."""

    HELP_EXTRA = {'panel [off|tree|activity]': 'the side pane '
                                               '(F2 cycles)'}

    _PANES = ('off', 'tree', 'activity')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pane = 'off'
        self._tree_lines = []
        self._gate = None
        self._app = None
        # The prompt_toolkit buffers, attached by _build_app
        self._scroll_buffer = None
        self._activity_buffer = None
        self._input_buffer = None

    # -- the scrollback plumbing -------------------------------------

    def _invalidate(self):
        app = self._app
        if app is not None:
            app.invalidate()

    def _watch_line(self, line):
        buffer = self._activity_buffer
        if buffer is None:
            super()._watch_line(line)
            return
        buffer.insert_text(line + '\n')
        # The pane keeps the tail of the stream
        lines = buffer.text.split('\n')
        if len(lines) > _ACTIVITY_LINES:
            buffer.text = '\n'.join(lines[-_ACTIVITY_LINES:])
            buffer.cursor_position = len(buffer.text)
        self._invalidate()

    # -- the input gate (questions answered on the input line) -------

    def _gate_ask(self, message, secret=False):
        """Ask in the scrollback; the input line answers, Escape
        declines. Runs on the worker thread — never the interface
        thread, which has to stay free to do the answering."""
        gate = {'secret': secret, 'answered': threading.Event(),
                'value': None}
        self._gate = gate
        self._echo(message + '\n')
        gate['answered'].wait()
        self._gate = None
        self._invalidate()
        return gate['value']

    def _gate_answer(self, line):
        """The input line answering a pending question."""
        gate = self._gate
        if gate is None:
            return False
        if gate['secret']:
            gate['value'] = line
            self._echo('\n')
        else:
            gate['value'] = line.strip().lower() in ('y', 'yes')
            self._echo(line + '\n')
        gate['answered'].set()
        return True

    def _gate_decline(self):
        """Escape declines the pending question."""
        gate = self._gate
        if gate is None:
            return False
        gate['value'] = '' if gate['secret'] else False
        self._echo('\n')
        gate['answered'].set()
        return True

    def _ask(self, message):
        return bool(self._gate_ask(message))

    def _ask_passphrase(self):
        phrase = self._gate_ask('the trust passphrase: ', secret=True)
        if not phrase:
            raise ReplError('cancelled')
        return phrase

    # -- the command submission ---------------------------------------

    def _echo(self, text):
        if self._scroll_buffer is not None:
            self._scroll_buffer.insert_text(text)
            # The scrollback keeps the tail of the session
            lines = self._scroll_buffer.text.split('\n')
            if len(lines) > _SCROLLBACK_LINES:
                self._scroll_buffer.text = \
                    '\n'.join(lines[-_SCROLLBACK_LINES:])
                self._scroll_buffer.cursor_position = \
                    len(self._scroll_buffer.text)
            self._invalidate()

    def submit(self, line):
        """One input line: an answer when a question is pending, a
        command otherwise (one at a time)."""
        if self._gate is not None:
            self._gate_answer(line)
            return
        if self._busy:
            self._echo('a command is running; Ctrl-C drops its '
                       'connection\n')
            return
        if not line.strip():
            return
        self._busy = True
        thread = threading.Thread(target=self._execute_guarded,
                                  args=(line,), daemon=True)
        thread.start()

    def _prompt_marker(self):
        """The input-line marker: '>' normally, '!' while elevated.
        The live prompt and the scrollback echo share it."""
        return '!>' if self.elevated else '>'

    def _execute_guarded(self, line):
        """Run one command on the worker thread; the interface stays
        alive and repaints as the answer lands. An unexpected error
        lands in the scrollback — a traceback on the raw terminal
        would corrupt the display and cost the session its worker."""
        self._echo(self._prompt_marker() + ' ' + line + '\n')
        try:
            done = _execute_line(self, line)
        except KeyboardInterrupt:
            done = False
        except Exception as errmsg:
            self._echo('error: internal: {}\n'.format(errmsg))
            self._echo(traceback.format_exc())
            done = False
        finally:
            self._busy = False
            if self.pane == 'tree':
                self._refresh_tree()
            self._invalidate()
        if done:
            app = self._app
            if app is not None:
                app.exit()

    # -- the side pane -------------------------------------------------

    def cmd_panel(self, args):
        """The side pane: off, the directory tree of the working
        directory, or the activity feed of the watch."""
        if len(args) > 1 or (args and args[0] not in self._PANES):
            raise ReplError('usage: panel [off|tree|activity]')
        self.pane = args[0] if args else 'off'
        if self.pane == 'tree':
            self._refresh_tree()
        self._invalidate()

    def cycle_pane(self):
        """The F2 cycle: off -> tree -> activity -> off."""
        index = (self._PANES.index(self.pane) + 1) % len(self._PANES)
        self.pane = self._PANES[index]
        if self.pane == 'tree' and not self._busy:
            self._refresh_tree()
        self._invalidate()

    def _refresh_tree(self):
        """The pane's directory listing of the working directory."""
        try:
            result = self._request(
                    'tools/call',
                    {'name': 'project.list',
                     'arguments': ({'path': self.cwd} if self.cwd
                                   else {})})
        except ReplError:
            return
        if result.get('isError'):
            return
        self._tree_lines = [
                entry.get('name', '') + (
                        '/' if entry.get('type') == 'dir' else '')
                for entry in result.get('structuredContent') or []]

    # -- the editor ----------------------------------------------------

    def _run_editor(self, path):
        """The editor owns the terminal for a while: the interface
        suspends, runs it above the scrollback, and repaints. The
        suspension is scheduled on the application's event loop —
        this method runs on the command's worker thread, which has
        no loop of its own ('ensure_future' would fail there)."""
        app = self._app
        loop = getattr(app, 'loop', None)
        if loop is None or loop.is_closed():
            # No live interface (the headless tests); the editor
            # just runs
            return super()._run_editor(path)
        from prompt_toolkit.application.run_in_terminal import (
                run_in_terminal)
        finished = threading.Event()
        outcome = {}

        def edit():
            try:
                outcome['returncode'] = super(ConsoleRepl,
                                              self)._run_editor(path)
            except Exception as errmsg:
                outcome['error'] = errmsg
            finally:
                finished.set()

        def completed(fut):
            if not fut.cancelled():
                # Retrieve the exception of the suspension machinery
                # itself ('edit' captures its own), so it is not
                # reported as never retrieved
                exc = fut.exception()
                if exc is not None and 'error' not in outcome:
                    outcome['error'] = exc
            finished.set()

        def suspend_and_edit():
            try:
                future = run_in_terminal(edit)
            except Exception as errmsg:
                outcome['error'] = errmsg
                finished.set()
                return
            future.add_done_callback(completed)

        try:
            loop.call_soon_threadsafe(suspend_and_edit)
        except RuntimeError:
            # The loop closed between the check and the call
            return super()._run_editor(path)
        finished.wait()
        error = outcome.get('error')
        if error is not None:
            raise ReplError('the editor could not run: {}'.format(
                    error))
        if 'returncode' not in outcome:
            raise ReplError('the editor never ran')
        return outcome['returncode']


def _build_app(session):
    """The console's prompt_toolkit application, headless-buildable
    for the tests."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (BufferControl,
                                       ConditionalContainer,
                                       DynamicContainer,
                                       FormattedTextControl, HSplit,
                                       Layout, VSplit, Window)
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.layout.processors import (
            DynamicProcessor, PasswordProcessor)
    from prompt_toolkit.styles import Style
    from .repl import _Completer
    from .shell import _toolbar

    scroll_buffer = Buffer()
    activity_buffer = Buffer()
    session._scroll_buffer = scroll_buffer
    session._activity_buffer = activity_buffer
    writer = _BufferWriter(session, scroll_buffer)
    session._out = writer
    session._err = writer

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

    input_buffer = Buffer(
            completer=_Completer(session), multiline=False,
            accept_handler=accept,
            # The history file is shared with the other front ends
            # of this project and view
            history=FileHistory(session.history_file))
    session._input_buffer = input_buffer
    # A view switch moves the recall and the record to the view's
    # own history file
    session._history_changed = lambda: setattr(
            input_buffer, 'history', FileHistory(session.history_file))
    # A pending secret gate (the passphrase prompt) masks the input
    password = PasswordProcessor()
    input_control = BufferControl(
            buffer=input_buffer,
            input_processors=[DynamicProcessor(
                    lambda: (password if session._gate is not None
                             and session._gate['secret'] else None))])

    def pane_container():
        if session.pane == 'tree':
            return Window(FormattedTextControl(
                    lambda: '\n'.join(session._tree_lines)
                    or '(empty directory)'),
                    width=30, style='class:pane')
        if session.pane == 'activity':
            # The feed grows upward from the input line, like the
            # scrollback next to it
            return HSplit([
                    Window(style='class:pane'),
                    Window(BufferControl(buffer=activity_buffer,
                                         focusable=False),
                           dont_extend_height=True,
                           style='class:pane')], width=40)
        return Window()

    bindings = KeyBindings()

    @bindings.add('f2')
    def _cycle(event):
        session.cycle_pane()

    @bindings.add('escape')
    def _escape(event):
        gate = session._gate
        session._gate_decline()
        if gate is None or gate['secret']:
            # Do not leave a half-typed secret on the line
            event.app.current_buffer.reset()

    @bindings.add('c-c')
    def _ctrl_c(event):
        if session._gate is not None:
            session._gate_decline()
        elif session._busy:
            # The command's connection drops; its worker errors out
            session.client.close()
            session._echo('^C\n')
        else:
            event.app.current_buffer.reset()

    # 'dont_extend_height' under an empty filler window: the
    # scrollback grows upward from the input line like a terminal,
    # and claims the whole area once the content overflows
    scroll_window = Window(BufferControl(buffer=scroll_buffer,
                                         focusable=False),
                           wrap_lines=True, dont_extend_height=True)

    @bindings.add('pageup')
    def _pageup(event):
        info = scroll_window.render_info
        height = info.window_height if info is not None else 10
        scroll_window.vertical_scroll = max(
                0, scroll_window.vertical_scroll - height)

    @bindings.add('pagedown')
    def _pagedown(event):
        info = scroll_window.render_info
        height = info.window_height if info is not None else 10
        scroll_window.vertical_scroll += height

    from prompt_toolkit.layout.containers import Float, FloatContainer
    from prompt_toolkit.layout.menus import CompletionsMenu
    panes = HSplit([
            VSplit([
                    HSplit([Window(), scroll_window]),
                    ConditionalContainer(
                            DynamicContainer(pane_container),
                            filter=Condition(
                                    lambda: session.pane != 'off')),
            ]),
            VSplit([
                    Window(FormattedTextControl(
                            lambda: session._prompt_marker() + ' '),
                           dont_extend_width=True),
                    Window(input_control),
            ], height=1),
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
            layout=layout, key_bindings=bindings, full_screen=True,
            style=Style.from_dict({'status': 'reverse',
                                   'pane': 'bg:#444444'}))


def run_console(session):
    """Run the fullscreen session; without prompt_toolkit the plain
    reader carries it."""
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        # No library, no cockpit; the plain reader carries the
        # session
        return run_interactive_plain(session)
    app = _build_app(session)
    session._app = app
    session.background_watch = True
    session._echo(_banner(session) + '\n')
    try:
        app.run()
    finally:
        session.stop_watch()
        session._app = None
    return 0
