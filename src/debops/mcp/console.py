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
import os
import subprocess
import threading
import time

from .repl import (Repl, ReplError, _banner, _run_verb_of,
                   run_interactive_plain)


def _format_run_row(entry):
    """One run in the Runs tab: the id, the mode and the playbooks,
    the exit and the duration."""
    mode = entry.get('mode', '?')
    playbooks = ' '.join(entry.get('playbooks') or ['?'])
    exit_code = entry.get('exitCode')
    verdict = ('exit {}'.format(exit_code)
               if exit_code is not None else
               entry.get('status', '?'))
    duration = ''
    started, finished = entry.get('startedAt'), entry.get('finishedAt')
    if started and finished:
        try:
            from datetime import datetime
            began = datetime.fromisoformat(
                    started.replace('Z', '+00:00'))
            ended = datetime.fromisoformat(
                    finished.replace('Z', '+00:00'))
            duration = ', {} s'.format(max(0, int(
                    (ended - began).total_seconds())))
        except ValueError:
            pass
    return '{}: {} {} — {}{}'.format(entry.get('id'), mode, playbooks,
                                     verdict, duration)


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

  F1, ?       this help (again to close)
  F2-F6       the tabs: Queue, Runs, Journal, Docs, Credits
  q           close the detail; quit from the tab area
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
        self._tab_controls = {}
        self._tab_containers = {}
        # The Queue tab: the last fetched state, the selectable rows
        # of the render, the cursor, the open job's detail
        self._queue_batches = []
        self._queue_started = None
        self._queue_rows = []
        self._queue_cursor = 0
        self._queue_detail = None
        self._queue_collapsed = set()
        self._queue_refreshing = threading.Lock()
        # The Runs tab: the finished executions and the open log
        self._runs_entries = []
        self._runs_cursor = 0
        self._runs_detail = None   # {'id': N, 'lines': [...]}
        self._runs_scroll = 0
        self._runs_refreshing = threading.Lock()
        # The Journal tab: the journalctl feed, a bounded
        # scrollback, the source switch ('mcp': the client and the
        # daemon, 'user', 'system')
        self._journal_lines = collections.deque(maxlen=2000)
        self._journal_scroll = 0
        self._journal_proc = None
        self._journal_note = None
        self._journal_source = 'mcp'
        # The Credits tab: the package's credits file, scrolled
        self._credits_scroll = 0

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

    # -- the Queue tab ------------------------------------------------

    def _poll_foreign_run_once(self, watcher):
        super()._poll_foreign_run_once(watcher)
        if self.console_tab == 'queue':
            self._queue_refresh(watcher)
        elif self.console_tab == 'runs':
            self._runs_refresh(watcher)

    def _queue_refresh(self, connection):
        """Fetch the queue state for the Queue tab on a side
        connection (the poller's, or a one-shot one): the batches
        with their jobs. Skips when a refresh is in flight."""
        if not self._queue_refreshing.acquire(blocking=False):
            return
        try:
            data = {}
            batches = []
            try:
                answer = connection.request(
                        'tools/call',
                        {'name': 'queue.list', 'arguments': {}})
                result = answer.get('result', {})
                if result.get('isError'):
                    return
                data = result.get('structuredContent') or {}
                for entry in data.get('batches') or []:
                    detail = connection.request(
                            'tools/call',
                            {'name': 'batch.info',
                             'arguments': {'name': entry['name']}})
                    batch = (detail.get('result', {})
                             .get('structuredContent')
                             or {}).get('batch')
                    if batch is not None:
                        batches.append(batch)
            except (AttributeError, ConnectionError, EOFError,
                    KeyError, OSError, ReplError, TypeError):
                # A dead daemon or a malformed answer: the tab keeps
                # the last good state
                return
            self._queue_started = data.get('started')
            self._queue_batches = batches
            self._queue_rows = self._queue_build_rows()
            self._queue_cursor = min(self._queue_cursor,
                                     max(0, len(self._queue_rows) - 1))
            self._invalidate()
        finally:
            self._queue_refreshing.release()

    def _queue_refresh_async(self):
        """A one-shot refresh of the Queue tab on its own thread
        (the open, a removal) — the interface never blocks on it."""
        def refresh():
            try:
                side = self.client.clone()
                self._queue_refresh(side)
                side.close()
            except (AttributeError, ConnectionError, EOFError,
                    OSError):
                pass
        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()

    def _queue_build_rows(self):
        """The selectable rows of the Queue tab: each batch, then
        its jobs (a collapsed batch hides them)."""
        rows = []
        for batch in self._queue_batches:
            rows.append(('batch', batch))
            if batch['name'] in self._queue_collapsed:
                continue
            for job in batch.get('jobs') or []:
                rows.append(('job', job))
        return rows

    def _queue_move(self, direction):
        """The cursor through the selectable rows."""
        if not self._queue_rows:
            return
        self._queue_cursor = min(
                len(self._queue_rows) - 1,
                max(0, self._queue_cursor + direction))
        self._invalidate()

    def _queue_open(self):
        """Enter on a row: the job's details; a batch toggles its
        jobs away."""
        if not self._queue_rows:
            return
        kind, payload = self._queue_rows[self._queue_cursor]
        if kind == 'job':
            self._queue_detail = payload
        else:
            name = payload['name']
            if name in self._queue_collapsed:
                self._queue_collapsed.discard(name)
            else:
                self._queue_collapsed.add(name)
            self._queue_rows = self._queue_build_rows()
            self._queue_cursor = min(self._queue_cursor,
                                     max(0, len(self._queue_rows) - 1))
        self._invalidate()

    def _queue_remove(self):
        """Remove the selected entry on a worker thread: a job
        prunes from itself onward, a batch drops with its queued
        jobs (a running job finishes); the daemon's refusals land
        as notes."""
        if not self._queue_rows:
            return
        kind, payload = self._queue_rows[self._queue_cursor]
        if kind == 'job':
            tool, arguments = 'job.prune', {'id': payload['id']}
        else:
            tool, arguments = 'batch.remove', {'name': payload['name']}

        def call():
            try:
                side = self.client.clone()
                try:
                    answer = side.request(
                            'tools/call',
                            {'name': tool, 'arguments': arguments})
                    result = answer.get('result', {})
                    content = result.get('content') or []
                    if content:
                        self._note(content[0].get('text', ''))
                    # The tab reflects the change at once
                    self._queue_refresh(side)
                finally:
                    side.close()
            except (AttributeError, ConnectionError, EOFError,
                    OSError) as errmsg:
                self._note('the removal failed: {}'.format(errmsg))

        thread = threading.Thread(target=call, daemon=True)
        thread.start()

    # -- the Runs tab --------------------------------------------------

    def _runs_refresh(self, connection):
        """Fetch the finished executions for the Runs tab on a side
        connection. Skips when a refresh is in flight."""
        if not self._runs_refreshing.acquire(blocking=False):
            return
        try:
            try:
                answer = connection.request(
                        'tools/call',
                        {'name': 'execution.list', 'arguments': {}})
                result = answer.get('result', {})
                if result.get('isError'):
                    return
                entries = result.get('structuredContent') or []
            except (AttributeError, ConnectionError, EOFError,
                    KeyError, OSError, ReplError, TypeError):
                return
            if isinstance(entries, dict):
                entries = entries.get('executions') or []
            self._runs_entries = entries
            self._runs_cursor = min(self._runs_cursor,
                                    max(0, len(entries) - 1))
            self._invalidate()
        finally:
            self._runs_refreshing.release()

    def _runs_refresh_async(self):
        """A one-shot refresh of the Runs tab on its own thread."""
        def refresh():
            try:
                side = self.client.clone()
                self._runs_refresh(side)
                side.close()
            except (AttributeError, ConnectionError, EOFError,
                    OSError):
                pass
        thread = threading.Thread(target=refresh, daemon=True)
        thread.start()

    def _runs_move(self, direction):
        if not self._runs_entries:
            return
        self._runs_cursor = min(
                len(self._runs_entries) - 1,
                max(0, self._runs_cursor + direction))
        self._invalidate()

    def _runs_open(self):
        """Enter on a run: the log, colorized, in the area's whole
        height. The log arrives through the daemon's log.reader —
        the cache file's path stays the server's business."""
        if not self._runs_entries:
            return
        entry = self._runs_entries[self._runs_cursor]
        from .repl import ANSI_THEME, AnsibleColorizer

        def fetch():
            try:
                side = self.client.clone()
                answer = side.request(
                        'tools/call',
                        {'name': 'log.reader',
                         'arguments': {'id': entry['id']}})
                side.close()
            except (AttributeError, ConnectionError, EOFError,
                    OSError) as errmsg:
                self._note('the log cannot be read: {}'.format(errmsg))
                return
            result = answer.get('result', {})
            content = result.get('content') or []
            if result.get('isError') or not content:
                self._note(content[0].get('text', 'cannot read the '
                                          'log') if content else
                           'cannot read the log')
                return
            colorizer = AnsibleColorizer(self.theme or ANSI_THEME)
            raw = content[0].get('text', '')
            lines = [colorizer.colorize(line)
                     for line in raw.splitlines()]
            self._runs_detail = {'id': entry.get('id'),
                                 'lines': lines}
            self._runs_scroll = 0
            self._invalidate()

        thread = threading.Thread(target=fetch, daemon=True)
        thread.start()

    def _runs_close(self):
        self._runs_detail = None
        self._runs_scroll = 0
        self._invalidate()

    def _runs_scroll_by(self, lines):
        """Scroll the open log by whole lines, from its top."""
        if self._runs_detail is None:
            return
        height = self._area_height()
        limit = max(0, len(self._runs_detail['lines']) - height)
        self._runs_scroll = min(limit,
                                max(0, self._runs_scroll + lines))
        self._invalidate()

    def _area_height(self):
        """The tab area's height in rows: the terminal less the
        chrome (the title, the tab bar, the transcript, the prompt,
        the status line)."""
        try:
            from prompt_toolkit.application import get_app
            rows = get_app().output.get_size().rows
        except Exception:
            return 10
        return max(1, rows - self.console_repl_lines - 4)

    # -- the Journal tab ----------------------------------------------

    # The feed's sources: the MCP processes (the client and the
    # daemon, matched by pid), the local user's journal, the system
    # journal
    _JOURNAL_SOURCES = ('mcp', 'user', 'system')

    def _journal_command(self):
        """The journalctl invocation of the current source."""
        command = ['journalctl', '--follow', '--lines', '50',
                   '--output=short']
        if self._journal_source != 'system':
            command.append('--user')
        if self._journal_source == 'mcp':
            pids = [os.getpid()]
            socket_path = getattr(self.client, '_path', None)
            if socket_path is not None:
                from .client import socket_pid_path
                try:
                    with open(socket_pid_path(socket_path)) as fp:
                        pids.append(int(fp.read().strip()))
                except (OSError, ValueError):
                    pass
            command.extend('_PID={}'.format(pid) for pid in pids)
        return command

    def _journal_start(self):
        """Stream the journal into the Journal tab; journalctl
        missing or a dead feed lands as a note in the tab, not a
        crash."""
        if self._journal_proc is not None:
            return
        self._journal_note = None
        try:
            proc = subprocess.Popen(self._journal_command(),
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True, errors='replace')
        except OSError as errmsg:
            self._journal_note = ('the journal feed needs '
                                  'journalctl: {}'.format(errmsg))
            self._invalidate()
            return
        self._journal_proc = proc
        thread = threading.Thread(target=self._journal_loop,
                                  args=(proc,), daemon=True)
        thread.start()

    def _journal_loop(self, proc):
        """The feed's reader thread: the lines land in the tab's
        scrollback; a reader paged up holds the viewport."""
        try:
            for line in proc.stdout:
                with self._transcript_lock:
                    self._journal_lines.append(line.rstrip('\n'))
                    if self._journal_scroll:
                        self._journal_scroll += 1
                self._invalidate()
        except (OSError, ValueError):
            pass
        proc.wait()
        if self._journal_proc is proc:
            # A spontaneous death; a deliberate stop or a source
            # switch replaced this feed already and stays quiet
            self._journal_proc = None
            self._journal_note = ('the journal feed ended (the '
                                  'process died; the scrollback '
                                  'stays)')
            self._invalidate()

    def _journal_stop(self):
        """End the feed: the console exits, the follower dies
        with it."""
        proc = self._journal_proc
        if proc is not None:
            self._journal_proc = None
            try:
                proc.terminate()
            except OSError:
                pass

    def _journal_switch(self, direction=1):
        """Cycle the feed's source: the MCP processes, the user's
        journal, the system's. The feed restarts, the scrollback
        clears."""
        sources = self._JOURNAL_SOURCES
        index = sources.index(self._journal_source)
        self._journal_source = sources[
                (index + direction) % len(sources)]
        self._journal_stop()
        with self._transcript_lock:
            self._journal_lines.clear()
            self._journal_scroll = 0
        self._journal_start()

    def _journal_scroll_by(self, lines):
        """Scroll the journal view by whole lines; zero is the
        live tail."""
        height = self._area_height()
        with self._transcript_lock:
            limit = max(0, len(self._journal_lines) - height)
            self._journal_scroll = min(
                    limit, max(0, self._journal_scroll + lines))
        self._invalidate()

    def _credits_scroll_by(self, lines):
        """Scroll the credits by whole lines, from their top."""
        # The render re-reads the file's lines; the scroll only
        # needs a clamp against the current content at render time,
        # so here the offset just stays non-negative
        self._credits_scroll = max(0, self._credits_scroll + lines)
        self._invalidate()

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
            app.layout.focus(self._tab_controls[self.console_tab])
        if self.console_tab == 'queue':
            # The open is fresh data
            self._queue_refresh_async()
        elif self.console_tab == 'runs':
            self._runs_refresh_async()
        elif self.console_tab == 'journal':
            self._journal_start()
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
            app.layout.focus(self._tab_controls[self.console_tab])
        if self.console_tab == 'queue':
            self._queue_refresh_async()
        elif self.console_tab == 'runs':
            self._runs_refresh_async()
        elif self.console_tab == 'journal':
            self._journal_start()
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


