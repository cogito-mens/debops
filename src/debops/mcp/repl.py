# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The 'debops mcp repl' client: the MCP tools for a human.

The verbs map onto the tools of the project's daemon one to one.
The execution verbs poll the task the server returns and print its
status tail as it moves; a confirmation gate asks the human. Pipes
work: commands on standard input, answers on standard output.
"""

import datetime
import getpass
import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from . import server as _server
from .client import (UnixClient, default_socket_path,
                     socket_pid_path)
from .runs import path_key
from .trust import TRUST_PATH

logger = logging.getLogger('debops')

# The capabilities the interactive clients (the REPL, the shell)
# declare: the tasks extension (a long execution is a handle the
# client polls) and the form elicitation (a confirmation gate asks
# the human)
INTERACTIVE_CAPABILITIES = {
    'elicitation': {},
    'extensions': {_server.TASKS_EXTENSION: {}},
}

# The prompt when nothing else is known
DEFAULT_PROMPT = 'debops> '

# How often a running task is polled for its status tail
POLL_INTERVAL = 0.5  # seconds

# The execution options of check/run: the REPL spelling to the tool
# argument; a valued option takes the next token
EXECUTION_OPTIONS = {
    '--limit': ('limit', True),
    '--tags': ('tags', True),
    '--skip-tags': ('skip_tags', True),
    '--forks': ('forks', True),
    '--start-at-task': ('start_at_task', True),
    '--user': ('user', True),
    '--syntax-check': ('syntax_check', False),
    '--list-hosts': ('list_hosts', False),
    '--list-tasks': ('list_tasks', False),
    '--list-tags': ('list_tags', False),
    '--force-handlers': ('force_handlers', False),
    '--flush-cache': ('flush_cache', False),
}


class ReplError(Exception):
    """A command-line mistake of the REPL; printed, never raised out."""


def ask(message, default=False):
    """A yes/no question; the default answers on Enter and EOF."""
    suffix = ' [Y/n] ' if default else ' [y/N] '
    try:
        reply = input(message + suffix)
    except (EOFError, KeyboardInterrupt):
        return default
    reply = reply.strip().lower()
    if not reply:
        return default
    return reply in ('y', 'yes')


# The daemon options a project may preset for an autospawned daemon
# ('project.autospawn_options'); the dropped keys belong to the spawn
# machinery (the transport, the identity) or carry knobs of their own
# (the view, the policy)
_SPAWN_OPTIONS_DROP = frozenset((
        'spawn', 'no-spawn', 'socket', 'instance', 'instance-root',
        'detach', 'stdio', 'docs', 'view', 'view-lock',
        'idle-timeout'))


def spawn_options_tokens(options):
    """The 'project.autospawn_options' mapping as 'mcp serve' command
    line tokens: a true value is a bare flag, a false one is left
    out, anything else becomes a '--key value' pair; the keys the
    spawn machinery or another knob already owns are dropped.
    Returns (tokens, dropped keys); raises ValueError on an entry of
    an unusable type."""
    tokens = []
    dropped = []
    for key, value in sorted((options or {}).items()):
        key = str(key).replace('_', '-')
        if key in _SPAWN_OPTIONS_DROP:
            dropped.append(key)
            continue
        if value is True:
            tokens.append('--' + key)
        elif value is False or value is None:
            continue
        elif isinstance(value, (str, int)):
            tokens.extend(('--' + key, str(value)))
        else:
            raise ValueError(
                    'project.autospawn_options.{}: not a string, a '
                    'number or a boolean'.format(key))
    return tokens, dropped


def connect_or_spawn(socket_path, project, docs=False, spawn=None,
                     interactive=False, legacy=False, err=sys.stderr,
                     spawn_options=()):
    """The client for the daemon at the socket, spawning it when it
    is missing.

    'spawn' is None (ask when interactive, spawn quietly in a pipe),
    True (never ask) or False (never spawn). The spawned daemon is
    detached: it keeps running for the next client. 'spawn_options'
    carries the preset daemon options of the project (the filtered
    'project.autospawn_options'). With 'legacy' the REPL speaks the
    2025-era protocol revision, to match a daemon started with
    '--legacy'.
    """
    probe = UnixClient(socket_path, timeout=30)
    answer = None
    try:
        answer = probe.request('server/discover', {})
    except (ConnectionError, EOFError, TimeoutError):
        pass
    if answer is not None:
        result = answer.get('result', {})
        # The era marker is stripped only by a legacy daemon
        legacy_daemon = 'resultType' not in result
        if legacy_daemon and not legacy:
            raise ReplError('the daemon at {} speaks the 2025-era '
                            'protocol; retry with --legacy'
                            .format(socket_path))
        if not legacy_daemon and legacy:
            raise ReplError('the daemon at {} speaks the {} revision; '
                            'drop --legacy'.format(
                                    socket_path, _server.PROTOCOL_VERSION))
        probe.close()
        connected = UnixClient(socket_path, timeout=30,
                               capabilities=INTERACTIVE_CAPABILITIES,
                               legacy=legacy)
        if not legacy:
            # The native answer carries the server's identity in the
            # '_meta' envelope; the legacy client's handshake learns it
            connected.protocol_version = _server.PROTOCOL_VERSION
            info = (result.get('_meta', {}).get(
                    'io.modelcontextprotocol/serverInfo') or {})
            connected.server_version = info.get('version')
            # The daemon's current view too: the socket is shared,
            # the configured default may not be where it is
            connected.server_view = result.get('_meta', {}).get(
                    _server.VIEW_META_KEY)
        return connected
    if spawn is None:
        if interactive:
            spawn = ask('Spawn daemon?', default=True)
        else:
            # A pipe gets its daemon without ceremony
            spawn = True
    if not spawn:
        raise ReplError('no MCP daemon is running (socket: {}); start '
                        'one with: debops mcp serve --socket'
                        .format(socket_path))
    # The REPL creates the runtime directory: the daemon's own log
    # files land next to the socket, ahead of its startup
    os.makedirs(os.path.dirname(socket_path), mode=0o700,
                exist_ok=True)
    print('Spawning the MCP daemon (detached): {}'
          .format(socket_path), file=err)
    # The bare --socket makes the daemon compute the same well-known
    # path (and write its pid sidecar); --detach reports when it is
    # up, the spawned marker gives it the idle timeout, and the bare
    # --log sends its audit log to the syslog (the journal)
    command = [sys.executable, '-m', 'debops', 'mcp', 'serve',
               '--socket', '--detach', '--log']
    # The preset daemon options of the project, if any
    command.extend(spawn_options)
    if docs:
        command.append('--docs')
    if legacy:
        command.append('--legacy')
    spawned = subprocess.run(
            command, cwd=project.path if project is not None else None,
            env=dict(os.environ, DEBOPS_MCP_SPAWNED='1'),
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30)
    if spawned.returncode != 0:
        details = (spawned.stderr or '').strip()
        raise ReplError('the daemon did not start: {}'.format(
                details or 'unknown error'))
    spawned_client = UnixClient(socket_path, timeout=30,
                                capabilities=INTERACTIVE_CAPABILITIES,
                                legacy=legacy)
    # The spawned daemon runs the same code as the client
    spawned_client.server_version = _server.__version__
    spawned_client.protocol_version = (
            _server.LEGACY_PROTOCOL_VERSIONS[0] if legacy
            else _server.PROTOCOL_VERSION)
    return spawned_client


def bootstrap_trust(client, project):
    """Authenticate silently with the passphrase file when the
    account can read it; an untrusted session works regardless."""
    if project is None:
        return False
    try:
        with open(os.path.join(project.path, TRUST_PATH)) as fp:
            phrase = fp.read().strip()
    except OSError:
        return False
    try:
        answer = client.request('server/authenticate',
                                {'passphrase': phrase})
    except (ConnectionError, EOFError, TimeoutError):
        return False
    if 'error' in answer:
        return False
    client.bearer = answer['result']['token']
    return True


def _daemon_pid(socket_path):
    """The pid of the daemon at the socket, from its sidecar.

    A recycled pid is not ours to kill, so the process must answer
    to 'debops-mcp' in /proc first. Returns (pid, origin); the
    refusals raise a ReplError naming the reason.
    """
    pid_path = socket_pid_path(socket_path)
    try:
        with open(pid_path) as fp:
            lines = fp.read().split()
    except OSError:
        raise ReplError('no pid file at {} — only a daemon on the '
                        'well-known socket is stoppable from here; '
                        'stop an explicit one by hand'
                        .format(pid_path))
    try:
        pid = int(lines[0])
    except (IndexError, ValueError):
        raise ReplError('unreadable pid file: ' + pid_path)
    origin = lines[1] if len(lines) > 1 else 'hand-started'
    try:
        with open('/proc/{}/comm'.format(pid)) as fp:
            comm = fp.read().strip()
    except OSError:
        raise ReplError('the daemon (pid {}) is already gone; its '
                        'stale files are cleaned up by the next one'
                        .format(pid))
    if not comm.startswith('debops-mcp'):
        raise ReplError('pid {} is not a debops-mcp process ({}); '
                        'refusing to kill it'.format(pid, comm))
    return pid, origin


def stop_daemon(socket_path, out=sys.stdout, kill=False):
    """Stop the daemon at the socket.

    The pid comes from the socket's sidecar and is correlated against
    /proc before any signal — see _daemon_pid. The default SIGTERM
    lets the daemon drain its in-flight work; 'kill' is the SIGKILL
    for a wedged one. This is a guardrail, not a boundary: a
    same-account process can always kill(2) its own daemons, and a
    daemon on another account is unreachable either way.
    """
    pid_path = socket_pid_path(socket_path)
    pid, origin = _daemon_pid(socket_path)
    if kill:
        os.kill(pid, 9)
        print('the {} daemon (pid {}) was killed; its stale files '
              'are cleaned up by the next one'.format(origin, pid),
              file=out)
        return
    os.kill(pid, 15)
    # The shutdown drains the in-flight work first
    deadline = time.monotonic() + 5
    while os.path.exists(pid_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    if os.path.exists(pid_path):
        print('the {} daemon (pid {}) is draining its in-flight work'
              .format(origin, pid), file=out)
    else:
        print('the {} daemon (pid {}) stopped'.format(origin, pid),
              file=out)


def execution_arguments(tokens):
    """The check/run arguments: the targets plus the policy options."""
    playbooks = []
    arguments = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith('--'):
            spec = EXECUTION_OPTIONS.get(token)
            if spec is None:
                raise ReplError('unknown option: {} (the policy allows: '
                                '{})'.format(token, ', '.join(sorted(
                                        EXECUTION_OPTIONS))))
            name, takes_value = spec
            if takes_value:
                index += 1
                if index >= len(tokens):
                    raise ReplError('{} needs a value'.format(token))
                arguments[name] = tokens[index]
            else:
                arguments[name] = True
        else:
            playbooks.append(token)
        index += 1
    if not playbooks:
        raise ReplError('a playbook target is required')
    arguments['playbook'] = (playbooks[0] if len(playbooks) == 1
                             else playbooks)
    return arguments


def _collapse(path):
    """The prompt form of a path: the parents collapse to their
    first character, the last component stays whole — the inventory
    and resources names run deep
    ('inventory/group_vars/all' shows as 'i/g/all/')."""
    parts = [part for part in path.split('/') if part]
    if not parts:
        return ''
    return '/'.join([part[:1] for part in parts[:-1]]
                    + [parts[-1]]) + '/'


class Repl(object):
    """One interpreter session against a daemon."""

    def __init__(self, client, project_name=None, view=None,
                 out=sys.stdout, err=sys.stderr, interactive=False,
                 project_path=None, project_type=None,
                 view_root_rel=None, confined=False,
                 history_max=0):
        self.client = client
        self.project_name = project_name
        self.view = view
        self._out = out
        self._err = err
        self.interactive = interactive
        # The ambient confinement of the account: the escape hatches
        # of the session (the editor) stay closed
        self.confined = confined
        # The bound of the history files (0: unbounded)
        self.history_max = history_max
        self.had_error = False
        self._completion_cache = {}
        self.project_path = project_path
        self.project_type = project_type
        # The view root relative to the project directory ('ansible'
        # in a legacy project, 'ansible/views/<view>' in a modern
        # one); None when there is no view to be scoped to
        self._view_root_rel = view_root_rel
        # The working directory, always relative to the project
        # directory; '' is the project directory itself
        self.cwd = view_root_rel or ''
        self._previous_cwd = None
        self.elevated = False
        # The elevation TTL, for the status display (the server
        # enforces its own; this is the client's mirror of it)
        self._elevated_until = None
        # The background watch state: the shell sets the flag and the
        # watch verb toggles a reader thread on its own connection
        # instead of holding the foreground
        self.background_watch = False
        self._watch_client = None
        self._watch_thread = None
        # A front end which runs the commands on a worker thread (the
        # console) flags the busy state; the completion never calls
        # the server while it is set — the client is one connection
        self._busy = False

    @property
    def prompt(self):
        name = self.project_name or 'docs'
        view = ':{}'.format(self.view) if self.view else ''
        marker = '!' if self.elevated else ''
        place = ''
        root = self._view_root_rel
        if self.view and root is not None:
            if self.cwd == root:
                place = ''
            elif self.cwd.startswith(root + '/'):
                # Inside the view the parents collapse to initials;
                # the last component stays whole
                place = '/' + _collapse(self.cwd[len(root) + 1:])
            else:
                # Above the view the path is project-rooted and whole
                place = ':/' + self.cwd if self.cwd else ':/'
        elif self.cwd:
            place = ':/' + self.cwd
        return 'debops{} {}{}{}> '.format(marker, name, view, place)

    @property
    def history_file(self):
        """The command history of the session: one file per project
        and view (the run-log key naming), a shared one for the
        documentation mode. Recomputed on access — a view switch
        moves the recall and the record."""
        if self.project_path is None:
            name = 'mcp-history-documentation'
        else:
            name = 'mcp-history-' + path_key(
                    self.project_path, self.project_name,
                    self.view or '')
        return os.path.join(_history_dir(), name)

    def _history_changed(self):
        """The history dimension (the view) switched; the front ends
        re-point the input history at the current file."""

    def _new_history(self):
        """The command history of the session (its current project
        and view), bounded to the configured length. One factory for
        every front end and the view-switch hooks, so the bound and
        the file never drift apart."""
        return _bounded_history_class()(self.history_file,
                                        self.history_max)

    def status_line(self):
        """The session state for a status bar: the place on the left,
        the trust state on the right. The shell renders it below the
        input line; the plain interpreter never calls it."""
        name = self.project_name or 'docs'
        view = ':{}'.format(self.view) if self.view else ''
        root = self._view_root_rel
        if self.view and root is not None:
            if self.cwd == root:
                place = ''
            elif self.cwd.startswith(root + '/'):
                place = '/' + _collapse(self.cwd[len(root) + 1:])
            else:
                place = ':/' + self.cwd if self.cwd else ':/'
        else:
            place = ':/' + self.cwd if self.cwd else ''
        right = ''
        if self.elevated:
            remaining = None
            if self._elevated_until is not None:
                remaining = int(self._elevated_until - time.monotonic())
            if remaining is not None and remaining <= 0:
                # The server dropped the elevation on its own clock
                self.elevated = False
                self._elevated_until = None
            else:
                right = ('!elev {}m'.format((remaining + 59) // 60)
                         if remaining is not None else '!elev')
        if not right and getattr(self.client, 'bearer', None) is not None:
            right = 'trusted'
        left = '{}{}{}'.format(name, view, place)
        return left, right

    # -- the wire -------------------------------------------------

    def _request(self, method, params):
        try:
            response = self.client.request(method, params)
        except (ConnectionError, EOFError, TimeoutError) as errmsg:
            # A dead daemon is a line on the error channel, not a
            # traceback; the next command reconnects
            raise ReplError('the daemon is not answering ({}); is it '
                            'running?'.format(errmsg))
        if 'error' in response:
            details = (response['error'].get('data', {}) or {})
            message = details.get('details') or response['error'].get(
                    'message', 'unknown error')
            raise ReplError(message)
        result = response.get('result') or {}
        # Every answer tells the session where the daemon's view is;
        # it may have moved since the last one
        self._adopt_view(result.get('_meta') or {})
        return result

    # -- the confirmation gate -------------------------------------

    def _confirm(self, requests):
        """Answer a confirmation elicitation; the response to send.
        A pipe cannot answer a confirmation, so it declines — the
        operation is refused cleanly instead of dangling."""
        request = (requests.get('inputRequests', {})
                   .get('confirmation', {}))
        message = (request.get('params', {}).get('message')
                   or 'the operation asks for confirmation')
        if not self.interactive:
            print('error: {} — a pipe cannot answer a '
                  'confirmation'.format(message), file=self._err)
            return {'action': 'decline', 'content': {'confirmed': False}}
        answer = self._ask('{} [y/N] '.format(message))
        return {'action': 'accept' if answer else 'decline',
                'content': {'confirmed': answer}}

    def _ask(self, message):
        """Ask a yes/no question; the default is 'no'."""
        return ask(message)

    # -- the execution flow -----------------------------------------

    def _call_tool(self, name, arguments):
        """One tool call; a confirmation gate asks the human (a
        decline comes back as the tool's error result). The declared
        view rides every call: the daemon refuses a mutation which
        checked out a stale view."""
        declared = {_server.VIEW_META_KEY: self.view}
        result = self._request('tools/call',
                               {'name': name, 'arguments': arguments,
                                '_meta': dict(declared)})
        while result.get('resultType') == 'input_required':
            answers = self._confirm(result)
            result = self._request(
                    'tools/call',
                    {'name': name, 'arguments': arguments,
                     '_meta': dict(declared),
                     'inputResponses': {'confirmation': answers}})
        # A legacy daemon strips the resultType envelope; the task
        # handle is recognized by its shape there
        if result.get('resultType') == 'task' or (
                'taskId' in result and 'status' in result):
            return self._wait_task(result)
        return result

    def _wait_task(self, task):
        """Poll a task to its end, printing the status tail as it
        moves; Ctrl-C cancels the task."""
        task_id = task.get('taskId')
        last_message = None
        try:
            while True:
                time.sleep(POLL_INTERVAL)
                task = self._request('tasks/get', {'taskId': task_id})
                message = task.get('statusMessage')
                if message and message != last_message:
                    print(message, file=self._err)
                    last_message = message
                status = task.get('status')
                if status == 'input_required':
                    # A decline completes the task with the tool's
                    # refusal, which the poll then returns
                    answers = self._confirm(task)
                    self._request('tasks/update',
                                  {'taskId': task_id,
                                   'inputResponses':
                                   {'confirmation': answers}})
                    continue
                if status == 'completed':
                    return task.get('result') or {}
                if status in ('failed', 'cancelled'):
                    error = task.get('error') or {}
                    raise ReplError(error.get('message',
                                              'the execution failed'))
        except KeyboardInterrupt:
            try:
                self._request('tasks/cancel', {'taskId': task_id})
            except ReplError:
                pass
            raise ReplError('cancelled (interrupted)')

    # -- the renderer -------------------------------------------------

    def _render(self, result):
        """Print a tool result; errors go to the error channel."""
        if result.get('isError'):
            self.had_error = True
        for block in result.get('content', []):
            if block.get('type') != 'text':
                continue
            text = block.get('text', '')
            if result.get('isError'):
                print('error: ' + text, file=self._err)
            else:
                print(text, file=self._out)

    def _note(self, text):
        """A status line on the error channel; the presentation hook
        for the interfaces with a status bar of their own."""
        print(text, file=self._err)

    # -- the verbs ------------------------------------------------------

    def cmd_status(self, args):
        if args:
            raise ReplError('status takes no arguments')
        return self._call_tool('project.status', {})

    def cmd_views(self, args):
        if args:
            raise ReplError('views takes no arguments')
        return self._call_tool('view.list', {})

    def cmd_view(self, args):
        if len(args) != 1:
            raise ReplError('usage: view <name>')
        # The session state follows the answer's '_meta', which every
        # result carries: the view is the daemon's, shared with every
        # client — this command included
        return self._call_tool('view.switch', {'view': args[0]})

    def _adopt_view(self, meta):
        """Follow the daemon's current view, carried in every
        result's '_meta': it is shared state, and another client may
        have moved it. The scope edge, the working directory, the
        completions and the history file all follow; a quiet no-op
        when unchanged."""
        if _server.VIEW_META_KEY not in meta:
            return
        view = meta[_server.VIEW_META_KEY]
        if view == self.view:
            return
        self.view = view
        self._view_root_rel = _view_root_rel(self.project_type, view)
        self.cwd = self._view_root_rel or ''
        self._previous_cwd = None
        self._completion_cache.clear()
        self._history_changed()
        self._note('the view is now {}; the working directory moved '
                   'to its root'.format(view or '(none)'))

    def cmd_check(self, args):
        return self._call_tool('check', execution_arguments(args))

    def cmd_run(self, args):
        return self._call_tool('run', execution_arguments(args))

    def cmd_elevate(self, args):
        """Raise the session: the gates answer themselves while it
        lasts ('drop' ends it)."""
        if args:
            raise ReplError('elevate takes no arguments')
        if self.client.legacy:
            raise ReplError('trust needs the {} revision; this daemon '
                            'speaks the 2025-era protocol'
                            .format(_server.PROTOCOL_VERSION))
        if self.elevated:
            raise ReplError('already elevated ("drop" ends it)')
        phrase = self._passphrase()
        if self.client.bearer is None:
            answer = self._request('server/authenticate',
                                   {'passphrase': phrase})
            self.client.bearer = answer['token']
        answer = self._request('server/elevate', {'passphrase': phrase})
        self.elevated = True
        expires_in = answer.get('expiresIn')
        self._elevated_until = (time.monotonic() + expires_in
                                if isinstance(expires_in,
                                              (int, float)) else None)
        self._note('elevated; the confirmation gates answer themselves '
                   'for a while of active use')

    def cmd_drop(self, args):
        """End the elevation ahead of its timeout."""
        if args:
            raise ReplError('drop takes no arguments')
        if not self.elevated:
            raise ReplError('not elevated')
        if self.client.bearer is not None and not self.client.legacy:
            self._request('server/drop', {})
        self.elevated = False
        self._elevated_until = None
        root = self._view_root_rel
        if root is not None and not (self.cwd == root
                                     or self.cwd.startswith(
                                             root + '/')):
            # The working directory is out of the scope again
            self.cwd = root
            self._previous_cwd = None
            self._note('dropped; the gates ask again, and the working '
                       'directory is the view root')
        else:
            self._note('dropped; the gates ask again')

    def cmd_watch(self, args):
        """Stream the activity of the server's clients until
        Ctrl-C — in the shell, in the background, until 'watch'
        toggles it off."""
        if args:
            raise ReplError('watch takes no arguments')
        if self.client.legacy:
            raise ReplError('the 2025-era protocol has no '
                            'subscription streams')
        if self.background_watch:
            self._toggle_watch()
            return
        self._ensure_trusted()
        self._note('watching the server activity (Ctrl-C ends it)')
        try:
            for message in self.client.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'activity': True}}):
                if message.get('method') == 'notifications/activity':
                    print(message.get('params', {}).get('line', ''),
                          file=self._out)
        except KeyboardInterrupt:
            # Closing the channel ends the stream server-side
            self.client.close()
            self._note('watch ended')
        except (EOFError, RuntimeError, TimeoutError) as errmsg:
            raise ReplError(str(errmsg))

    def _toggle_watch(self):
        """Start or stop the background watch: a reader thread on its
        own connection, so the session keeps answering while the
        activity lines print."""
        if self._watch_thread is not None:
            self.stop_watch(note='watch ended')
            return
        self._ensure_trusted()
        watcher = self.client.clone()
        thread = threading.Thread(target=self._watch_loop,
                                  args=(watcher,), daemon=True)
        self._watch_client = watcher
        self._watch_thread = thread
        thread.start()
        self._note('watching the server activity in the background '
                   '("watch" again ends it)')

    def _watch_line(self, line):
        """One line of the watched activity; the presentation hook —
        the console routes it to the activity pane."""
        print(line, file=self._out)

    def _watch_loop(self, watcher):
        """The background watch: the activity lines print until the
        toggle or the connection ends the stream."""
        died = None
        try:
            for message in watcher.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'activity': True}}):
                if message.get('method') == 'notifications/activity':
                    self._watch_line(
                            message.get('params', {}).get('line', ''))
        except (EOFError, ConnectionError, RuntimeError, TimeoutError,
                OSError) as errmsg:
            died = str(errmsg)
        finally:
            if self._watch_client is watcher:
                # The stream ended on its own — say so
                self._watch_client = None
                self._watch_thread = None
                try:
                    watcher.close()
                except Exception:
                    pass
                self._note('watch ended' + (': ' + died
                                            if died else ''))

    def stop_watch(self, note=None):
        """Stop the background watch, if any; closing the channel
        ends the stream server-side."""
        watcher = self._watch_client
        self._watch_client = None
        self._watch_thread = None
        if watcher is not None:
            try:
                watcher.close()
            except Exception:
                pass
        if note:
            self._note(note)

    def cmd_edit(self, args):
        """Open a project file in $VISUAL/$EDITOR and write the
        changes back through the tools (committed by the server)."""
        if self.confined:
            # The editor's own escapes (':!sh' and friends) would
            # leave the confinement of the account
            raise ReplError('a confined session has no editor')
        if len(args) != 1:
            raise ReplError('usage: edit <path>')
        if not self.interactive:
            raise ReplError('edit needs a terminal')
        path = self._resolve(args[0])
        write_path = self._resolve_write(args[0])
        result = self._call_tool('project.read', {'path': path})
        current = ''
        if result.get('isError'):
            text = ''.join(block.get('text', '') for block in
                           result.get('content', []))
            if 'not found' not in text and 'no such' not in text \
                    and 'does not exist' not in text:
                # A refusal (a denied path) stops the edit; a missing
                # file starts empty
                self._render(result)
                return
        else:
            current = (result.get('structuredContent') or {}).get(
                    'content')
            if current is None:
                # The text rendering is wrapped for display, not for
                # editing; an older daemon has no structured answer
                raise ReplError('the daemon does not answer '
                                'structured reads; edit needs them')
        fd, tmp = tempfile.mkstemp(prefix='debops-edit-',
                                   suffix='-' + os.path.basename(path))
        try:
            with os.fdopen(fd, 'w') as fp:
                fp.write(current)
            returncode = self._run_editor(tmp)
            if returncode != 0:
                raise ReplError('the editor exited with {}'
                                .format(returncode))
            with open(tmp) as fp:
                new = fp.read()
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if new == current:
            self._note('unchanged: ' + path)
            return
        result = self._call_tool('file.write',
                                 {'path': write_path, 'content': new})
        self._render(result)

    def _run_editor(self, path):
        """Open the editor on the temporary file; the console
        suspends its own interface for it."""
        editor = (os.environ.get('VISUAL') or
                  os.environ.get('EDITOR') or 'vi')
        return subprocess.call(shlex.split(editor) + [path])

    def _passphrase(self):
        """The trust passphrase: the file when the account reads it,
        a prompt when it exists but is unreadable (the cross-account
        case), a clean refusal when it is absent — a daemon without
        the file has nothing to check an answer against."""
        if self.project_path is not None:
            try:
                with open(os.path.join(self.project_path,
                                       TRUST_PATH)) as fp:
                    return fp.read().strip()
            except PermissionError:
                pass  # it exists; ask
            except OSError:
                raise ReplError('no trust passphrase')
        if not self.interactive:
            raise ReplError('the trust passphrase of the project is '
                            'not readable here; a pipe cannot ask '
                            'for it')
        return self._ask_passphrase()

    def _ask_passphrase(self):
        """The hidden passphrase prompt; the presentation hook — the
        console asks through its own input line."""
        try:
            return getpass.getpass('the trust passphrase: ')
        except (EOFError, KeyboardInterrupt):
            raise ReplError('cancelled')

    def _ensure_trusted(self):
        """Authenticate the session when the passphrase is reachable;
        raises the reason when not."""
        if self.client.bearer is not None:
            return
        if self.client.legacy:
            raise ReplError('trust needs the {} revision; this daemon '
                            'speaks the 2025-era protocol'
                            .format(_server.PROTOCOL_VERSION))
        phrase = self._passphrase()
        answer = self._request('server/authenticate',
                               {'passphrase': phrase})
        self.client.bearer = answer['token']

    def cmd_logs(self, args):
        if args:
            raise ReplError('logs takes no arguments')
        return self._call_tool('execution.list', {})

    def cmd_log(self, args):
        if len(args) != 1:
            raise ReplError('usage: log <execution id>')
        try:
            execution_id = int(args[0])
        except ValueError:
            raise ReplError('the execution id is a number: {!r}'
                            .format(args[0]))
        return self._call_tool('execution.output',
                               {'id': execution_id})

    def cmd_purge(self, args):
        if len(args) != 1:
            raise ReplError('usage: purge <age in seconds>')
        try:
            older_than = int(args[0])
        except ValueError:
            raise ReplError('the age is a number of seconds: {!r}'
                            .format(args[0]))
        return self._call_tool('execution.purge',
                               {'older_than': older_than})

    def cmd_roles(self, args):
        arguments = {'filter': args[0]} if args else {}
        return self._call_tool('collection.roles', arguments)

    def cmd_role(self, args):
        if len(args) != 1:
            raise ReplError('usage: role <name>')
        return self._call_tool('role.info', {'name': args[0]})

    def _cmd_role_detail(self, args, tool):
        if not args or len(args) > 2:
            raise ReplError('usage: {} <role> [--full]'.format(tool))
        arguments = {'name': args[0]}
        if len(args) == 2:
            if args[1] != '--full':
                raise ReplError('unknown option: {} (only --full)'
                                .format(args[1]))
            arguments['detail'] = 'full'
        return self._call_tool(tool, arguments)

    def cmd_defaults(self, args):
        return self._cmd_role_detail(args, 'role.defaults')

    def cmd_vars(self, args):
        return self._cmd_role_detail(args, 'role.vars')

    def cmd_tasks(self, args):
        return self._cmd_role_detail(args, 'role.tasks')

    def cmd_handlers(self, args):
        return self._cmd_role_detail(args, 'role.handlers')

    def cmd_playbooks(self, args):
        if not args:
            return self._call_tool('playbook.list', {})
        if len(args) != 1:
            raise ReplError('usage: playbooks [filter | collection]')
        # A dotted name is a collection; its playbooks answer through
        # the collection listing
        if '.' in args[0]:
            return self._call_tool('collection.playbooks',
                                   {'collection': args[0]})
        return self._call_tool('playbook.list', {'filter': args[0]})

    def cmd_playbook(self, args):
        if not args or len(args) > 2:
            raise ReplError('usage: playbook <target> [--full]')
        arguments = {'playbook': args[0]}
        if len(args) == 2:
            if args[1] != '--full':
                raise ReplError('unknown option: {} (only --full)'
                                .format(args[1]))
            arguments['detail'] = 'full'
        return self._call_tool('playbook.info', arguments)

    def cmd_doc(self, args):
        plugin = None
        arguments = {}
        index = 0
        while index < len(args):
            token = args[index]
            if token == '--type':
                index += 1
                if index >= len(args):
                    raise ReplError('--type needs a value')
                arguments['type'] = args[index]
            elif token == '--list':
                arguments['list'] = True
            elif token == '--snippet':
                arguments['snippet'] = True
            elif token == '--json':
                arguments['format'] = 'json'
            elif token.startswith('--'):
                raise ReplError('unknown option: {} (doc knows '
                                '--type, --list, --snippet, --json)'
                                .format(token))
            elif plugin is None:
                plugin = token
            else:
                raise ReplError('usage: doc [plugin] [--type TYPE] '
                                '[--list | --snippet] [--json]')
            index += 1
        if plugin is not None:
            arguments['plugin'] = plugin
        if not arguments.get('list') and plugin is None:
            raise ReplError('doc needs a plugin name (or --list)')
        return self._call_tool('ansible.doc', arguments)

    def cmd_files(self, args):
        if len(args) > 1:
            raise ReplError('usage: files [directory]')
        arguments = ({'path': self._resolve(args[0])} if args
                     else {})
        return self._call_tool('project.files', arguments)

    def cmd_read(self, args):
        if len(args) != 1:
            raise ReplError('usage: read <path>')
        return self._call_tool('project.read',
                               {'path': self._resolve(args[0])})

    def cmd_pull(self, args):
        if args:
            raise ReplError('pull takes no arguments')
        return self._call_tool('project.pull', {})

    def cmd_push(self, args):
        if args:
            raise ReplError('push takes no arguments')
        return self._call_tool('project.push', {})

    def cmd_environment(self, args):
        arguments = {'filter': args[0]} if args else {}
        return self._call_tool('environment.list', arguments)

    def cmd_set(self, args):
        if len(args) != 2:
            raise ReplError('usage: set <NAME> <value>')
        return self._call_tool('environment.set',
                               {'name': args[0], 'value': args[1]})

    def cmd_unset(self, args):
        if len(args) != 1:
            raise ReplError('usage: unset <NAME>')
        return self._call_tool('environment.unset', {'name': args[0]})

    # -- the navigation -----------------------------------------------

    def _resolve(self, argument):
        """Resolve a navigation path against the current directory.

        Returns the path relative to the project directory, without a
        leading slash ('' is the project directory itself). A leading
        '/' anchors at the project directory, the way the tools spell
        it. The scope: an unelevated session stays inside the view,
        an elevated one roams the project directory; nothing leaves
        the project.
        """
        if argument.startswith('/'):
            components = []
        else:
            components = [part for part in self.cwd.split('/') if part]
        for part in argument.split('/'):
            if part in ('', '.'):
                continue
            if part == '..':
                if not components:
                    raise ReplError("the project's edge")
                components.pop()
            else:
                components.append(part)
        resolved = '/'.join(components)
        root = self._view_root_rel
        if not self.elevated and root is not None:
            if resolved != root and not resolved.startswith(root + '/'):
                raise ReplError("the view's edge; elevate to go "
                                "further")
        return resolved

    def _resolve_write(self, argument):
        """Resolve for a write tool: the view-relative spelling inside
        the view (the write tools anchor there), the project-rooted
        '/...' spelling above it."""
        resolved = self._resolve(argument)
        root = self._view_root_rel
        if root is not None and (resolved == root
                                 or resolved.startswith(root + '/')):
            view_relative = resolved[len(root):].lstrip('/')
            if not view_relative:
                raise ReplError('the path names the view directory '
                                'itself')
            return view_relative
        if not resolved:
            raise ReplError('the path names the project directory '
                            'itself')
        return '/' + resolved

    def cmd_pwd(self, args):
        """Print the current directory (project-rooted, paste-able)."""
        if args:
            raise ReplError('pwd takes no arguments')
        print('/' + self.cwd if self.cwd else '/', file=self._out)

    def cmd_cd(self, args):
        """Change the current directory; bare 'cd' returns to the view
        root, 'cd -' to the previous directory."""
        if len(args) > 1:
            raise ReplError('usage: cd [directory]')
        if not args:
            target = self._view_root_rel or ''
        elif args[0] == '-':
            if self._previous_cwd is None:
                raise ReplError('no previous directory')
            target = self._previous_cwd
        else:
            target = self._resolve(args[0])
        result = self._call_tool(
                'project.list', {'path': target} if target else {})
        if result.get('isError'):
            return result
        self._previous_cwd = self.cwd
        self.cwd = target

    def cmd_ls(self, args):
        """List the current directory (or the named one), one level."""
        if len(args) > 1:
            raise ReplError('usage: ls [path]')
        target = self._resolve(args[0]) if args else self.cwd
        return self._call_tool(
                'project.list', {'path': target} if target else {})

    def cmd_mkdir(self, args):
        """Create a directory, with parents as needed."""
        if len(args) != 1:
            raise ReplError('usage: mkdir <directory>')
        return self._call_tool('project.mkdir',
                               {'path': self._resolve_write(args[0])})

    def cmd_rm(self, args):
        """Delete a file (a directory is refused)."""
        if len(args) != 1:
            raise ReplError('usage: rm <path>')
        return self._call_tool('file.delete',
                               {'path': self._resolve_write(args[0])})

    def cmd_mv(self, args):
        """Rename a file; the target must not exist. One commit."""
        if len(args) != 2:
            raise ReplError('usage: mv <source> <target>')
        return self._call_tool('file.move', {
                'source': self._resolve_write(args[0]),
                'target': self._resolve_write(args[1])})

    def cmd_cp(self, args):
        """Copy a file: read the source, write the target (the write
        commits). A target ending in '/' names a directory."""
        if len(args) != 2:
            raise ReplError('usage: cp <source> <target>')
        target_arg = args[1]
        if target_arg.endswith('/'):
            target_arg += os.path.basename(args[0].rstrip('/'))
        source = self._resolve(args[0])
        target = self._resolve_write(target_arg)
        if not source:
            raise ReplError('the source names the project directory '
                            'itself')
        result = self._call_tool('project.read', {'path': source})
        if result.get('isError'):
            return result
        content = (result.get('structuredContent') or {}).get('content')
        if content is None:
            # An older daemon answers the text rendering only
            raise ReplError('the daemon does not answer structured '
                            'reads; cp needs them')
        return self._call_tool('file.write',
                               {'path': target, 'content': content})

    # -- the loop ---------------------------------------------------

    def verbs(self):
        return dict((name[4:], getattr(self, name))
                    for name in dir(self) if name.startswith('cmd_'))

    def execute(self, line):
        """Run one command line; the result prints on the channels."""
        try:
            tokens = shlex.split(line)
        except ValueError as errmsg:
            raise ReplError(str(errmsg))
        if not tokens:
            return
        verb, args = tokens[0], tokens[1:]
        if verb in ('exit', 'quit'):
            raise EOFError()
        if verb == 'help':
            self._print_help()
            return
        handler = self.verbs().get(verb)
        if handler is None:
            raise ReplError("unknown verb: {!r} ('help' lists them)"
                            .format(verb))
        result = handler(args)
        if verb in ('edit', 'mkdir', 'rm', 'mv', 'cp'):
            # The directory listings the completion serves may have
            # changed
            self._completion_cache = {
                    key: value for key, value
                    in self._completion_cache.items()
                    if not (isinstance(key, tuple)
                            and key[0] == 'list')}
        # A verb which printed its own answer returns None
        if result is not None:
            self._render(result)

    # A front end with verbs of its own (the console's 'panel')
    # declares them here
    HELP_EXTRA = {}

    def _print_help(self):
        print('Commands (the MCP tools of the project, for a human):',
              file=self._out)
        entries = dict(HELP)
        entries.update(self.HELP_EXTRA)
        for verb, synopsis in sorted(entries.items()):
            print('  {:24s} {}'.format(verb, synopsis), file=self._out)

    # -- the completion candidates ----------------------------------

    # The verbs sharing one candidate source
    _CANDIDATE_SOURCES = {
        'check': 'playbooks', 'run': 'playbooks',
        'playbook': 'playbooks',
        'view': 'views',
        'role': 'roles', 'defaults': 'roles', 'vars': 'roles',
        'tasks': 'roles', 'handlers': 'roles',
    }

    # The verbs which complete paths relative to the current
    # directory ('cd' completes directories only)
    _PATH_VERBS = frozenset((
            'cd', 'ls', 'read', 'edit', 'mkdir', 'rm', 'cp', 'mv',
    ))

    def complete_candidates(self, verb):
        """The completion candidates of a verb, cached per view."""
        source = self._CANDIDATE_SOURCES.get(verb)
        if source is None:
            return []
        if source not in self._completion_cache:
            self._completion_cache[source] = self._load_candidates(verb)
        return self._completion_cache[source]

    def _load_candidates(self, verb):
        if self._busy:
            # A command is holding the one connection
            return []
        try:
            if verb in ('check', 'run', 'playbook'):
                result = self._request('tools/call',
                                       {'name': 'playbook.list',
                                        'arguments': {}})
                return [entry['target'] for entry in
                        result.get('structuredContent') or []]
            if verb == 'view':
                result = self._request('tools/call',
                                       {'name': 'view.list',
                                        'arguments': {}})
                structured = result.get('structuredContent') or {}
                return [entry['name'] for entry in
                        structured.get('views', [])]
            if verb in ('role', 'defaults', 'vars', 'tasks',
                        'handlers'):
                result = self._request('tools/call',
                                       {'name': 'collection.roles',
                                        'arguments': {}})
                names = []
                for entry in result.get('structuredContent') or []:
                    names.extend(entry.get('roles', []))
                return sorted(set(names))
        except (ReplError, ConnectionError, EOFError, OSError):
            return []
        return []

    def path_candidates(self, verb, fragment):
        """The path completion of a navigation verb, relative to the
        current directory; the listing comes from the server and is
        cached per directory."""
        dirs_only = verb == 'cd'
        if '/' in fragment:
            base, stub = fragment.rsplit('/', 1)
            base += '/'
        else:
            base, stub = '', fragment
        try:
            directory = self._resolve(base or '.')
        except ReplError:
            return []
        key = ('list', directory)
        if key not in self._completion_cache:
            if self._busy:
                # A command is holding the one connection
                return []
            try:
                result = self._request(
                        'tools/call',
                        {'name': 'project.list',
                         'arguments': ({'path': directory} if directory
                                       else {})})
            except (ReplError, ConnectionError, EOFError, OSError):
                result = {}
            if result.get('isError'):
                entries = []
            else:
                entries = result.get('structuredContent') or []
            self._completion_cache[key] = entries
        candidates = []
        for entry in self._completion_cache[key]:
            name = entry.get('name', '')
            if not name.startswith(stub):
                continue
            if dirs_only and entry.get('type') != 'dir':
                continue
            suffix = '/' if entry.get('type') == 'dir' else ''
            candidates.append(base + name + suffix)
        return candidates


HELP = {
    'status': 'the project state',
    'views': 'the infrastructure views',
    'view <name>': 'switch the active view',
    'check <targets> [opts]': 'a check-mode run (opts: --limit, '
                              '--tags, --list-hosts, ...)',
    'run <targets> [opts]': 'execute playbooks (same options)',
    'logs': 'the recorded executions',
    'log <id>': 'the output of an execution',
    'purge <seconds>': 'drop the run logs older than the age',
    'roles [filter]': 'the visible collections and their roles',
    'role <name>': 'describe a role',
    'defaults <name> [--full]': "a role's default variables",
    'vars <name> [--full]': "a role's internal variables",
    'tasks <name> [--full]': "a role's task list",
    'handlers <name> [--full]': "a role's handlers",
    'playbooks [filter | collection]': 'the playbook targets, or a '
                                       "collection's playbooks",
    'playbook <target> [--full]': 'describe a playbook',
    'doc <plugin> [opts]': 'ansible-doc (opts: --type, --list, '
                           '--snippet, --json)',
    'files [directory]': 'the project files, recursively (the whole '
                         'tree without one)',
    'read <path>': 'read a project file',
    'pwd': 'the current directory',
    'cd [directory | -]': 'change the current directory (bare: the '
                          'view root)',
    'ls [path]': 'list the directory, one level',
    'mkdir <directory>': 'create a directory, with parents',
    'rm <path>': 'delete a file',
    'mv <source> <target>': 'rename a file (one commit)',
    'cp <source> <target>': 'copy a file (a target ending in / names '
                            'a directory)',
    'pull / push': 'sync the project with its remote',
    'environment [filter]': 'the environment variables',
    'set <NAME> <value>': 'set an environment variable',
    'unset <NAME>': 'remove an environment variable',
    'edit <path>': 'open a project file in the editor, write back '
                   'through the tools',
    'elevate': 'the confirmation gates answer themselves for a '
               'while ("drop" ends it)',
    'drop': 'end the elevation',
    'watch': 'stream the activity of the server\'s clients '
             '(a trusted session; Ctrl-C ends it — in the shell it '
             'runs in the background and "watch" toggles it)',
    'help': 'this text',
    'exit': 'leave the interpreter (Ctrl-D works too)',
}


# The base class when prompt_toolkit is importable (the async
# completion protocol comes from it); the plain reader never builds a
# completer, so a missing library costs nothing
try:
    from prompt_toolkit.completion import Completer as _PtCompleter
except ImportError:
    _PtCompleter = object


def _candidates_for(repl, before):
    """The completion candidates for the line up to the cursor: the
    verbs at the start of the line, the candidates of the verb after
    it. Returns (word being completed, candidates). Shared by the
    prompt_toolkit completer and the readline adapter."""
    words = before.split()
    # The fragment being completed is the last token, unless the
    # line ends with a space ('--' fragments survive the split,
    # which the word extraction of the document would not)
    if words and not before.endswith(' '):
        word = words.pop()
    else:
        word = ''
    if not words:
        return word, sorted(list(repl.verbs()) + ['help', 'exit'])
    if words[0] in ('check', 'run') and word.startswith('--'):
        return word, sorted(EXECUTION_OPTIONS)
    if words[0] == 'doc' and word.startswith('--'):
        return word, ['--type', '--list', '--snippet', '--json']
    if words[0] in Repl._PATH_VERBS:
        return word, repl.path_candidates(words[0], word)
    return word, repl.complete_candidates(words[0])


def _suspend_bindings(repl):
    """The Ctrl-Z of the prompt front ends: UNIX job control when
    the account is free (the 'enable_suspend' of prompt_toolkit
    carries it), a note when it is confined — the default binding
    would insert a literal '^Z' into the input line, which neither
    wants."""
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    bindings = KeyBindings()

    @bindings.add('c-z', filter=Condition(lambda: repl.confined))
    def _confined_suspend(event):
        repl._note('a confined session does not background')

    return bindings


class _Completer(_PtCompleter):
    """The prompt_toolkit completer: the verbs, then the candidates
    of the verb (playbook targets, role names, files, views)."""

    def __init__(self, repl):
        self._repl = repl

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion
        word, candidates = _candidates_for(
                self._repl, document.text_before_cursor)
        for candidate in candidates:
            if candidate.startswith(word):
                yield Completion(candidate, start_position=-len(word))


def _readline_complete(readline, repl):
    """The readline completion of the plain reader: the same
    candidates as the prompt_toolkit completer, adapted to the
    (text, state) protocol."""
    candidates = []

    def complete(text, state):
        if state == 0:
            line = readline.get_line_buffer()
            word, found = _candidates_for(
                    repl, line[:readline.get_begidx()] + text)
            del candidates[:]
            candidates.extend(
                    candidate for candidate in found
                    if candidate.startswith(word))
        if state < len(candidates):
            return candidates[state]
        return None
    return complete


def run_pipe(repl, commands=None):
    """Commands on stdin — or the one-shot string of the '-c' form —
    answers on stdout; a nonzero exit status notes an error."""
    lines = commands.splitlines() if commands is not None else sys.stdin
    for line in lines:
        try:
            repl.execute(line)
        except ReplError as errmsg:
            print('error: {}'.format(errmsg), file=repl._err)
            repl.had_error = True
        except EOFError:
            break
        except KeyboardInterrupt:
            # An interrupted script dies like any other tool
            return 130
        # The answer must leave now: a driver's next read waits for
        # it, and a pipe is block-buffered without the flush
        for channel in (repl._out, repl._err):
            if hasattr(channel, 'flush'):
                channel.flush()
    return 1 if repl.had_error else 0


def _banner(repl):
    """The greeting line: the server's version and protocol era."""
    client = repl.client
    version = getattr(client, 'server_version', None) or '?'
    protocol = getattr(client, 'protocol_version', None) or '?'
    return ('debops mcp repl (DebOps {}, protocol {}) - "help" lists '
            'the commands, Ctrl-D exits'.format(version, protocol))


def run_interactive(repl):
    """The interactive interpreter: prompt_toolkit when it is here
    (history, completion), the plain readline loop otherwise."""
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.shortcuts import CompleteStyle
    except ImportError:
        # The plain reader carries the session; completion just is
        # not there
        return run_interactive_plain(repl)
    session = PromptSession(
            completer=_Completer(repl),
            # The shell habit: Tab inserts the completion, a double
            # Tab lists the candidates
            complete_style=CompleteStyle.READLINE_LIKE,
            # Job control, unless the account is confined
            enable_suspend=not repl.confined,
            key_bindings=_suspend_bindings(repl),
            history=repl._new_history())
    # A view switch moves the recall and the record to the view's
    # own history file
    repl._history_changed = lambda: setattr(
            session.default_buffer, 'history', repl._new_history())
    print(_banner(repl), file=repl._err)
    while True:
        try:
            line = session.prompt(repl.prompt)
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        try:
            if _execute_line(repl, line):
                break
        except KeyboardInterrupt:
            # A command died on the interrupt; the session survives.
            # The connection may hold a half-read answer — drop it
            print('^C', file=repl._err)
            repl.client.close()
    return 0


def _import_error_detail():
    """The missing module of the last ImportError."""
    return sys.exc_info()[1]


def _history_dir():
    directory = os.path.join(
            os.environ.get('XDG_DATA_HOME', os.path.expanduser(
                    os.path.join('~', '.local', 'share'))),
            'debops')
    os.makedirs(directory, exist_ok=True)
    return directory


def _read_history_strings(path):
    """The entries of the shared history file, oldest first. The
    prompt_toolkit format ('# timestamp' separators, '+'-prefixed
    lines building one entry) and the plain one-command-per-line
    files of the early plain reader both load."""
    entries = []
    current = []
    try:
        with open(path, 'rb') as fp:
            lines = [line.decode('utf-8', 'replace') for line in fp]
    except OSError:
        return entries
    for line in lines:
        if line.startswith('+'):
            current.append(line[1:])
            continue
        if current:
            # Join the entry, dropping the trailing newline
            entries.append(''.join(current)[:-1])
            current = []
        if line.startswith('#') or not line.strip():
            continue
        # The legacy plain format: one command per line
        entries.append(line[:-1] if line.endswith('\n') else line)
    if current:
        entries.append(''.join(current)[:-1])
    return entries


def _append_history_string(path, line):
    """Append one command to the shared history file, in the
    prompt_toolkit format the other front ends read."""
    with open(path, 'ab') as fp:
        fp.write('\n# {}\n'.format(
                datetime.datetime.now()).encode('utf-8'))
        for part in line.split('\n'):
            fp.write('+{}\n'.format(part).encode('utf-8'))


def _view_root_rel(project_type, view):
    """The view's root relative to the project directory; None when
    no view is active (the scope is the project directory then)."""
    if project_type == 'legacy':
        return 'ansible'
    if project_type is not None and view is not None:
        return os.path.join('ansible', 'views', view)
    return None


def history_max_entries(config_raw):
    """The 'mcp.history.max_entries' knob: the bound of the shared
    history files, in entries (0: unbounded; absent: a generous
    10000). The files live in the account's data home and fill the
    account's disk, so the knob is the account's (ambient), never
    the project's."""
    raw = config_raw.get('mcp', {}).get('history', {}).get(
            'max_entries', 10000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Unknown mcp.history.max_entries value %r, "
                       'using 10000', raw)
        value = 10000
    return max(0, value)


def _prune_history_file(path, max_entries):
    """Trim the history file to its newest 'max_entries' records.
    The retained records keep their bytes — timestamps included — so
    the format survives: a record is its separator lines (a blank
    line, a '# timestamp') plus the '+' lines of the entry; a legacy
    plain line is a record of its own."""
    try:
        with open(path, 'rb') as fp:
            data = fp.read()
    except OSError:
        return
    records = []
    pending = []
    current = None
    for line in data.decode('utf-8', 'replace').splitlines(
            keepends=True):
        if line.startswith('+'):
            if current is None:
                current = list(pending)
                pending = []
                records.append(current)
            current.append(line)
        elif line.startswith('#') or not line.strip():
            # A separator: it precedes its record
            current = None
            pending.append(line)
        else:
            # The legacy plain format: the line is the record
            current = None
            records.append(pending + [line])
            pending = []
    if len(records) <= max_entries:
        return
    with open(path, 'wb') as fp:
        for record in records[len(records) - max_entries:]:
            for line in record:
                fp.write(line.encode('utf-8'))


_BOUNDED_HISTORY_CLASS = None


def _bounded_history_class():
    """The FileHistory subclass with the bounded length, built on
    first use — prompt_toolkit imports stay out of this module."""
    global _BOUNDED_HISTORY_CLASS
    if _BOUNDED_HISTORY_CLASS is None:
        from prompt_toolkit.history import FileHistory

        class BoundedFileHistory(FileHistory):
            """The shared history file with a bounded length: an
            append trims the oldest whole records beyond the
            maximum (0: unbounded)."""

            def __init__(self, filename, max_entries):
                super().__init__(filename)
                self._max_entries = max_entries

            def store_string(self, string):
                super().store_string(string)
                if self._max_entries > 0:
                    _prune_history_file(self.filename,
                                        self._max_entries)

        _BOUNDED_HISTORY_CLASS = BoundedFileHistory
    return _BOUNDED_HISTORY_CLASS


def run_interactive_plain(repl):
    """The no-dependency interactive loop: readline history when the
    module is there, plain input() either way."""
    try:
        import readline
    except ImportError:
        readline = None
    if readline is not None:
        # The per-view file, in the prompt_toolkit format the other
        # front ends read and write; readline keeps the recall in
        # memory, fed from it and appended to it per command, and
        # re-fed when a view switch moves the file
        def _feed_history():
            for entry in _read_history_strings(repl.history_file):
                readline.add_history(entry)

        def _reload_history():
            readline.clear_history()
            _feed_history()
        _feed_history()
        readline.set_history_length(1000)
        repl._history_changed = _reload_history
        # The completion is the engine's own; the word break keeps
        # the path fragments whole
        readline.set_completer_delims(' \t\n')
        readline.set_completer(_readline_complete(readline, repl))
        readline.parse_and_bind('tab: complete')
    print(_banner(repl), file=repl._err)
    if repl.confined:
        # A confined account gets no job control: the keystroke is
        # consumed, the process stays in the foreground
        previous_tstp = signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    try:
        while True:
            try:
                line = input(repl.prompt)
            except KeyboardInterrupt:
                # A fresh prompt, not a dead session
                print()
                continue
            except EOFError:
                break
            if readline is not None and line.strip():
                try:
                    _append_history_string(repl.history_file, line)
                    if repl.history_max > 0:
                        _prune_history_file(repl.history_file,
                                            repl.history_max)
                except OSError:
                    pass
            try:
                done = _execute_line(repl, line)
            except KeyboardInterrupt:
                # The command died on the interrupt; the session
                # survives. The connection may hold a half-read
                # answer — drop it
                print('^C', file=repl._err)
                repl.client.close()
                continue
            if done:
                break
    finally:
        if repl.confined:
            signal.signal(signal.SIGTSTP, previous_tstp)
    return 0


def _execute_line(repl, line):
    """One command in an interactive loop; True when it asked to
    leave."""
    try:
        repl.execute(line)
    except ReplError as errmsg:
        print('error: {}'.format(errmsg), file=repl._err)
        repl.had_error = True
    except EOFError:
        return True
    return False


def run(client, project_name=None, view=None, interactive=None,
        project_path=None, project_type=None, view_root_rel=None,
        frontend=None, command=None, confined=False, history_max=0,
        plain=False):
    """Build and run an interpreter session against the connected
    daemon. 'frontend' selects the chrome: None for the plain
    interpreter, 'shell' for the status-bar interface, 'console' for
    the fullscreen cockpit (a pipe stays a pipe either way).
    'command' is the one-shot '-c' form: the commands run
    non-interactively and the process exits — how ssh drives a login
    shell. 'confined' marks the ambient confinement of the account:
    the session keeps its escape hatches (the editor) closed.
    'plain' forces the no-chrome reader, even when prompt_toolkit
    is available (a friend to slow links)."""
    if command is not None:
        interactive = False
        frontend = None
    if interactive is None:
        interactive = sys.stdin.isatty()
    session_class = Repl
    runner = None
    if interactive and not plain and frontend == 'shell':
        from .shell import run_shell
        runner = run_shell
    elif interactive and not plain and frontend == 'console':
        from .console import ConsoleRepl, run_console
        session_class = ConsoleRepl
        runner = run_console
    session = session_class(client, project_name=project_name,
                            view=view, interactive=interactive,
                            project_path=project_path,
                            project_type=project_type,
                            view_root_rel=view_root_rel,
                            confined=confined, history_max=history_max)
    if runner is not None:
        return runner(session)
    if interactive:
        if plain:
            return run_interactive_plain(session)
        return run_interactive(session)
    return run_pipe(session, command)
