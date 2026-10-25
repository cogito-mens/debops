# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The console front end: the interpreter as a fullscreen cockpit.

The layout, top to bottom: the title line (the project's name, a
bang while elevated, the clock), the toggleable tab area (Queue,
Runs, Journal, Docs, Credits — F2-F6 select, F1 the help, Tab and
Shift-Tab cycle), the transcript (the REPL's output and the live
log of a run, a few lines high), the prompt line, and the status
line the shell shares.

The prompt holds the focus by default (project.mcp.console.
prompt_active); Escape leaves it for the tab area, 'i' returns —
and 'C-x o' toggles the focus for the Emacs habit. Without
prompt_toolkit the console degrades to the plain reader.
"""

import collections
import threading
import time

from .repl import (Repl, ReplError, _banner, run_interactive_plain)

# The tabs of the console's area, in order; F2-F6 select them and
# F1 opens the help
TABS = ('queue', 'runs', 'journal', 'docs', 'credits')

_TAB_KEYS = {'f1': 'help', 'f2': 'queue', 'f3': 'runs',
             'f4': 'journal', 'f5': 'docs', 'f6': 'credits'}

_TAB_TITLES = {'help': 'Help', 'queue': 'Queue', 'runs': 'Runs',
               'journal': 'Journal', 'docs': 'Docs',
               'credits': 'Credits'}

_HELP_TEXT = """\
The console keys

  F1          this help (again to close)
  F2-F6       the tabs: Queue, Runs, Journal, Docs, Credits
  Tab, S-Tab  cycle the tabs (when the area has the focus)
  Esc         leave the prompt for the tab area; back again
  i           return to the prompt (the vim habit)
  C-x o       the other window, the Emacs habit
  PgUp/PgDn   scroll the transcript; Home/End its ends
  Ctrl-C      decline a question; cancel the run; clear the line
  Ctrl-D      exit
"""


class _TranscriptWriter(object):
    """The file-like the session's output channels write into: the
    text lands in the console's transcript as lines (ANSI-colored
    as they arrived), bounded; the window renders the tail slice.
    A reader paged up holds its place while the lines grow."""

    def __init__(self, session):
        self._session = session
        self._partial = ''

    def write(self, text):
        if not text:
            return 0
        session = self._session
        parts = (self._partial + text).split('\n')
        # The trailing fragment waits for its newline
        self._partial = parts.pop()
        if not parts:
            return len(text)
        with session._transcript_lock:
            session._console_lines.extend(parts)
            if session._transcript_scroll:
                # A reader in the scrollback keeps the viewport:
                # the scroll counts up as the tail grows
                session._transcript_scroll += len(parts)
        session._invalidate()
        return len(text)

    def flush(self):
        pass


def _title_text(session):
    """The top status line: the project's name (a bang while
    elevated) on the left, the clock on the right."""
    from prompt_toolkit.application import get_app
    title = 'DebOps!' if session.elevated else 'DebOps'
    clock = time.strftime('%H:%M')
    width = get_app().output.get_size().columns
    padding = max(1, width - len(title) - len(clock))
    return title + ' ' * padding + clock


def _tab_bar_text(session):
    """The tab bar: the titles in order, the active one standing
    out, the key hints at the right."""
    from prompt_toolkit.application import get_app
    titles = ['help'] + list(TABS)
    fragments = []
    for name in titles:
        style = 'class:tabs-active' if name == session.console_tab \
                else 'class:tabs'
        fragments.append((style, ' {} '.format(_TAB_TITLES[name])))
        fragments.append(('class:tabs', '│'))
    hints = 'i: prompt │ Esc: back'
    used = sum(len(fragment[1]) for fragment in fragments)
    width = get_app().output.get_size().columns
    # A narrow bar drops the hints before it clips the titles
    if used + len(hints) + 1 <= width:
        fragments.append(('class:tabs',
                          ' ' * (width - used - len(hints)) + hints))
    return fragments


def _transcript_text(session):
    """The transcript's visible slice: the tail of the lines, held
    up by the scroll when the reader pages back. A short content
    hugs the bottom of the window, like a terminal's."""
    with session._transcript_lock:
        total = len(session._console_lines)
        end = total - session._transcript_scroll
        start = max(0, end - session.console_repl_lines)
        slice_ = list(session._console_lines)[start:end]
    missing = session.console_repl_lines - len(slice_)
    if missing > 0 and not session._transcript_scroll:
        slice_ = [''] * missing + slice_
    return '\n'.join(slice_)