# -- the Queue tab ---------------------------------------------------


def _queue_tab_text(session):
    """The Queue tab: the dispatch state, the active run with its
    tally and its current line, then the batches and their jobs.
    The cursor's row stands out."""
    fragments = []

    def line(text, style='class:tabs'):
        fragments.append((style, text + '\n'))

    started = session._queue_started
    line('Queue: {}'.format('dispatching' if started else 'parked'
                            if started is not None else '…'))
    # The active run — ours or one watched from elsewhere
    run_line = None
    task_line = None
    if session.active_run is not None:
        run = session.active_run['run']
        run_line = session._run_segment(
                _run_verb_of(run).lower(), run.get('playbooks'),
                run.get('taskIndex'), run.get('taskCount'),
                run.get('taskCounts'))
        task_line = run.get('currentTask')
    elif session._foreign_run is not None:
        foreign = session._foreign_run
        run_line = session._run_segment(
                _run_verb_of(foreign).lower(), foreign.get('playbooks'),
                foreign.get('tasksDone'), foreign.get('tasksTotal'),
                foreign.get('taskCounts'))
        task_line = foreign.get('currentPlaybook')
    if run_line is not None:
        line('Running: {}'.format(run_line))
        if task_line:
            line('    {}'.format(task_line))
    if not session._queue_batches:
        line('(no batches)')
    for index, (kind, payload) in enumerate(session._queue_rows):
        style = 'reverse' if index == session._queue_cursor \
                else 'class:tabs'
        if kind == 'batch':
            marker = ('▸' if payload['name'] in session._queue_collapsed
                      else '▾')
            line('{} {} — {} job(s), ~{} s ({}{})'.format(
                    marker, payload['name'], len(payload.get('jobs')
                                                 or []),
                    payload.get('estimateSeconds', 0),
                    payload.get('state', '?'),
                    ', pending review' if payload.get('pending')
                    else ''), style)
        else:
            line('    {}: {} {} [{}]{}'.format(
                    payload['id'], payload['mode'],
                    ' '.join(payload['targets']), payload['state'],
                    ' — {}'.format(payload['error'])
                    if payload.get('error') else ''), style)
    return fragments


