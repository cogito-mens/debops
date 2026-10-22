# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""The 'debops mcp repl' client: the MCP tools for a human.

The verbs map onto the tools of the project's daemon one to one.
The execution verbs poll the task the server returns and print its
status tail as it moves; a confirmation gate asks the human. Pipes
work: commands on standard input, answers on standard output.
"""

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
import time

from . import server as _server
from .client import UnixClient, default_socket_path
from .trust import TRUST_PATH

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
    except EOFError:
        return default
    reply = reply.strip().lower()
    if not reply:
        return default
    return reply in ('y', 'yes')


def connect_or_spawn(socket_path, project, docs=False, spawn=None,
                     interactive=False, legacy=False, err=sys.stderr):
    """The client for the daemon at the socket, spawning it when it
    is missing.

    'spawn' is None (ask when interactive, spawn quietly in a pipe),
    True (never ask) or False (never spawn). The spawned daemon is
    detached: it keeps running for the next client. With 'legacy'
    the REPL speaks the 2025-era protocol revision, to match a
    daemon started with '--legacy'.
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
        return connected
    if spawn is None:
        if interactive:
            spawn = ask('No MCP daemon is running for this project. '
                        'Spawn one, detached? (socket: {})'
                        .format(socket_path), default=True)
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


def stop_daemon(socket_path, out=sys.stdout):
    """Stop the daemon at the socket; the client-spawned ones only.

    The pid sidecar names the process and, on its second line, the
    daemon's origin: only a 'spawned' one is reaped from here — a
    daemon started by hand is refused (the answer names the pid, so
    the operator can still act). This is a guardrail, not a boundary:
    a same-account process can always kill(2) its own daemons, and a
    daemon on another account is unreachable either way.
    """
    pid_path = socket_path + '.pid'
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
    if len(lines) < 2 or lines[1] != 'spawned':
        raise ReplError('the daemon at {} (pid {}) was not spawned by '
                        'a client; stop it by hand if it must go: '
                        'kill {}'.format(socket_path, pid, pid))
    # A recycled pid is not ours to kill
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
    os.kill(pid, 15)
    # The shutdown drains the in-flight work first
    deadline = time.monotonic() + 5
    while os.path.exists(pid_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    if os.path.exists(pid_path):
        print('the daemon (pid {}) is draining its in-flight work'
              .format(pid), file=out)
    else:
        print('the daemon (pid {}) stopped'.format(pid), file=out)


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


class Repl(object):
    """One interpreter session against a daemon."""

    def __init__(self, client, project_name=None, view=None,
                 out=sys.stdout, err=sys.stderr, interactive=False,
                 project_path=None):
        self.client = client
        self.project_name = project_name
        self.view = view
        self._out = out
        self._err = err
        self.interactive = interactive
        self.had_error = False
        self._completion_cache = {}
        self.project_path = project_path
        self.elevated = False

    @property
    def prompt(self):
        name = self.project_name or 'docs'
        view = ':{}'.format(self.view) if self.view else ''
        marker = '!' if self.elevated else ''
        return 'debops{} {}{}> '.format(marker, name, view)

    # -- the wire -------------------------------------------------

    def _request(self, method, params):
        response = self.client.request(method, params)
        if 'error' in response:
            details = (response['error'].get('data', {}) or {})
            message = details.get('details') or response['error'].get(
                    'message', 'unknown error')
            raise ReplError(message)
        return response.get('result') or {}

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
        decline comes back as the tool's error result)."""
        result = self._request('tools/call',
                               {'name': name, 'arguments': arguments})
        while result.get('resultType') == 'input_required':
            answers = self._confirm(result)
            result = self._request(
                    'tools/call',
                    {'name': name, 'arguments': arguments,
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
        result = self._call_tool('view.switch', {'view': args[0]})
        if not result.get('isError'):
            # The prompt and the completions follow the view
            self.view = args[0]
            self._completion_cache.clear()
        return result

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
        self._request('server/elevate', {'passphrase': phrase})
        self.elevated = True
        print('elevated; the confirmation gates answer themselves '
              'for a while of active use', file=self._err)

    def cmd_drop(self, args):
        """End the elevation ahead of its timeout."""
        if args:
            raise ReplError('drop takes no arguments')
        if not self.elevated:
            raise ReplError('not elevated')
        if self.client.bearer is not None and not self.client.legacy:
            self._request('server/drop', {})
        self.elevated = False
        print('dropped; the gates ask again', file=self._err)

    def cmd_watch(self, args):
        """Stream the activity of the server's clients until
        Ctrl-C."""
        if args:
            raise ReplError('watch takes no arguments')
        if self.client.legacy:
            raise ReplError('the 2025-era protocol has no '
                            'subscription streams')
        if not self._ensure_trusted():
            raise ReplError("watch needs a trusted session: the "
                            "activity stream is the operator's view "
                            'of the other clients')
        print('watching the server activity (Ctrl-C ends it)',
              file=self._err)
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
            print('watch ended', file=self._err)
        except (EOFError, RuntimeError) as errmsg:
            raise ReplError(str(errmsg))

    def cmd_edit(self, args):
        """Open a project file in $VISUAL/$EDITOR and write the
        changes back through the tools (committed by the server)."""
        if len(args) != 1:
            raise ReplError('usage: edit <path>')
        if not self.interactive:
            raise ReplError('edit needs a terminal')
        path = args[0]
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
            current = ''.join(block.get('text', '') for block in
                              result.get('content', []))
        fd, tmp = tempfile.mkstemp(prefix='debops-edit-',
                                   suffix='-' + os.path.basename(path))
        try:
            with os.fdopen(fd, 'w') as fp:
                fp.write(current)
            editor = (os.environ.get('VISUAL') or
                      os.environ.get('EDITOR') or 'vi')
            returncode = subprocess.call(shlex.split(editor) + [tmp])
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
            print('unchanged: ' + path, file=self._err)
            return
        result = self._call_tool('file.write',
                                 {'path': path, 'content': new})
        self._render(result)

    def _passphrase(self):
        """The trust passphrase: the file when the account reads it,
        a prompt otherwise."""
        if self.project_path is not None:
            try:
                with open(os.path.join(self.project_path,
                                       TRUST_PATH)) as fp:
                    return fp.read().strip()
            except OSError:
                pass
        if not self.interactive:
            raise ReplError('the trust passphrase of the project is '
                            'not readable here; a pipe cannot ask '
                            'for it')
        try:
            return getpass.getpass('the trust passphrase: ')
        except EOFError:
            raise ReplError('cancelled')

    def _ensure_trusted(self):
        """Authenticate the session when the passphrase is reachable;
        True when trusted."""
        if self.client.bearer is not None:
            return True
        if self.client.legacy:
            return False
        try:
            phrase = self._passphrase()
        except ReplError:
            return False
        try:
            answer = self._request('server/authenticate',
                                   {'passphrase': phrase})
        except ReplError:
            return False
        self.client.bearer = answer['token']
        return True

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
        arguments = {'path': args[0]} if args else {}
        return self._call_tool('project.files', arguments)

    def cmd_read(self, args):
        if len(args) != 1:
            raise ReplError('usage: read <path>')
        return self._call_tool('project.read', {'path': args[0]})

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
        # A verb which printed its own answer returns None
        if result is not None:
            self._render(result)

    def _print_help(self):
        print('Commands (the MCP tools of the project, for a human):',
              file=self._out)
        for verb, synopsis in sorted(HELP.items()):
            print('  {:24s} {}'.format(verb, synopsis), file=self._out)

    # -- the completion candidates ----------------------------------

    # The verbs sharing one candidate source
    _CANDIDATE_SOURCES = {
        'check': 'playbooks', 'run': 'playbooks',
        'playbook': 'playbooks',
        'view': 'views',
        'role': 'roles', 'defaults': 'roles', 'vars': 'roles',
        'tasks': 'roles', 'handlers': 'roles',
        'read': 'files', 'edit': 'files',
    }

    def complete_candidates(self, verb):
        """The completion candidates of a verb, cached per view."""
        source = self._CANDIDATE_SOURCES.get(verb)
        if source is None:
            return []
        if source not in self._completion_cache:
            self._completion_cache[source] = self._load_candidates(verb)
        return self._completion_cache[source]

    def _load_candidates(self, verb):
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
            if verb == 'read':
                result = self._request('tools/call',
                                       {'name': 'project.files',
                                        'arguments': {}})
                return list(result.get('structuredContent') or [])
        except (ReplError, ConnectionError, EOFError, OSError):
            return []
        return []


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
    'files [directory]': 'the project files',
    'read <path>': 'read a project file',
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
             '(a trusted session; Ctrl-C ends it)',
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


class _Completer(_PtCompleter):
    """The prompt_toolkit completer: the verbs, then the candidates
    of the verb (playbook targets, role names, files, views)."""

    def __init__(self, repl):
        self._repl = repl

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion
        before = document.text_before_cursor
        words = before.split()
        # The fragment being completed is the last token, unless the
        # line ends with a space ('--' fragments survive the split,
        # which the word extraction of the document would not)
        if words and not before.endswith(' '):
            word = words.pop()
        else:
            word = ''
        if not words:
            candidates = sorted(list(self._repl.verbs())
                                + ['help', 'exit'])
        elif words[0] in ('check', 'run') and word.startswith('--'):
            candidates = sorted(EXECUTION_OPTIONS)
        elif words[0] == 'doc' and word.startswith('--'):
            candidates = ['--type', '--list', '--snippet', '--json']
        else:
            candidates = self._repl.complete_candidates(words[0])
        for candidate in candidates:
            if candidate.startswith(word):
                yield Completion(candidate, start_position=-len(word))


def run_pipe(repl):
    """Commands on stdin, answers on stdout; a nonzero exit status
    notes an error."""
    for line in sys.stdin:
        try:
            repl.execute(line)
        except ReplError as errmsg:
            print('error: {}'.format(errmsg), file=repl._err)
            repl.had_error = True
        except EOFError:
            break
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
        from prompt_toolkit.history import FileHistory
    except ImportError:
        # The plain reader carries the session; completion just is
        # not there
        return run_interactive_plain(repl)
    history_dir = _history_dir()
    session = PromptSession(
            completer=_Completer(repl),
            # The shell habit: Tab inserts the completion, a double
            # Tab lists the candidates
            complete_style=CompleteStyle.READLINE_LIKE,
            history=FileHistory(os.path.join(history_dir,
                                             'mcp-repl-history')))
    print(_banner(repl), file=repl._err)
    while True:
        try:
            line = session.prompt(repl.prompt)
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if _execute_line(repl, line):
            break
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


def run_interactive_plain(repl):
    """The no-dependency interactive loop: readline history when the
    module is there, plain input() either way."""
    try:
        import readline
    except ImportError:
        readline = None
    history = None
    if readline is not None:
        history = os.path.join(_history_dir(), 'mcp-repl-history')
        try:
            readline.read_history_file(history)
        except OSError:
            pass
        readline.set_history_length(1000)
    print(_banner(repl), file=repl._err)
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
            done = _execute_line(repl, line)
            if done:
                break
    finally:
        if history is not None:
            try:
                readline.write_history_file(history)
            except OSError:
                pass
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
        project_path=None):
    """Build and run an interpreter session against the connected
    daemon."""
    if interactive is None:
        interactive = sys.stdin.isatty()
    session = Repl(client, project_name=project_name, view=view,
                   interactive=interactive, project_path=project_path)
    if interactive:
        return run_interactive(session)
    return run_pipe(session)