class ConsoleRepl(Repl):
    """The fullscreen session: the transcript, the tab area and the
    focus dance live here; the prompt_toolkit wiring lives in
    run_console(). The session works headless (the tests drive it
    directly); the buffers attach when the interface is built."""

    def _prompt_marker(self):
        """The input-line chip: the state flags ('>' normally, '!'
        while elevated, '#' for root, bracketed when confined). The
        live prompt and the transcript echo share it."""
        return self._console_marker()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        config = self.console_config
        self.console_repl_lines = max(3, int(config.get(
                'repl_lines', 5)))
        prompt_active = config.get('prompt_active', True)
        if isinstance(prompt_active, str):
            prompt_active = prompt_active.strip().lower() not in (
                    '0', 'false', 'no', 'off')
        self.console_prompt_active = bool(prompt_active)
        self.console_keys = str(config.get('keys', 'vim'))
        # The active tab ('help', 'queue', ...); None: the area is
        # closed
        self.console_tab = None
        # Where the focus lives: the prompt or the tab area
        self.console_focus = 'prompt'
        # The tail and the bottom status line, like the shell's
        self.run_display = 'shell'
        # The questions are answered on the input line
        self.gate_input = True
        # The transcript: the bounded output lines and the window's
        # scroll offset (the lines held up from the tail)
        self._console_lines = collections.deque(maxlen=2000)
        self._transcript_lock = threading.Lock()
        self._transcript_scroll = 0
        # The tab contents and the prompt buffer attach at build
        self._input_buffer = None
        self._tab_buffers = {}

    # -- the transcript plumbing -------------------------------------

    def _color_output(self):
        # The transcript renders the ANSI itself; the console always
        # drives a terminal
        return True

    def _print_output_text(self, text):
        # The run's output lands in the transcript, not above the
        # fullscreen application
        self._out.write(text)

    def _scroll_transcript(self, lines):
        """Scroll the transcript's window by whole lines; zero is
        the tail (the window follows the output)."""
        with self._transcript_lock:
            limit = max(0, len(self._console_lines)
                        - self.console_repl_lines)
            self._transcript_scroll = min(
                    limit, max(0, self._transcript_scroll + lines))
        self._invalidate()

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

    # -- the focus dance -----------------------------------------------

    def _focus_area(self, tab=None):
        """Move the focus to the tab area, opening it first when it
        is closed."""
        if tab is not None:
            self.console_tab = tab
        if self.console_tab is None:
            self.console_tab = 'queue'
        self.console_focus = 'area'
        app = self._app
        if app is not None:
            app.layout.focus(self._tab_buffers[self.console_tab])
        self._invalidate()

    def _focus_prompt(self):
        """Move the focus back to the prompt line."""
        self.console_focus = 'prompt'
        app = self._app
        if app is not None:
            app.layout.focus(self._input_buffer)
        self._invalidate()

    def _toggle_tab(self, tab):
        """An F-key: open the area on its tab; the open tab's key
        closes the area and returns to the prompt."""
        if self.console_tab == tab and self.console_focus == 'area':
            self.console_tab = None
            self._focus_prompt()
        else:
            self._focus_area(tab)

    def _cycle_tab(self, direction):
        """Tab and Shift-Tab in the area: the tabs in a ring."""
        order = list(TABS)
        index = order.index(self.console_tab)
        self.console_tab = order[(index + direction) % len(order)]
        app = self._app
        if app is not None:
            app.layout.focus(self._tab_buffers[self.console_tab])
        self._invalidate()