def _queue_detail_text(session):
    """The open job's details, filling the Queue tab."""
    job = session._queue_detail
    lines = ['{}: {} {}'.format(job['id'], job['mode'],
                                ' '.join(job['targets'])),
             'batch: {}    state: {}    position: {}'.format(
                     job['batch'], job['state'],
                     job.get('position', '—')),
             'created: {}'.format(job.get('createdAt', '—'))]
    options = job.get('options') or {}
    if options:
        lines.append('options: {}'.format(' '.join(
                '{}={}'.format(key, value)
                for key, value in sorted(options.items()))))
    tally = job.get('tally')
    if tally:
        lines.append('tally: {} tasks x {} hosts, ~{} s'.format(
                tally.get('taskCount'), tally.get('hostCount'),
                tally.get('estimateSeconds')))
    if job.get('executionId') is not None:
        lines.append('execution: {}'.format(job['executionId']))
    if job.get('error'):
        lines.append('error: {}'.format(job['error']))
    lines.append('')
    lines.append('q/Esc: back to the queue')
    return '\n'.join(lines)


def _runs_tab_text(session):
    """The Runs tab: the finished executions, newest at the bottom;
    the cursor's row stands out."""
    fragments = []
    if not session._runs_entries:
        return [('class:tabs', '(no executions recorded)\n')]
    for index, entry in enumerate(session._runs_entries):
        style = 'reverse' if index == session._runs_cursor \
                else 'class:tabs'
        fragments.append((style, _format_run_row(entry) + '\n'))
    return fragments


def _runs_detail_text(session):
    """The open run's log: the area's whole height of it, read
    top-down; the colorizer's ANSI lands as it ran."""
    detail = session._runs_detail
    lines = detail['lines']
    height = session._area_height()
    start = min(session._runs_scroll, max(0, len(lines) - height))
    body = list(lines[start:start + height])
    header = ('execution {} — {} lines │ j/k: scroll │ '
              'PgUp/PgDn: pages │ q/Esc: back\n'
              .format(detail['id'], len(lines)))
    return header + '\n'.join(body)


def _journal_tab_text(session):
    """The Journal tab: the journalctl feed, live; the tail
    follows, a paged reader holds. 's' switches the source."""
    height = session._area_height()
    with session._transcript_lock:
        total = len(session._journal_lines)
        end = total - session._journal_scroll
        start = max(0, end - height)
        body = list(session._journal_lines)[start:end]
    lines = ['journal: {} (s switches: mcp/user/system) │ '
             'j/k, PgUp/PgDn: scroll'.format(session._journal_source)]
    note = session._journal_note
    if note is not None:
        lines.append('({})'.format(note))
    if not body and note is None:
        lines.append('(the journal is quiet)')
    lines.extend(body)
    return '\n'.join(lines)


def _credits_tab_text(session):
    """The Credits tab: the package's credits file, centered on the
    area like a credits roll, scrolled top-down. Generated at
    package build time ('make credits'); a dev checkout without it
    says so."""
    import pkgutil
    try:
        data = pkgutil.get_data('debops', os.path.join(
                '_data', 'debops', 'credits'))
    except OSError:
        data = None
    if data is None:
        return ('(no credits file yet — it is generated at package '
                'build time: make credits)')
    text = data.decode('utf-8', 'replace').rstrip()
    lines = text.splitlines()
    # Centered on the area, like a credits roll
    from prompt_toolkit.application import get_app
    width = get_app().output.get_size().columns
    centered = [' ' * max(0, (width - len(line)) // 2) + line
                for line in lines]
    height = session._area_height()
    start = min(session._credits_scroll,
                max(0, len(centered) - height))
    return '\n'.join(centered[start:start + height])


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

    # The tab contents, each a focusable control (the area's focus
    # target); the Queue tab renders the queue's state
    tab_texts = {
            'help': lambda: _HELP_TEXT,
            'queue': lambda: (_queue_detail_text(session)
                              if session._queue_detail is not None
                              else _queue_tab_text(session)),
            'runs': lambda: (ANSI(_runs_detail_text(session))
                             if session._runs_detail is not None
                             else _runs_tab_text(session)),
            'journal': lambda: _journal_tab_text(session),
            'docs': lambda: '(Docs: the browsable documentation '
                            'arrives here; until then the “doc” verb '
                            'answers it)',
            'credits': lambda: _credits_tab_text(session),
    }
    for name in ('help',) + TABS:
        control = FormattedTextControl(tab_texts[name],
                                       focusable=True)
        session._tab_controls[name] = control
        session._tab_containers[name] = Window(control,
                                               wrap_lines=True)
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
        return session._tab_containers[session.console_tab]

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
        # back; an open detail (a job, a run's log) closes first
        if session.console_focus == 'prompt':
            session._focus_area()
        elif session._queue_detail is not None:
            session._queue_detail = None
            session._invalidate()
        elif session._runs_detail is not None:
            session._runs_close()
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

    in_queue = Condition(lambda: session.console_focus == 'area'
                         and session.console_tab == 'queue')
    detail_open = Condition(lambda: session._queue_detail is not None)

    @bindings.add('j', filter=in_queue & ~detail_open)
    @bindings.add('down', filter=in_queue & ~detail_open)
    def _queue_down(event):
        session._queue_move(1)

    @bindings.add('k', filter=in_queue & ~detail_open)
    @bindings.add('up', filter=in_queue & ~detail_open)
    def _queue_up(event):
        session._queue_move(-1)

    @bindings.add('enter', filter=in_queue)
    def _queue_enter(event):
        session._queue_open()

    # The removal: vim's x and dd, Emacs' kill-line
    @bindings.add('x', filter=in_queue & ~detail_open)
    @bindings.add('c-k', filter=in_queue & ~detail_open)
    @bindings.add('d', 'd', filter=in_queue & ~detail_open)
    def _queue_remove_key(event):
        session._queue_remove()

    in_runs = Condition(lambda: session.console_focus == 'area'
                        and session.console_tab == 'runs')
    log_open = Condition(lambda: session._runs_detail is not None)

    @bindings.add('j', filter=in_runs & ~log_open)
    @bindings.add('down', filter=in_runs & ~log_open)
    def _runs_down(event):
        session._runs_move(1)

    @bindings.add('k', filter=in_runs & ~log_open)
    @bindings.add('up', filter=in_runs & ~log_open)
    def _runs_up(event):
        session._runs_move(-1)

    @bindings.add('enter', filter=in_runs & ~log_open)
    def _runs_enter(event):
        session._runs_open()

    # The open log scrolls; the detail's keys shadow the
    # transcript's paging while it is open
    @bindings.add('j', filter=in_runs & log_open)
    @bindings.add('down', filter=in_runs & log_open)
    def _log_down(event):
        session._runs_scroll_by(1)

    @bindings.add('k', filter=in_runs & log_open)
    @bindings.add('up', filter=in_runs & log_open)
    def _log_up(event):
        session._runs_scroll_by(-1)

    @bindings.add('pageup', filter=in_runs & log_open)
    @bindings.add('s-pageup', filter=in_runs & log_open)
    def _log_pageup(event):
        session._runs_scroll_by(-session._area_height())

    @bindings.add('pagedown', filter=in_runs & log_open)
    @bindings.add('s-pagedown', filter=in_runs & log_open)
    def _log_pagedown(event):
        session._runs_scroll_by(session._area_height())

    in_journal = Condition(lambda: session.console_focus == 'area'
                           and session.console_tab == 'journal')

    @bindings.add('j', filter=in_journal)
    @bindings.add('down', filter=in_journal)
    def _journal_down(event):
        session._journal_scroll_by(-1)

    @bindings.add('k', filter=in_journal)
    @bindings.add('up', filter=in_journal)
    def _journal_up(event):
        session._journal_scroll_by(1)

    @bindings.add('pageup', filter=in_journal)
    @bindings.add('s-pageup', filter=in_journal)
    def _journal_pageup(event):
        session._journal_scroll_by(session._area_height())

    @bindings.add('pagedown', filter=in_journal)
    @bindings.add('s-pagedown', filter=in_journal)
    def _journal_pagedown(event):
        session._journal_scroll_by(-session._area_height())

    @bindings.add('s', filter=in_journal)
    def _journal_switch_source(event):
        session._journal_switch()

    in_credits = Condition(lambda: session.console_focus == 'area'
                           and session.console_tab == 'credits')

    @bindings.add('j', filter=in_credits)
    @bindings.add('down', filter=in_credits)
    def _credits_down(event):
        session._credits_scroll_by(1)

    @bindings.add('k', filter=in_credits)
    @bindings.add('up', filter=in_credits)
    def _credits_up(event):
        session._credits_scroll_by(-1)

    @bindings.add('pageup', filter=in_credits)
    @bindings.add('s-pageup', filter=in_credits)
    def _credits_pageup(event):
        session._credits_scroll_by(-session._area_height())

    @bindings.add('pagedown', filter=in_credits)
    @bindings.add('s-pagedown', filter=in_credits)
    def _credits_pagedown(event):
        session._credits_scroll_by(session._area_height())

    # '?' opens the help wherever it is not text: the tab area, or
    # an empty input line
    question_ok = Condition(
            lambda: session.console_focus == 'area'
            or not session._input_buffer.text)

    @bindings.add('?', filter=question_ok)
    def _help_key(event):
        session._toggle_tab('help')

    # 'q' exits from the tab area (an open detail closes first);
    # the prompt keeps it as text
    @bindings.add('q', filter=area_live & detail_open)
    def _queue_close_key(event):
        session._queue_detail = None
        session._invalidate()

    @bindings.add('q', filter=area_live & log_open)
    def _runs_close_key(event):
        session._runs_close()

    @bindings.add('q', filter=area_live & ~detail_open & ~log_open)
    def _exit_key(event):
        session.submit('exit')

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
                             else session._tab_controls[
                                     session.console_tab]))
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
        session._journal_stop()
        session._app = None
    return 0