def _bind_suspend(bindings, session):
    """The console's Ctrl-Z: UNIX job control when the account is
    free, a note when it is confined (prompt_toolkit's default
    binding would insert a literal '^Z' into the input line, which
    neither wants)."""

    @bindings.add('c-z')
    def _suspend(event):
        if session.confined:
            session._note('a confined session does not background')
        else:
            event.app.suspend_to_background()


def _build_app(session):
    """The console's prompt_toolkit application, headless-buildable
    for the tests."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.bindings.named_commands import (
            get_by_name)
    from prompt_toolkit.layout import (BufferControl,
                                       ConditionalContainer,
                                       DynamicContainer,
                                       FormattedTextControl, HSplit,
                                       Layout, VSplit, Window)
    from prompt_toolkit.layout.containers import Float, FloatContainer
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.layout.processors import (
            DynamicProcessor, PasswordProcessor)
    from prompt_toolkit.styles import Style
    from .repl import _Completer
    from .shell import _toolbar

    writer = _TranscriptWriter(session)
    session._out = writer
    session._err = writer

    # The tab contents: the placeholders for now, each its own
    # buffer so the area's focus has somewhere to sit
    tab_buffers = {}
    tab_containers = {}
    for name in ('help',) + TABS:
        buffer = Buffer()
        buffer.text = (_HELP_TEXT if name == 'help'
                       else '({}: soon)'.format(_TAB_TITLES[name]))
        tab_buffers[name] = buffer
        tab_containers[name] = Window(
                BufferControl(buffer=buffer, focusable=True),
                wrap_lines=True)
    session._tab_buffers = tab_buffers
    if not session.console_prompt_active and session.console_tab is None:
        # The configurable default: the console starts on the Queue
        session.console_tab = 'queue'

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
            history=session._new_history())
    session._input_buffer = input_buffer
    # A view switch moves the recall and the record to the view's
    # own history file
    session._history_changed = lambda: setattr(
            input_buffer, 'history', session._new_history())
    # A pending secret gate (the passphrase prompt) masks the input
    password = PasswordProcessor()
    input_control = BufferControl(
            buffer=input_buffer,
            input_processors=[DynamicProcessor(
                    lambda: (password if session._gate is not None
                             and session._gate['secret'] else None))])

    def tab_area_content():
        # The hidden area has no tab to render; the layout walks
        # the container regardless
        if session.console_tab is None:
            return Window()
        return tab_containers[session.console_tab]

    bindings = KeyBindings()
    _bind_suspend(bindings, session)

    area_live = Condition(lambda: session.console_focus == 'area')

    @bindings.add('escape')
    def _escape(event):
        if session._gate is not None:
            session._gate_decline()
            if session._gate is None:
                # The secret never lingers on the line
                event.app.current_buffer.reset()
            return
        # The focus dance: the prompt leaves for the tab area and
        # back
        if session.console_focus == 'prompt':
            session._focus_area()
        else:
            session._focus_prompt()

    @bindings.add('i', filter=area_live)
    def _insert(event):
        # The vim habit: back to the prompt
        session._focus_prompt()

    @bindings.add('c-x', 'o')
    def _other_window(event):
        # The Emacs habit: the other window
        if session.console_focus == 'prompt':
            session._focus_area()
        else:
            session._focus_prompt()

    for _key, _tab in _TAB_KEYS.items():
        @bindings.add(_key)
        def _tab_key(event, tab=_tab):
            session._toggle_tab(tab)

    @bindings.add('tab', filter=area_live)
    def _tab_next(event):
        session._cycle_tab(1)

    @bindings.add('s-tab', filter=area_live)
    def _tab_prev(event):
        session._cycle_tab(-1)

    @bindings.add('pageup')
    @bindings.add('s-pageup')
    def _pageup(event):
        session._scroll_transcript(session.console_repl_lines)

    @bindings.add('pagedown')
    @bindings.add('s-pagedown')
    def _pagedown(event):
        session._scroll_transcript(-session.console_repl_lines)

    @bindings.add('home')
    @bindings.add('s-home')
    def _scroll_top(event):
        with session._transcript_lock:
            session._transcript_scroll = max(
                    0, len(session._console_lines)
                    - session.console_repl_lines)
        session._invalidate()

    @bindings.add('end')
    @bindings.add('s-end')
    def _scroll_bottom(event):
        session._scroll_transcript(-10 ** 9)

    @bindings.add('c-home')
    def _line_home(event):
        # Home/End moved to the scrollback; the line's start and end
        # keep a chord of their own
        get_by_name('beginning-of-line').call(event)

    @bindings.add('c-end')
    def _line_end(event):
        get_by_name('end-of-line').call(event)

    @bindings.add('c-c')
    def _ctrl_c(event):
        if session._gate is not None:
            session._gate_decline()
        elif session.active_run is not None:
            # The signal passes along to the daemon, like in the
            # other front ends: the run stops, the console keeps
            # running
            session._cancel_run()
        elif (session._foreign_run is not None
                and session._foreign_run.get('taskId') is not None):
            # A run started elsewhere stops from here too — the
            # daemon asks the cancelling client for the trust
            session._cancel_run(session._foreign_run['taskId'])
        elif session._follow_client is not None:
            # A 'log --follow' tail ends; the run is untouched
            session._follow_client.close()
            session._echo('^C (the log tail)\n')
        elif session._busy:
            # The command's connection drops; its worker errors out
            session.client.close()
            session._echo('^C\n')
        else:
            event.app.current_buffer.reset()

    panes = HSplit([
            # The title line: the project's name left, the clock
            # right
            Window(FormattedTextControl(lambda: _title_text(session),
                                        focusable=False),
                   height=1, style='class:status'),
            # The tab area, hidden until a key opens it; it claims
            # the free space with the filler
            ConditionalContainer(
                    HSplit([
                            Window(FormattedTextControl(
                                    lambda: _tab_bar_text(session),
                                    focusable=False),
                                   height=1, style='class:tabs'),
                            DynamicContainer(tab_area_content),
                    ]),
                    filter=Condition(
                            lambda: session.console_tab is not None)),
            # The filler takes the slack, so the transcript, the
            # prompt and the status line hug the bottom of the
            # screen like a terminal's
            Window(),
            # The transcript: the REPL's output and the run's live
            # log, the tail few lines
            Window(FormattedTextControl(
                    lambda: ANSI(_transcript_text(session)),
                    focusable=False),
                   height=session.console_repl_lines,
                   dont_extend_height=True, wrap_lines=True),
            # The prompt line
            VSplit([
                    Window(FormattedTextControl(
                            lambda: session._prompt_marker() + ' '),
                           dont_extend_width=True),
                    Window(input_control),
            ], height=1),
            # The status line the shell shares
            Window(FormattedTextControl(lambda: _toolbar(session)),
                   height=1, style='class:status'),
    ])
    layout = Layout(
            FloatContainer(
                    content=panes,
                    floats=[Float(xcursor=True, ycursor=True,
                                  content=CompletionsMenu())]),
            focused_element=(input_buffer
                             if session.console_prompt_active
                             else tab_buffers[session.console_tab]))
    return Application(
            layout=layout, key_bindings=bindings, full_screen=True,
            style=Style.from_dict({'status': 'reverse',
                                   'tabs': '',
                                   'tabs-active': 'reverse bold'}))


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
    session._echo(_banner(session) + '\n')
    # The status poller: the runs started elsewhere ride the status
    # line, and its ticks keep the clock honest
    watcher = session.client.clone()
    stop = threading.Event()
    poller = threading.Thread(
            target=session._poll_foreign_runs,
            args=(watcher, stop), daemon=True)
    poller.start()
    try:
        app.run()
    finally:
        stop.set()
        watcher.close()
        session._app = None
    return 0
