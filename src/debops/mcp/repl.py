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
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback

from . import server as _server
from .client import (UnixClient, default_socket_path,
                     socket_pid_path)
from .runs import current_span, path_key
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


class _HelpRequested(Exception):
    """The command asked for its help ('-h'/'--help'): carries the
    rendered text; the interpreter prints it and nothing runs."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


class _Option(object):
    """One option of a command spec: the short and long forms, the
    destination name, the value shape (a metavar when the option
    takes a value) and the help line."""

    def __init__(self, short, long, dest, metavar=None, help=''):
        self.short = short
        self.long = long
        self.dest = dest
        self.metavar = metavar
        self.help = help

    @property
    def takes_value(self):
        return self.metavar is not None

    def forms(self):
        """The option strings, for the help and the completion."""
        names = [name for name in (self.short, self.long)
                 if name is not None]
        if self.takes_value:
            return ', '.join(names) + ' ' + self.metavar
        return ', '.join(names)


class _CommandSpec(object):
    """The argument shape of a verb: the usage name, the verbose
    description for '-h', the positionals ((name, help, nargs)) and
    the options. The one table drives the parsing, the help and the
    completion."""

    def __init__(self, name, description='', positionals=(),
                 options=()):
        self.name = name
        self.description = description
        self.positionals = list(positionals)
        self.options = list(options)

    def option_named(self, long=None, short=None):
        for option in self.options:
            if long is not None and option.long == long:
                return option
            if short is not None and option.short == short:
                return option
        return None


def _usage(spec):
    """The wrapped usage line of a command, à la argparse."""
    parts = ['[-h]']
    for option in spec.options:
        form = option.short or option.long
        if option.takes_value:
            form += ' ' + option.metavar
        parts.append('[{}]'.format(form))
    for name, _help, nargs in spec.positionals:
        parts.append('{} [{} ...]'.format(name, name)
                     if nargs == '+' else name)
    return '\n'.join(textwrap.wrap(
            'usage: {} {}'.format(spec.name, ' '.join(parts)), 76,
            subsequent_indent='    ', break_on_hyphens=False,
            break_long_words=False))


def _help_rows(rows):
    """The name/help rows of a help section, aligned on a column;
    a help line longer than the room wraps under itself."""
    column = min(max(len(name) for name, _help in rows) + 2, 26)
    lines = []
    for name, help in rows:
        head = '  ' + name.ljust(column)
        if len(head) > 28:
            lines.append(head)
            lines.extend(textwrap.wrap(help, 78,
                                       initial_indent=' ' * 30,
                                       subsequent_indent=' ' * 30))
        else:
            lines.extend(textwrap.wrap(
                    help, 78, initial_indent=head,
                    subsequent_indent=' ' * len(head)))
    return lines


def _command_help(spec):
    """The verbose help of a command, à la argparse: the usage, the
    description, the positionals and the options."""
    lines = [_usage(spec)]
    if spec.description:
        lines.extend([''] + spec.description.splitlines())
    rows = []
    for name, help, _nargs in spec.positionals:
        rows.append((name, help))
    if rows:
        lines.extend(['', 'positional arguments:'])
        lines.extend(_help_rows(rows))
    if spec.options:
        option_rows = [('-h, --help', 'show this help')]
        option_rows.extend((option.forms(), option.help)
                           for option in spec.options)
        lines.extend(['', 'options:'])
        lines.extend(_help_rows(option_rows))
    return '\n'.join(lines)


def _parse_arguments(spec, tokens):
    """The tokens of a command against its spec: the (option values,
    positionals) pair. '-h'/'--help' raises _HelpRequested with the
    rendered help; a mistake raises ReplError with a pointer to it.

    The forms are the ansible-playbook ones: '-l VALUE' and
    '-lVALUE', '--limit VALUE' and '--limit=VALUE'; a flag never
    consumes the next token; '--' ends the option parsing.
    """
    values = {}
    positionals = []
    options_open = True
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if options_open and token == '--':
            options_open = False
        elif options_open and token in ('-h', '--help'):
            raise _HelpRequested(_command_help(spec))
        elif options_open and token.startswith('--'):
            name, equal, inline = token.partition('=')
            option = spec.option_named(long=name)
            if option is None:
                raise ReplError("unknown option: {!r} — try '{} -h'"
                                .format(name, spec.name))
            if not option.takes_value:
                if equal:
                    raise ReplError('{} takes no value'.format(name))
                values[option.dest] = True
            elif equal:
                values[option.dest] = inline
            else:
                index += 1
                if index >= len(tokens):
                    raise ReplError('{} needs a value'.format(name))
                values[option.dest] = tokens[index]
        elif options_open and token.startswith('-') and token != '-':
            short, attached = token[:2], token[2:]
            option = spec.option_named(short=short)
            if option is None or (attached and not option.takes_value):
                raise ReplError("unknown option: {!r} — try '{} -h'"
                                .format(token, spec.name))
            if attached:
                values[option.dest] = attached
            else:
                index += 1
                if index >= len(tokens):
                    raise ReplError('{} needs a value'.format(short))
                values[option.dest] = tokens[index]
        else:
            positionals.append(token)
        index += 1
    return values, positionals


# The execution options of 'check'/'run'/'job add': the set the
# daemon knows, with the ansible-playbook short forms; the opt-ins
# are marked — the project policy (project.mcp.execute_options)
# decides what passes, the daemon relays the refusal
_EXECUTION_OPTIONS = (
    _Option('-l', '--limit', 'limit', metavar='SUBSET',
            help='only run plays and tasks whose hosts match the '
                 'pattern'),
    _Option('-t', '--tags', 'tags', metavar='TAGS',
            help='only run tasks with these tags (a comma list)'),
    _Option(None, '--skip-tags', 'skip_tags', metavar='TAGS',
            help='skip tasks with these tags (a comma list)'),
    _Option('-f', '--forks', 'forks', metavar='N',
            help='this many parallel host processes (policy opt-in)'),
    _Option(None, '--start-at-task', 'start_at_task', metavar='NAME',
            help='start at the task with this name (policy opt-in)'),
    _Option('-u', '--user', 'user', metavar='NAME',
            help='the default connection user (a bootstrap override '
                 'of the policy)'),
    _Option(None, '--syntax-check', 'syntax_check',
            help='parse the playbooks and exit; nothing runs'),
    _Option(None, '--list-hosts', 'list_hosts',
            help='list the affected hosts and exit; nothing runs'),
    _Option(None, '--list-tasks', 'list_tasks',
            help='list the tasks and exit; nothing runs'),
    _Option(None, '--list-tags', 'list_tags',
            help='list the tags and exit; nothing runs'),
    _Option(None, '--force-handlers', 'force_handlers',
            help='run the notified handlers immediately '
                 '(policy opt-in)'),
    _Option(None, '--flush-cache', 'flush_cache',
            help='flush the local fact cache first (policy opt-in)'),
)

COMMAND_SPECS = {
    'check': _CommandSpec(
            'check',
            description=(
                    'Check the playbooks against the managed hosts '
                    'without changing\n'
                    'them (ansible-playbook --check): the tasks '
                    'report what they would\n'
                    'have changed.\n'
                    '\n'
                    'The run tallies the tasks first — the status '
                    "line's progress and\n"
                    'time estimate come from the tally — and a '
                    'project-local playbook\n'
                    'asks for confirmation before it runs. The '
                    'session blocks until\n'
                    'the run ends; Ctrl-C cancels the run.'),
            positionals=[('PLAYBOOK',
                          'the playbook target(s); a set name '
                          'expands to its playbooks', '+')],
            options=_EXECUTION_OPTIONS),
    'run': _CommandSpec(
            'run',
            description=(
                    'Execute the playbooks on the managed hosts '
                    '(ansible-playbook).\n'
                    '\n'
                    'The run tallies the tasks first — the status '
                    "line's progress and\n"
                    'time estimate come from the tally. The session '
                    'blocks until the\n'
                    'run ends; Ctrl-C cancels the run.'),
            positionals=[('PLAYBOOK',
                          'the playbook target(s); a set name '
                          'expands to its playbooks', '+')],
            options=_EXECUTION_OPTIONS),
    'job': _CommandSpec(
            'job add',
            description=(
                    "Queue a playbook run in a batch. The queue runs "
                    "a batch's jobs\n"
                    "in order once started ('queue start'); 'job "
                    "prune <id>' drops a queued\n"
                    "job, 'job del <batch>' removes a finished "
                    'batch.\n'
                    '\n'
                    'The mode word comes before the playbook, '
                    'UNIX-style; -m|--mode\n'
                    'names it explicitly — with the option, a '
                    "playbook named exactly\n"
                    "'check', 'run' or 'debug' stays a playbook. "
                    'The options are\n'
                    "the execution options of 'check' and 'run'."),
            positionals=[('BATCH', 'the batch to queue the job in',
                          '1'),
                         ('PLAYBOOK',
                          'the playbook target(s); a set name '
                          'expands to its playbooks', '+')],
            options=(_Option('-m', '--mode', 'mode', metavar='MODE',
                             help="the run mode: 'check', 'run' or "
                                  "'debug' (default: run)"),)
            + _EXECUTION_OPTIONS),
}


class ReplError(Exception):
    """A command-line mistake of the REPL; printed, never raised out."""


class TransientError(ReplError):
    """A dropped connection mid-wait: the daemon is busy, not dead.
    The wait loops retry it; the other errors stay fatal."""


def ask(message, default=False):
    """A yes/no question; the default answers on Enter and EOF.
    The answer is not a command — it never joins the readline
    recall (a passphrase at a gate would sit on the Up arrow)."""
    suffix = ' [Y/n] ' if default else ' [y/N] '
    try:
        reply = input(message + suffix)
    except (EOFError, KeyboardInterrupt):
        return default
    try:
        import readline
    except ImportError:
        readline = None
    if readline is not None:
        # input() feeds the recall; a question's answer drops back out
        length = readline.get_current_history_length()
        if length > 0 and readline.get_history_item(length) == reply:
            readline.remove_history_item(length - 1)
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


def daemon_alive(socket_path):
    """Whether something accepts connections at the socket path — a
    bare connect, no protocol spoken (the bridge is era-agnostic)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def spawn_daemon(socket_path, project, docs=False, legacy=False,
                 err=sys.stderr, spawn_options=()):
    """Spawn the daemon detached at the well-known socket; raise
    ReplError when it does not come up. 'spawn_options' carries the
    preset daemon options of the project (the filtered
    'project.autospawn_options')."""
    # The client creates the runtime directory: the daemon's own log
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
    spawn_daemon(socket_path, project, docs=docs, legacy=legacy,
                 err=err, spawn_options=spawn_options)
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


def tool_arguments(tokens):
    """The 'tool/<name>' hatch arguments: key=value pairs.

    'true'/'false' are booleans, integers are numbers, everything
    else is a string; a repeated key gathers the values into a list.
    """
    arguments = {}
    for token in tokens:
        if '=' not in token:
            raise ReplError('the tool arguments are key=value pairs; '
                            'got {!r}'.format(token))
        key, value = token.split('=', 1)
        if not key:
            raise ReplError('the tool arguments are key=value pairs; '
                            'got {!r}'.format(token))
        if value in ('true', 'false'):
            value = value == 'true'
        else:
            try:
                value = int(value)
            except ValueError:
                pass
        if key in arguments:
            previous = arguments[key]
            if not isinstance(previous, list):
                arguments[key] = [previous]
            arguments[key].append(value)
        else:
            arguments[key] = value
    return arguments


def execution_arguments(tokens, spec):
    """The check/run arguments: the targets plus the policy options,
    parsed against the verb's spec (the ansible-playbook short and
    long forms)."""
    arguments, playbooks = _parse_arguments(spec, tokens)
    if not playbooks:
        raise ReplError("a playbook target is required — try '{} -h'"
                        .format(spec.name))
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


def _format_duration(seconds):
    """A duration as the humans write it: 0:30, 4:05, 1:12:00."""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return '{}:{:02d}:{:02d}'.format(hours, minutes, secs)
    return '{}:{:02d}'.format(minutes, secs)


def _run_remaining(elapsed, done, total, estimate):
    """The time-to-completion of a run, in seconds: the elapsed time
    extrapolated over the tally — skipped tasks race through their
    banners, so the extrapolation shrinks as they count — or the
    probe's static estimate while the first task has not landed.
    None when nothing can say."""
    if done and total:
        return max(0.0, elapsed * (total - done) / done)
    if estimate:
        return max(0.0, estimate - elapsed)
    return None


def _run_verb(mode):
    return {'check': 'Checking', 'run': 'Running',
            'debug': 'Debugging'}.get(mode, 'Running')


def _run_verb_of(run):
    """The display verb of a run dict: its phase while it prepares
    (the gates, the tally probe), its mode's verb once it runs."""
    if run.get('phase'):
        return run['phase']
    mode = run.get('mode')
    return _run_verb(mode) if mode else 'Preparing'


def _remaining_playbooks(playbooks, done, counts):
    """The playbooks still on the status line: the running one (its
    tally span holds the task position) and the ones after it,
    dropping one by one as the spans pass; the whole list when the
    spans are unknown. The last one stays while its recap prints
    (the span walk clamps to it)."""
    if not playbooks:
        return []
    if counts is None or len(counts) != len(playbooks):
        return list(playbooks)
    span = current_span(counts, done or 0)
    if span is None:
        return list(playbooks[-1:])
    return list(playbooks[span:])


def _ellipsize_middle(names, budget):
    """The playbook list fitted to the budget: whole names from the
    front and the back in alternation, the middle elided with an
    ellipsis; the running (first) name survives any budget."""
    if not names:
        return ''
    full = ' '.join(names)
    if budget is None or len(full) <= budget:
        return full
    room = max(1, budget - 3)  # the ' … ' between the two ends
    front, back = [], []
    left, right = 0, len(names) - 1
    turn_front = True
    taken = 0
    while left <= right:
        name = names[left] if turn_front else names[right]
        extra = len(name) + (1 if front or back else 0)
        if taken + extra > room:
            break
        if turn_front:
            front.append(name)
            left += 1
        else:
            back.insert(0, name)
            right -= 1
        taken += extra
        turn_front = not turn_front
    if not front and not back:
        # An absurd budget: the running name, hard-cut
        return names[0][:max(1, budget)]
    return ' '.join(front + ['…'] + back)


def _foreground_run(command, shell):
    """Run the command with the terminal handed over, the way the
    line editors do '!cmd': the child in its own foreground process
    group, so its job control (Ctrl-C, Ctrl-Z) is its own."""
    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        completed = subprocess.run([shell, '-c', command],
                                   capture_output=True, text=True)
        if completed.stdout:
            print(completed.stdout, end='')
        if completed.stderr:
            print(completed.stderr, end='', file=sys.stderr)
        return completed.returncode
    child = subprocess.Popen([shell, '-c', command],
                             start_new_session=True)
    saved = os.tcgetpgrp(fd)
    try:
        os.tcsetpgrp(fd, child.pid)
    except OSError:
        pass
    try:
        return child.wait()
    finally:
        try:
            os.tcsetpgrp(fd, saved)
        except OSError:
            pass


# -- the ansible output coloring ------------------------------------

# The line classes of the ansible output, mapped to the ANSI codes
# ansible itself assigns by default (its [colors] configuration).
# The table is the theme: another front end maps the same classes
# into its own style vocabulary (the console: the prompt_toolkit
# styles). None leaves the line plain — the banners and the recap
# line are uncolored upstream
ANSI_THEME = {
    'ok': '0;32',
    'changed': '0;33',
    'failed': '0;31',
    'unreachable': '1;31',
    'skipped': '0;36',
    'warning': '1;35',
    'deprecation': '0;35',
    'error': '0;31',
    'debug': '1;30',
    'verbose': '0;34',
    'diff-add': '0;32',
    'diff-remove': '0;31',
    'diff-lines': '0;36',
    'retry': '1;30',
    'header': None,
    'recap': None,
    'plain': None,
}

_RESULT_LINE = re.compile(
        r'^(ok|changed|fatal|failed|unreachable|skipping)\b')
_RETRY_LINE = re.compile(r'^FAILED - RETRYING: ')
_HEADER_LINE = re.compile(
        r'^(PLAY \[|TASK \[|PLAY RECAP|PLAYBOOK RECAP|CHECK MODE'
        r'|DRY RUN)')
_RECAP_LINE = re.compile(r'^\S+\s+: ok=\d+')
_WARNING_LINE = re.compile(r'^\[(WARNING|DEPRECATION WARNING|ERROR)\]')
_DIFF_OPEN = re.compile(r'^(--- |\+\+\+ |@@ )')
_RECAP_COUNT = re.compile(
        r'\b(ok|changed|unreachable|failed|skipped|rescued|ignored)'
        r'=(\d+)')
# The face of a recap field, by ansible's own [colors] defaults
_RECAP_FACE = {'ok': 'ok', 'changed': 'changed',
               'unreachable': 'unreachable', 'failed': 'failed',
               'skipped': 'skipped', 'rescued': 'ok',
               'ignored': 'warning'}
_BANNER_STARS = re.compile(r'^(.*?)( \*+)$')


def classify_ansible_line(line):
    """The class of an ansible output line: 'ok', 'changed',
    'failed', 'unreachable', 'skipped', 'retry', 'header',
    'warning', 'deprecation', 'error', 'recap' — or 'plain'."""
    if _HEADER_LINE.match(line):
        return 'header'
    warning = _WARNING_LINE.match(line)
    if warning is not None:
        return {'WARNING': 'warning', 'DEPRECATION WARNING':
                'deprecation', 'ERROR': 'error'}[warning.group(1)]
    if _RETRY_LINE.match(line):
        return 'retry'
    if _RECAP_LINE.match(line):
        return 'recap'
    result = _RESULT_LINE.match(line)
    if result is not None:
        word = result.group(1)
        if word in ('fatal', 'failed'):
            return 'failed'
        if word == 'skipping':
            return 'skipped'
        return word
    return 'plain'


class AnsibleColorizer(object):
    """The ansible output, colored the way ansible itself colors a
    terminal. The theme maps the line classes to ANSI codes (None
    leaves the line plain). The diff block is stateful: it opens on
    the '---'/'+++'/'@@' lines and ends at the first line outside
    its shapes.

    A multi-line block keeps its color until the next recognized
    line class: the continuation of a deprecation warning reads as
    one block, like ansible's own.

    With 'width' (a callable), the banner lines are re-padded to the
    client's terminal at print time — the log keeps the width the
    run recorded, the live run pretends the local one."""

    # The classes whose blocks span several lines
    _STICKY = frozenset(('warning', 'deprecation', 'error', 'failed',
                         'unreachable'))

    def __init__(self, theme=ANSI_THEME, width=None):
        self.theme = theme
        self._width = width
        self._in_diff = False
        self._sticky = None

    def _wrap(self, cls, line):
        code = self.theme.get(cls)
        if code is None:
            return line
        return '\x1b[{}m{}\x1b[0m'.format(code, line)

    def colorize(self, line):
        if self._in_diff:
            if _DIFF_OPEN.match(line):
                return self._wrap('diff-lines', line)
            if line.startswith('+'):
                return self._wrap('diff-add', line)
            if line.startswith('-'):
                return self._wrap('diff-remove', line)
            # The diff ends at the first line outside its shapes
            self._in_diff = False
        elif _DIFF_OPEN.match(line):
            self._in_diff = True
            return self._wrap('diff-lines', line)
        cls = classify_ansible_line(line)
        if cls == 'plain':
            # A multi-line block (a warning's explanation, a failed
            # task's module output) keeps its color until the next
            # recognized line
            if self._sticky is not None:
                return self._wrap(self._sticky, line)
            return line
        self._sticky = cls if cls in self._STICKY else None
        if cls == 'header':
            return self._repad(self._wrap(cls, line))
        if cls == 'recap':
            return self._color_recap(line)
        return self._wrap(cls, line)

    def _repad(self, line):
        """The banner padded to the client's terminal width; the log
        keeps the run's own width. Ansible pads with '*' to the
        terminal's columns."""
        if self._width is None:
            return line
        width = self._width()
        if not width:
            return line
        match = _BANNER_STARS.match(line)
        if match is None:
            return line
        text = match.group(1)
        return text + ' ' + '*' * max(0, width - len(text) - 1)

    def _color_recap(self, line):
        """The recap line, by ansible's own rules: every count takes
        its face when nonzero, and the host takes its outcome's
        face."""
        counts = dict((match.group(1), int(match.group(2)))
                      for match in _RECAP_COUNT.finditer(line))

        def _count(match):
            if int(match.group(2)) == 0:
                return match.group(0)
            return self._wrap(_RECAP_FACE[match.group(1)],
                              match.group(0))

        prefix = ''
        host_match = re.match(r'^(\S+)(\s+: )', line)
        if host_match is not None:
            face = ('failed' if (counts.get('failed', 0)
                                 or counts.get('unreachable', 0))
                    else 'changed' if counts.get('changed', 0)
                    else 'ok')
            prefix = self._wrap(face, host_match.group(1))
            line = line[len(host_match.group(1)):]
        return prefix + _RECAP_COUNT.sub(_count, line)


class Repl(object):
    """One interpreter session against a daemon."""

    def __init__(self, client, project_name=None, view=None,
                 out=sys.stdout, err=sys.stderr, interactive=False,
                 project_path=None, project_type=None,
                 view_root_rel=None, confined=False,
                 history_max=0, untrusted=False):
        self.client = client
        self.project_name = project_name
        self.view = view
        self._out = out
        self._err = err
        self.interactive = interactive
        # The ambient confinement of the account: the escape hatches
        # of the session (the editor) stay closed
        self.confined = confined
        # '--untrusted': the trust passphrase file stays unread on
        # purpose — the prompt is the only way in (debugging the
        # untrusted paths)
        self.untrusted = untrusted
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
        # The '--elevated' session: the renewal event and the cached
        # passphrase (the daemon re-checks it on every renewal)
        self._elevate_stop = None
        self._elevate_phrase = None
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
        # A front end with a live interface (the shell, the console)
        # answers questions on the input line and drives the command
        # submission itself
        self.gate_input = False
        self._gate = None
        self._app = None
        # The note when a command is refused for another in flight;
        # the shell's Ctrl-C aborts its runs, the console's drops
        # their connections
        self.busy_note = ('a command is running; Ctrl-C drops its '
                          'connection\n')
        # The shell echoes its local ('!') commands inside the
        # handoff, so the child's output cannot beat the echo to the
        # screen; the console's transcript needs no such dance
        self._inline_local_echo = False
        # The server's tool surface ({name: summary}), fetched lazily
        # for the help and the completion; the 'tools' verb governs
        # their visibility in both
        self._server_tools = None
        self._tools_visible = False
        # The run progress rendering of the front end: 'tail' prints
        # the moving log tail (the repl), 'shell' adds the status
        # line segment (the shell), 'lines' floats the two status lines (the
        # console, driven by active_run)
        self.run_display = 'tail'
        # The run currently tracked: {'run': the task's bookkeeping,
        # 'started': the monotonic first sighting, 'task_id': the
        # task's handle}; None at rest
        self.active_run = None
        # The run's live log tail, on a side connection of its own
        # (like the watch's): the run's own output instead of the
        # status digests
        self._tail_client = None
        self._tail_thread = None
        # A standalone 'log --follow' tail, for the front ends whose
        # Ctrl-C binding ends it
        self._follow_client = None
        # A run started elsewhere, from the status poller of a front
        # end with a status line (the shell)
        self._foreign_run = None
        # The lines the run's tail printed (the skip-render decision)
        self._tail_lines = 0
        # The colorizer of the active tail; and the print futures of
        # the app-driven front ends, drained before the marker
        self._run_colorizer = None
        self._pending_prints = []
        # The coalescing queue of the run's output (the flicker
        # control of the app front ends)
        self._pending_lines = []
        self._print_lock = threading.Lock()
        self._last_flush = 0.0
        # The output color theme of the session (the line-class to
        # ANSI table of ansible's own defaults); None follows the
        # terminal capability
        self.theme = None

    def _state_marks(self):
        """The state marks of a prompt: the elevation bang and the
        terminator (the root shell's '#' replaces '>')."""
        return ('!' if self.elevated else '',
                '#' if os.geteuid() == 0 else '>')

    @property
    def prompt(self):
        """The repl's prompt: the location between the name and the
        terminator; the marks tell the elevation and the root shell,
        the brackets the confined session."""
        name = self.project_name or 'docs'
        view = ':{}'.format(self.view) if self.view else ''
        bang, terminator = self._state_marks()
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
        marker = 'debops{} {}{}{}{}'.format(bang, name, view, place,
                                            terminator)
        if self.confined:
            marker = '[{}]'.format(marker)
        return marker + ' '

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
        if self.confined:
            # The confined session keeps its own recall: a testing
            # run's commands never mix into the operator's history
            name += '.confined'
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

    def status_line(self, width=None):
        """The session state for a status bar: the place on the left,
        the trust state on the right. The shell renders it below the
        input line; the plain interpreter never calls it. With the
        bar's width, the run segment keeps to its two thirds."""
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
        if self.run_display == 'shell':
            run_segment = None
            # The trust text shares the right side; the run segment
            # counts it into its two-thirds budget
            reserved = len(right) + 3 if right else 0
            if self.active_run is not None:
                # Our own run reports through its task polls
                run = self.active_run['run']
                run_segment = self._run_segment(
                        _run_verb_of(run).lower(), run.get('playbooks'),
                        run.get('taskIndex'), run.get('taskCount'),
                        run.get('taskCounts'),
                        elapsed=time.monotonic()
                        - self.active_run['started'],
                        estimate=run.get('estimateSeconds'),
                        width=width, reserved=reserved)
            elif self._foreign_run is not None:
                # A run started elsewhere (the queue, an agent,
                # another client), from the status poller
                foreign = self._foreign_run
                run_segment = self._run_segment(
                        _run_verb_of(foreign).lower(),
                        foreign.get('playbooks'),
                        foreign.get('tasksDone'),
                        foreign.get('tasksTotal'),
                        foreign.get('taskCounts'),
                        elapsed=foreign.get('elapsedSeconds'),
                        estimate=foreign.get('estimateSeconds'),
                        width=width, reserved=reserved)
            if run_segment:
                right = (run_segment + ' │ ' + right) \
                        if right else run_segment
        return left, right

    def _run_segment(self, verb, playbooks, done, total, counts=None,
                     elapsed=None, estimate=None, width=None,
                     reserved=0):
        """The run on the status line: the verb and the playbooks
        still to run (dropping one by one as their tally spans
        pass), then the tally and the clock — the stable-width parts
        (the task and play names shift too fast to read). The
        time-to-completion is the client's own extrapolation,
        recalculated at every render. The playbook list elides its
        middle to keep the segment within two thirds of the bar —
        the place on the left keeps its share."""
        parts = [verb]
        if done is not None and total:
            parts.append('{}/{}'.format(done, total))
        if elapsed is not None:
            parts.append(_format_duration(elapsed))
            remaining = _run_remaining(elapsed, done, total, estimate)
            if remaining is not None:
                parts.append('~' + _format_duration(remaining))
        # (+1 for the space the listing takes after the verb)
        fixed = len(' │ '.join(parts)) + reserved + 1
        budget = None if width is None else max(
                8, int(width * 2 / 3) - fixed)
        listing = _ellipsize_middle(
                _remaining_playbooks(playbooks, done, counts), budget)
        if listing:
            parts[0] = '{} {}'.format(verb, listing)
        return ' │ '.join(parts)

    # -- the wire -------------------------------------------------

    def _request(self, method, params):
        try:
            response = self.client.request(method, params)
        except TimeoutError as errmsg:
            # A slow answer is not a dead daemon: a run can hold the
            # project lock for its whole duration. The client dropped
            # the connection; the next command starts a fresh one
            # (TimeoutError is an OSError; this clause goes first), and
            # a wait loop retries the drop rather than abandoning the
            # run it watches
            raise TransientError('{}; the daemon may be busy (a run in '
                                 'flight?) — the connection was dropped, '
                                 'retry shortly'.format(errmsg))
        except (ConnectionError, EOFError, OSError) as errmsg:
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
        """Ask a yes/no question; the default is 'no'. A live
        interface answers on its input line."""
        if self.gate_input:
            return bool(self._gate_ask(message))
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

    def _poll_foreign_run_once(self, watcher):
        """One status read of the poller: the foreign run lands in
        the session state (or clears). Skipped while our own run
        reports through its task polls."""
        if self.active_run is not None:
            return
        try:
            result = watcher.request(
                    'tools/call', {'name': 'project.status',
                                   'arguments': {}})
            structured = (result.get('result', {})
                          .get('structuredContent') or {})
            if (isinstance(structured, dict)
                    and structured.get('runInFlight') is True):
                self._foreign_run = structured
            else:
                self._foreign_run = None
        except (ReplError, ConnectionError, EOFError, OSError):
            # A dead daemon clears the display; the next tick retries
            self._foreign_run = None
        self._invalidate()

    def _poll_foreign_runs(self, watcher, stop):
        """Keep the status line honest about runs started elsewhere
        (the queue, an agent, another client): read the project
        status on a cadence — slow when idle, quicker while one is
        in flight, and each poll is an audit-log line, so the idle
        cadence stays slow."""
        while not stop.is_set():
            self._poll_foreign_run_once(watcher)
            cadence = 2 if self._foreign_run is not None else 5
            stop.wait(cadence)

    def _cancel_run(self, task_id=None):
        """Cancel a run task, ours (the default) or one started
        elsewhere: the signal rides a fresh side connection — the
        main one may be mid-request (a Ctrl-C can land inside a
        poll). A foreign run asks the daemon for the trust; the
        refusal lands as a note. Returns True when a run was
        cancelled."""
        if task_id is None:
            active = self.active_run
            if active is None:
                return False
            task_id = active.get('task_id')
            if task_id is None:
                return False
        try:
            # The fresh connection: the interrupted main one can be
            # mid-answer, and its buffer is not a clean channel
            side = self.client.clone()
            side.request('tasks/cancel', {'taskId': task_id})
            side.close()
        except (AttributeError, ConnectionError, EOFError, OSError):
            # The side connection failed; the main one still answers
            try:
                self._request('tasks/cancel', {'taskId': task_id})
            except ReplError as errmsg:
                self._note(str(errmsg))
                return False
        except ReplError as errmsg:
            # The daemon refused (a run started elsewhere asks for
            # the trust)
            self._note(str(errmsg))
            return False
        self._note('cancelling the run')
        return True

    def _track_run(self, task):
        """Follow the run bookkeeping of an execution task; the front
        ends render their progress from it. Returns the run dict when
        one is tracked."""
        run = task.get('run')
        if run is None:
            return None
        active = self.active_run
        if active is None:
            active = self.active_run = {'run': run,
                                        'started': time.monotonic(),
                                        'task_id': task.get('taskId')}
        else:
            active['run'] = run
        if self.run_display in ('tail', 'shell'):
            # The bookkeeping arrives in stages: the 'preparing'
            # phase names the mode and the playbooks first, the
            # execution id and the resolved paths land with the
            # run's own start. The prelude and the tail wait for
            # their parts; the tail's replay covers the gap either
            # way
            if not active.get('preluded') and run.get('playbookPaths'):
                self._run_prelude(run)
                active['preluded'] = True
            if not active.get('tailed'):
                if self._start_log_tail(run):
                    active['tailed'] = True
        if self.run_display in ('shell', 'lines'):
            # The live chrome (the shell's toolbar, the console's
            # status lines) reads the session state
            self._invalidate()
        return run

    # -- the run's live log tail --------------------------------------

    def _run_prelude(self, run):
        """The 'Executing Ansible playbooks:' list of the plain CLI,
        the paths through the presentation filter."""
        paths = run.get('playbookPaths') or []
        if not paths:
            return
        from .present import make_path_presenter
        present = make_path_presenter(())
        print('Executing Ansible playbooks:', file=self._out)
        for path in paths:
            print(present(path), file=self._out)

    def _color_output(self):
        """Colorize the run's output when it reaches a terminal;
        pipes and the plain text channels stay ANSI-free."""
        return (self.interactive
                and hasattr(self._out, 'isatty')
                and self._out.isatty())

    def _output_columns(self):
        """The terminal width of the output channel, when it is one;
        the live app's size first, the channel's own otherwise."""
        app = self._app
        if app is not None and app.is_running:
            try:
                return app.output.get_size().columns
            except Exception:
                pass
        try:
            return os.get_terminal_size(self._out.fileno()).columns
        except (OSError, ValueError, AttributeError):
            return None

    def _print_output_line(self, line):
        """One line of the run's own output; colored on a terminal.
        A live prompt application needs its own printer (its plain
        write sanitizes the escape bytes away — the ANSI reader
        re-reads ours into real attributes), and the bursts coalesce:
        one erase/redraw of the chrome per window instead of one per
        line, so fast output does not flicker."""
        colorizer = self._run_colorizer
        if colorizer is not None:
            line = colorizer.colorize(line)
        app = self._app
        if app is None or not app.is_running:
            print(line, file=self._out)
            return
        with self._print_lock:
            self._pending_lines.append(line)
        self._flush_output_lines()

    def _flush_output_lines(self, force=False):
        """Print the queued run-output lines as one block. The queue
        drains when the oldest line has waited out the coalescing
        window, when the poll loop ticks, and when the tail ends."""
        with self._print_lock:
            if not self._pending_lines:
                return
            now = time.monotonic()
            if not force and now - self._last_flush < 0.1:
                return
            self._last_flush = now
            lines = self._pending_lines
            self._pending_lines = []
        self._print_output_text(''.join(line + '\n' for line in lines))

    def _print_output_text(self, text):
        """A block of the run's output through the live app's
        printer."""
        app = self._app
        if app is None or not app.is_running:
            print(text, end='', file=self._out)
            return
        import asyncio
        from prompt_toolkit.application.run_in_terminal import (
                in_terminal)
        from prompt_toolkit.formatted_text import ANSI

        async def _print():
            async with in_terminal():
                app.print_text(ANSI(text))

        future = asyncio.run_coroutine_threadsafe(_print(), app.loop)
        self._pending_prints.append(future)

    def _start_log_tail(self, run):
        """Tail the run's log over a side connection: the run's own
        output, live, instead of the status digests. The replay of
        the log's start rides the stream, so a fast run still prints
        whole."""
        execution_id = run.get('executionId')
        if (execution_id is None or self._tail_client is not None
                or self.client.legacy):
            return False
        try:
            watcher = self.client.clone()
        except (AttributeError, ConnectionError, OSError):
            # No side connection — the status digests carry the run
            return False
        self._tail_client = watcher
        self._tail_lines = 0
        self._run_colorizer = (AnsibleColorizer(self.theme
                                                or ANSI_THEME,
                                                width=self._output_columns)
                               if self._color_output() else None)
        thread = threading.Thread(target=self._tail_loop,
                                  args=(watcher, execution_id),
                                  daemon=True)
        self._tail_thread = thread
        thread.start()
        return True

    def _tail_loop(self, watcher, execution_id):
        """Print the run's log lines as they stream; the server ends
        the stream when the run finalizes. On a terminal the lines
        take ansible's own colors."""
        try:
            for message in watcher.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'runLog': execution_id}}):
                if message.get('method') == 'notifications/runs/log':
                    self._print_output_line(
                            message.get('params', {}).get('line', ''))
                    self._tail_lines += 1
        except (EOFError, ConnectionError, OSError, RuntimeError):
            pass
        finally:
            watcher.close()

    def _stop_log_tail(self):
        """End the run's tail, draining it first: the last log lines
        land before the completion marker does."""
        watcher = self._tail_client
        thread = self._tail_thread
        if thread is not None:
            thread.join(timeout=3)
        if thread is not None and thread.is_alive():
            # The finalize stream-end never came (an old daemon);
            # drop the connection
            if watcher is not None:
                watcher.close()
            thread.join(timeout=3)
        # The lines still queued for the coalescing window print
        # before the marker
        with self._print_lock:
            leftover = self._pending_lines
            self._pending_lines = []
        if leftover:
            self._print_output_text(
                    ''.join(line + '\n' for line in leftover))
        # The app-driven prints of the tail queued on its loop; let
        # them land before the marker
        for future in self._pending_prints:
            try:
                future.result(timeout=3)
            except Exception:
                pass
        self._pending_prints = []
        self._tail_client = None
        self._tail_thread = None
        self._run_colorizer = None

    def _finish_run(self):
        """Close the progress rendering of a run: the tail drains,
        the chrome clears. The verdict is in the log itself (the
        recap, and on failure the sendoff banner) — nothing more
        prints."""
        self._stop_log_tail()
        self.active_run = None
        if self.run_display in ('shell', 'lines'):
            self._invalidate()

    def _wait_task(self, task):
        """Poll a task to its end; the run progress renders per the
        front end's 'run_display'. A dropped connection retries — the
        human's run keeps its watcher. Ctrl-C cancels the task."""
        task_id = task.get('taskId')
        if self.run_display == 'shell':
            # The daemon's answer to the request is the task handle
            # arriving; say so before the prompt goes quiet
            self._note('debops-mcp: request received, processing...')
        last_message = None
        try:
            while True:
                time.sleep(POLL_INTERVAL)
                try:
                    task = self._request('tasks/get', {'taskId': task_id})
                except TransientError:
                    # The run holds the daemon busy; the wait survives
                    continue
                self._track_run(task)
                # The coalesced output of the run flushes on the tick
                self._flush_output_lines(force=True)
                message = task.get('statusMessage')
                if (message and message != last_message
                        and self.run_display in ('tail', 'shell')
                        and self._tail_client is None):
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
                    result = task.get('result') or {}
                    tailed = (last_message is not None
                              or self._tail_lines > 0)
                    # A failed run answers as an error result; the
                    # verdict follows it
                    self._finish_run()
                    if result.get('isError'):
                        if tailed:
                            # The failure scrolled by in the tail and
                            # the sendoff banner names the code; no
                            # excerpt re-dump
                            self.had_error = True
                            return {'content': [], 'isError': False}
                        return result
                    if tailed and self.run_display in ('tail', 'shell'):
                        # The run was shown live; nothing closes it
                        # out — the sendoff banner is in the log
                        return {'content': [], 'isError': False}
                    return result
                if status in ('failed', 'cancelled'):
                    tailed = (last_message is not None
                              or self._tail_lines > 0)
                    self._finish_run()
                    error = task.get('error') or {}
                    if tailed and self.run_display in ('tail', 'shell'):
                        # The failure scrolled by live; the exit
                        # status still marks the session
                        self.had_error = True
                        return {'content': [], 'isError': True}
                    raise ReplError(error.get('message',
                                              'the execution failed'))
        except KeyboardInterrupt:
            self._cancel_run()
            self._finish_run()
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

    def _invalidate(self):
        """The repaint hook of a live chrome; the line readers have
        no app and nothing to repaint."""
        app = self._app
        if app is not None:
            app.invalidate()

    def _echo(self, text):
        """A line of the session transcript; the console writes it to
        the scrollback pane instead."""
        print(text, end='', file=self._out)

    def _prompt_marker(self):
        """The marker of the input line and the transcript echo; the
        elevation shows on it. The root shell convention holds ('#'
        for EUID 0), and the confined session wears brackets."""
        bang, terminator = self._state_marks()
        marker = 'debops' + bang + terminator
        if self.confined:
            marker = '[{}]'.format(marker)
        return marker

    def _console_marker(self):
        """The console's prompt chip: the state flags alone — the
        panes carry the place; the brackets mark the confined
        session."""
        bang, terminator = self._state_marks()
        chip = bang + terminator
        return '[{}]'.format(chip) if self.confined else chip

    # -- the input gate (questions answered on the input line) -------
    #
    # The front ends with a live interface ('gate_input') run the
    # commands on a worker thread; a question is answered on the
    # input line, Escape declines. The gate runs on the worker —
    # never the interface thread, which has to stay free to do the
    # answering.

    def _gate_ask(self, message, secret=False):
        """Ask in the transcript; the input line answers."""
        gate = {'secret': secret, 'answered': threading.Event(),
                'value': None}
        self._gate = gate
        self._echo(message + '\n')
        self._invalidate()
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
        self._invalidate()
        return True

    def _gate_decline(self):
        """Escape declines the pending question."""
        gate = self._gate
        if gate is None:
            return False
        gate['value'] = '' if gate['secret'] else False
        self._echo('\n')
        gate['answered'].set()
        self._invalidate()
        return True

    # -- the command submission (front ends with a live app) ---------

    def submit(self, line):
        """One input line: an answer when a question is pending, a
        command otherwise (one at a time)."""
        if self._gate is not None:
            self._gate_answer(line)
            return
        if self._busy:
            self._echo(self.busy_note)
            return
        if not line.strip():
            return
        self._busy = True
        thread = threading.Thread(target=self._execute_guarded,
                                  args=(line,), daemon=True)
        thread.start()

    def _execute_guarded(self, line):
        """Run one command on the worker thread; the interface stays
        alive and repaints as the answer lands. An unexpected error
        lands in the transcript — a traceback on the raw terminal
        would corrupt the display and cost the session its worker."""
        if not (self._inline_local_echo
                and line.lstrip().startswith('!')):
            self._echo('{} {}\n'.format(self._prompt_marker(), line))
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
            self._invalidate()
        if done:
            app = self._app
            if app is not None:
                app.exit()

    # -- the server tool surface (the 'tool/<name>' hatch) -----------

    def _fetch_tools(self):
        """The server's tools/list as {name: summary}; empty when the
        daemon cannot answer. The summary is the first sentence, cut
        to a help line."""
        if self._busy:
            # A command is holding the one connection
            return {}
        try:
            result = self._request('tools/list', {})
        except ReplError:
            return {}
        tools = {}
        for tool in result.get('tools') or []:
            description = (tool.get('description') or '').strip()
            summary = description.split('. ', 1)[0]
            if len(summary) > 76:
                summary = summary[:73].rstrip() + '...'
            tools[tool['name']] = summary
        return tools

    def _server_tool_list(self):
        """The cached tool surface; a failed fetch caches as empty so
        the completion does not hammer a dead daemon. The 'tools'
        verb refetches when it unhides the listing — the daemon may
        have restarted with a changed policy."""
        if self._server_tools is None:
            self._server_tools = self._fetch_tools()
        return self._server_tools

    def _tool_hatch(self, name, tokens):
        """The 'tool/<name>' hatch: the server's own tool surface,
        reached directly. The server answers an unknown name with its
        own pointer; the local cache is the listing's business, not
        the call's."""
        if not name:
            raise ReplError('usage: tool/<name> [key=value ...]')
        return self._call_tool(name, tool_arguments(tokens))

    def cmd_tools(self, args):
        """Toggle the server tools in the help and the completion."""
        if args:
            raise ReplError('tools takes no arguments')
        self._tools_visible = not self._tools_visible
        if self._tools_visible:
            self._server_tools = self._fetch_tools()
            self._note('the server tools are listed ({}) — call one '
                       "as tool/<name>, 'tools' again hides them"
                       .format(len(self._server_tools)))
        else:
            self._note('the server tools are hidden from the help '
                       'and the completion')

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
        return self._call_tool('check', execution_arguments(
                args, COMMAND_SPECS['check']))

    def cmd_queue(self, args):
        """The execution queue: its state ('queue'), or steer it
        ('queue start|stop|clear')."""
        if not args or args == ['list']:
            return self._call_tool('queue.list', {})
        if len(args) == 1 and args[0] in ('start', 'stop', 'clear'):
            return self._call_tool('queue.' + args[0], {})
        raise ReplError('usage: queue [list|start|stop|clear]')

    def cmd_batch(self, args):
        """A job batch of the queue: 'batch <name>' shows it;
        'batch create|remove|prune|pending|accept <name>' steers
        it."""
        if len(args) == 1:
            return self._call_tool('batch.info', {'name': args[0]})
        if len(args) == 2 and args[0] in ('create', 'remove',
                                          'prune', 'pending'):
            return self._call_tool('batch.' + args[0],
                                   {'name': args[1]})
        if len(args) == 2 and args[0] == 'accept':
            # The review mark clears; the daemon wants the trust
            # for it (the elevation answers it)
            return self._call_tool('batch.pending',
                                   {'name': args[1],
                                    'pending': False})
        raise ReplError('usage: batch [create|remove|prune|pending|'
                        'accept] <name>  (or: batch <name>)')

    def cmd_job(self, args):
        """A queue job: 'job add <batch> [check|run|debug] '
        '<playbook> [...] [the run options]', 'job prune <id>' or
        'job del <batch>'.

        The mode word comes before the playbook, UNIX-style; the
        -m|--mode option names it explicitly — with the option the
        word is not consumed, so a playbook named exactly 'check',
        'run' or 'debug' stays a playbook.
        """
        if args in (['-h'], ['--help']):
            raise _HelpRequested(_command_help(COMMAND_SPECS['job']))
        if args and args[0] == 'add':
            values, positionals = _parse_arguments(
                    COMMAND_SPECS['job'], args[1:])
            if len(positionals) < 2:
                raise ReplError('a batch and a playbook are required '
                                "— try 'job -h'")
            batch = positionals[0]
            playbooks = positionals[1:]
            mode = values.pop('mode', None)
            if mode is None:
                mode = 'run'
                if playbooks and playbooks[0] in ('check', 'run',
                                                  'debug'):
                    mode = playbooks.pop(0)
            elif mode not in ('check', 'run', 'debug'):
                raise ReplError("the mode is one of: 'check', 'run', "
                                "'debug'")
            if not playbooks:
                raise ReplError("a playbook target is required — try "
                                "'job -h'")
            job_args = values
            job_args['batch'] = batch
            job_args['mode'] = mode
            job_args['playbook'] = (playbooks[0]
                                    if len(playbooks) == 1
                                    else playbooks)
            return self._call_tool('job.add', job_args)
        if len(args) == 2 and args[0] == 'prune':
            return self._call_tool('job.prune', {'id': args[1]})
        if len(args) == 2 and args[0] == 'del':
            return self._call_tool('job.del', {'batch': args[1]})
        raise ReplError('usage: job add <batch> [check|run|debug] '
                        '[-m mode] <playbook> [...] [options] | '
                        'job prune <id> | job del <batch> '
                        "('job -h' details the options)")

    def cmd_run(self, args):
        return self._call_tool('run', execution_arguments(
                args, COMMAND_SPECS['run']))

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

    def elevate_for_session(self):
        """The '--elevated' session: trusted, then elevated, and the
        elevation renewed before it lapses — the gates answer
        themselves for the session's duration. The daemon re-checks
        the passphrase on every renewal (sudo-style), so the phrase
        stays in the session's memory meanwhile; 'drop' forgets it
        and ends the renewals."""
        if self.client.legacy:
            raise ReplError('trust needs the {} revision; this daemon '
                            'speaks the 2025-era protocol'
                            .format(_server.PROTOCOL_VERSION))
        if self.elevated:
            return
        phrase = self._passphrase()
        if self.client.bearer is None:
            answer = self._request('server/authenticate',
                                   {'passphrase': phrase})
            self.client.bearer = answer['token']
        answer = self._request('server/elevate',
                               {'passphrase': phrase})
        self.elevated = True
        expires_in = answer.get('expiresIn')
        self._elevated_until = (time.monotonic() + expires_in
                                if isinstance(expires_in,
                                              (int, float)) else None)
        self._elevate_phrase = phrase
        self._elevate_stop = threading.Event()
        thread = threading.Thread(target=self._elevate_keeper,
                                  daemon=True)
        thread.start()
        self._note('elevated for the session; the confirmation gates '
                   'answer themselves')

    def _end_session_elevation(self):
        """End the '--elevated' renewals and forget the cached
        phrase."""
        if self._elevate_stop is not None:
            self._elevate_stop.set()
            self._elevate_stop = None
        self._elevate_phrase = None

    def _elevate_keeper(self):
        """Renew the session elevation before it lapses, on a slow
        cadence; a lapse the daemon refuses ends the renewals with
        a note."""
        while not self._elevate_stop.wait(30):
            if not self._maybe_renew():
                return

    def _maybe_renew(self):
        """One renewal check of the keeper; False ends it. The
        renewal rides a side connection — the main one may be
        mid-request."""
        if not self.elevated:
            return False
        until = self._elevated_until
        if until is None or until - time.monotonic() > 120:
            return True
        try:
            side = self.client.clone()
            answer = side.request('server/elevate',
                                  {'passphrase': self._elevate_phrase})
            side.close()
        except (AttributeError, ConnectionError, EOFError, OSError):
            # The side connection failed; the main one may still
            # answer
            try:
                answer = self._request(
                        'server/elevate',
                        {'passphrase': self._elevate_phrase})
            except ReplError as errmsg:
                self._note('the elevation lapsed: {}'.format(errmsg))
                return False
        except ReplError as errmsg:
            self._note('the elevation lapsed: {}'.format(errmsg))
            return False
        expires_in = answer.get('expiresIn')
        if isinstance(expires_in, (int, float)):
            self._elevated_until = time.monotonic() + expires_in
        return True

    def cmd_drop(self, args):
        """End the elevation ahead of its timeout."""
        if args:
            raise ReplError('drop takes no arguments')
        if not self.elevated:
            raise ReplError('not elevated')
        self._end_session_elevation()
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
        digest = None
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
            structured = result.get('structuredContent') or {}
            current = structured.get('content')
            digest = structured.get('digest')
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
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        if new == current:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            self._note('unchanged: ' + path)
            return
        write_args = {'path': write_path, 'content': new}
        if digest is not None:
            # The write precondition: the file must not have changed
            # since the read, or the save would silently revert
            # whoever wrote it while the editor was open
            write_args['digest'] = digest
        try:
            result = self._call_tool('file.write', write_args)
        except ReplError:
            self._note('the write did not land; your edits are kept '
                       'as the local copy ' + tmp)
            raise
        if result.get('isError'):
            self._render(result)
            self._note('your edits are kept as the local copy {}; '
                       '"edit" again to re-base them on the fresh '
                       'content'.format(tmp))
            return
        try:
            os.unlink(tmp)
        except OSError:
            pass
        self._render(result)

    def _run_editor(self, path):
        """Open the editor on the temporary file; the console
        suspends its own interface for it."""
        editor = (os.environ.get('VISUAL') or
                  os.environ.get('EDITOR') or 'vi')
        return subprocess.call(shlex.split(editor) + [path])

    # -- the local shell (the '!' escape) ----------------------------

    def _run_local(self, command):
        """Run the command in the client-local shell: the '!'
        escape. The daemon never sees the line; the confinement of
        the account forbids it entirely (the editor's rule).

        The command runs in the client's own working directory — the
        REPL's virtual cwd is a server-side view."""
        if self.confined:
            raise ReplError('a confined session has no local shell')
        if not command:
            raise ReplError('usage: !<command>')
        shell = os.environ.get('SHELL') or '/bin/sh'
        if not self.interactive:
            # No terminal to hand over: the pipes print back
            completed = subprocess.run([shell, '-c', command],
                                       capture_output=True, text=True)
            if completed.stdout:
                print(completed.stdout, end='', file=self._out)
            if completed.stderr:
                print(completed.stderr, end='', file=self._err)
            if completed.returncode != 0:
                self._note('!{} — exit {}'.format(command,
                                                  completed.returncode))
            return
        app = self._app
        if app is None or not app.is_running:
            # The plain reader: the terminal is already free
            rc = _foreground_run(command, shell)
        else:
            rc = self._run_local_in_app(app, command, shell)
        if rc != 0:
            self._note('!{} — exit {}'.format(command, rc))

    def _run_local_in_app(self, app, command, shell):
        """The handoff with the chrome suspended: the child owns the
        screen until it exits, then the interface redraws."""
        import asyncio
        from prompt_toolkit.application.run_in_terminal import (
                in_terminal)

        async def _run():
            async with in_terminal():
                if self._inline_local_echo:
                    from prompt_toolkit.formatted_text import ANSI
                    app.print_text(ANSI('{} !{}\n'.format(
                            self._prompt_marker(), command)))
                return _foreground_run(command, shell)

        return asyncio.run_coroutine_threadsafe(
                _run(), app.loop).result()

    def _passphrase(self):
        """The trust passphrase: the file when the account reads it,
        a prompt when it exists but is unreadable (the cross-account
        case), a clean refusal when it is absent — a daemon without
        the file has nothing to check an answer against."""
        if self.project_path is not None and not self.untrusted:
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
        """The hidden passphrase prompt; the presentation hook — a
        live interface asks through its own input line."""
        if self.gate_input:
            phrase = self._gate_ask('the trust passphrase: ',
                                    secret=True)
            if not phrase:
                raise ReplError('cancelled')
            return phrase
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
        follow = False
        rest = []
        for token in args:
            if token in ('-f', '--follow'):
                follow = True
            else:
                rest.append(token)
        if len(rest) != 1:
            raise ReplError('usage: log <execution id> [-f|--follow]')
        try:
            execution_id = int(rest[0])
        except ValueError:
            raise ReplError('the execution id is a number: {!r}'
                            .format(rest[0]))
        if not follow:
            return self._call_tool('execution.output',
                                   {'id': execution_id})
        if self.client.legacy:
            raise ReplError('the 2025-era protocol has no '
                            'subscription streams')
        # Replay the log and follow a live run; the server ends the
        # stream at the run's finalization. Ctrl-C stops the tail —
        # the run itself is untouched
        watcher = self.client.clone()
        self._follow_client = watcher
        try:
            for message in watcher.request_streaming(
                    'subscriptions/listen',
                    {'notifications': {'runLog': execution_id}}):
                if message.get('method') == 'notifications/runs/log':
                    print(message.get('params', {}).get('line', ''),
                          file=self._out)
        except KeyboardInterrupt:
            self._note('the tail ends; the run is untouched')
        except (EOFError, ConnectionError, OSError, RuntimeError) \
                as errmsg:
            raise ReplError(str(errmsg))
        finally:
            self._follow_client = None
            watcher.close()

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
        stripped = line.strip()
        if stripped.startswith('!'):
            # A client-local command; the daemon never sees it
            self._run_local(stripped[1:].strip())
            return
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
            self._print_help(args)
            return
        if verb == 'debops' and args and (
                args[0] in self.verbs() or args[0] in ('help', 'exit')
                or args[0].startswith('tool/')):
            # Thirteen years of habit die hard: the prefix goes
            verb, args = args[0], args[1:]
        if verb.startswith('tool/'):
            result = self._tool_hatch(verb[5:], args)
        else:
            handler = self.verbs().get(verb)
            if handler is None:
                if verb == 'debops':
                    raise ReplError('the REPL is already debops; '
                                    'drop the prefix')
                raise ReplError("unknown verb: {!r} ('help' lists them)"
                                .format(verb))
            try:
                result = handler(args)
            except _HelpRequested as request:
                print(request.text, file=self._out)
                result = None
        if verb in ('edit', 'mkdir', 'rm', 'mv', 'cp') or \
                verb.startswith('tool/'):
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

    def _print_help(self, args=None):
        if args:
            spec = COMMAND_SPECS.get(' '.join(args)) or \
                    COMMAND_SPECS.get(args[0])
            if spec is not None:
                print(_command_help(spec), file=self._out)
            else:
                self._note('no detailed help for {!r} (the listing '
                           'is all there is)'.format(args[0]))
            return
        print('Commands (the MCP tools of the project, for a human; '
              "a verb takes '-h' for the verbose form):",
              file=self._out)
        entries = dict(HELP)
        entries.update(self.HELP_EXTRA)
        for verb, synopsis in sorted(entries.items()):
            print('  {:24s} {}'.format(verb, synopsis), file=self._out)
        if self._tools_visible:
            tools = self._server_tool_list()
            print('Server tools (call as tool/<name>, the arguments '
                  'are key=value pairs):', file=self._out)
            for name in sorted(tools):
                print('  {:32s} {}'.format('tool/' + name,
                                           tools[name]),
                      file=self._out)
        else:
            # The listing stays out of the way; the hatch answers
            # either way
            print('(the server tools are hidden — the "tools" verb '
                  "lists them; call one as tool/<name>)",
                  file=self._out)

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
    'check <targets> [options]': 'a check-mode run '
                                 '("check -h" details the options)',
    'run <targets> [options]': 'execute the playbooks (same '
                               'options; "run -h" details them)',
    'queue [start|stop|clear]': 'the execution queue and its state',
    'batch [verb] <name>': 'a job batch of the queue (verbs: '
                           'create, remove, prune, pending, accept; '
                           'bare shows it)',
    'job add <batch> [mode] <pb> [...]': 'a new queue job '
                                         '("job -h" details it; '
                                         'also: prune <id>, '
                                         'del <batch>)',
    'logs': 'the recorded executions',
    'log <id> [-f]': 'the output of an execution; --follow tails a live one',
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
    'tools': 'show/hide the server tools in the help and the '
             'completion (call one as tool/<name>)',
    '!<command>': 'run it in the client-local shell (the daemon '
                  'never sees it)',
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
        candidates = list(repl.verbs()) + ['help', 'exit']
        if repl._tools_visible:
            candidates.extend('tool/' + name
                              for name in repl._server_tool_list())
        return word, sorted(candidates)
    if word.startswith('-') and words[0] in COMMAND_SPECS:
        # The job options apply to its 'add' subcommand only
        if words[0] == 'job' and (len(words) < 2 or words[1] != 'add'):
            return word, []
        forms = ['-h', '--help']
        for option in COMMAND_SPECS[words[0]].options:
            forms.extend(name for name in (option.short, option.long)
                         if name is not None)
        return word, sorted(forms)
    if words[0] == 'doc' and word.startswith('--'):
        return word, ['--type', '--list', '--snippet', '--json']
    if words[0] in Repl._PATH_VERBS:
        return word, repl.path_candidates(words[0], word)
    return word, repl.complete_candidates(words[0])


def _completion_columns(names, width):
    """The candidates as readline lists them: sorted, column-major,
    padded to even columns; one string per row."""
    if not names:
        return []
    column_width = max(len(name) for name in names) + 2
    columns = max(1, width // column_width)
    rows = (len(names) + columns - 1) // columns
    lines = []
    for row in range(rows):
        line = []
        for column in range(columns):
            index = column * rows + row
            if index < len(names):
                line.append(names[index].ljust(column_width))
        lines.append(''.join(line).rstrip())
    return lines


def _print_completion_candidates(completions):
    """List the candidates above the prompt, readline-style. The
    print goes through the patch_stdout proxy when it is active (the
    shell) and through run_in_terminal otherwise: both erase the
    interface first, so a bottom toolbar stays below the listing —
    prompt_toolkit's own readline display writes behind the
    renderer's back and strands the toolbar in the middle of the
    list."""
    from prompt_toolkit.application import get_app
    from prompt_toolkit.application.current import get_app_or_none
    from prompt_toolkit.application.run_in_terminal import (
            run_in_terminal)
    from prompt_toolkit.patch_stdout import StdoutProxy
    names = sorted(completion.display_text or completion.text
                   for completion in completions)
    try:
        width = get_app().output.get_size().columns
    except Exception:
        width = 80
    text = '\n'.join(_completion_columns(names, width))
    if isinstance(sys.stdout, StdoutProxy):
        print(text)
        return
    app = get_app_or_none()
    if app is not None and app.is_running:
        run_in_terminal(lambda: print(text))
    else:
        print(text)


def _key_bindings(repl):
    """The session's keys: the readline-habit Tab (complete the one
    candidate or the common prefix, list the candidates otherwise),
    and Ctrl-Z job control where the account is free — the
    'enable_suspend' of prompt_toolkit carries it; a confined session
    gets a note — the default binding would insert a literal '^Z'
    into the input line, which neither wants."""
    from prompt_toolkit.completion import (CompleteEvent,
                                           get_common_complete_suffix)
    from prompt_toolkit.enums import DEFAULT_BUFFER
    from prompt_toolkit.filters import Condition, has_focus
    from prompt_toolkit.key_binding import KeyBindings
    bindings = KeyBindings()

    @bindings.add('c-z', filter=Condition(lambda: repl.confined))
    def _confined_suspend(event):
        repl._note('a confined session does not background')

    # Swallow the pager keys and their modified variants: they would
    # recall the history — and an unbound one self-inserts its raw
    # escape sequence into the input line. The scrollback they
    # suggest is the terminal's own (Shift-PgUp in the emulators,
    # the copy mode in tmux) — an inline prompt cannot move it
    @bindings.add('pageup')
    @bindings.add('pagedown')
    @bindings.add('s-pageup')
    @bindings.add('s-pagedown')
    @bindings.add('c-pageup')
    @bindings.add('c-pagedown')
    @bindings.add('c-s-pageup')
    @bindings.add('c-s-pagedown')
    def _no_history_jump(event):
        pass

    @bindings.add('c-i', filter=has_focus(DEFAULT_BUFFER))
    def _tab(event):
        buffer = event.current_buffer
        if buffer.completer is None:
            return
        completions = list(buffer.completer.get_completions(
                buffer.document, CompleteEvent(completion_requested=True)))
        if not completions:
            return
        common = get_common_complete_suffix(buffer.document,
                                            completions)
        if len(completions) == 1:
            buffer.delete_before_cursor(-completions[0].start_position)
            buffer.insert_text(completions[0].text)
        elif common:
            buffer.insert_text(common)
        else:
            _print_completion_candidates(completions)

    return bindings


class _Completer(_PtCompleter):
    """The prompt_toolkit completer: the verbs, then the candidates
    of the verb (playbook targets, role names, files, views). A
    finished word earns the trailing space of the shell habit — the
    next argument follows right away; a directory candidate stays
    open for the path below it."""

    def __init__(self, repl):
        self._repl = repl

    def get_completions(self, document, complete_event):
        from prompt_toolkit.completion import Completion
        word, candidates = _candidates_for(
                self._repl, document.text_before_cursor)
        for candidate in candidates:
            if candidate.startswith(word):
                yield Completion(
                        candidate if candidate.endswith('/')
                        else candidate + ' ',
                        start_position=-len(word),
                        display=candidate)


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
            key_bindings=_key_bindings(repl),
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


def _history_records(data):
    """The records of a history file's bytes: each record is its
    separator lines (a blank line, a '# timestamp') plus the '+'
    lines of the entry; a legacy plain line is a record of its own.
    Shared by the pruning and the dedup."""
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
    return records


def _history_record_entry(record):
    """The entry text of one parsed record: its '+' lines joined
    (a legacy plain line is its own record)."""
    parts = [line[1:] for line in record if line.startswith('+')]
    if parts:
        return ''.join(parts)[:-1]
    for line in record:
        if not line.startswith('#') and line.strip():
            return line[:-1] if line.endswith('\n') else line
    return None


def _write_history_records(path, records):
    with open(path, 'wb') as fp:
        for record in records:
            for line in record:
                fp.write(line.encode('utf-8'))


def _prune_history_file(path, max_entries):
    """Trim the history file to its newest 'max_entries' records.
    The retained records keep their bytes — timestamps included — so
    the format survives."""
    try:
        with open(path, 'rb') as fp:
            data = fp.read()
    except OSError:
        return
    records = _history_records(data)
    if len(records) <= max_entries:
        return
    _write_history_records(path, records[len(records) - max_entries:])


def _dedup_history_file(path, line):
    """Drop the earlier records of a line from the history file:
    the recall keeps the newest invocation only (the runs repeat —
    the tenth identical 'check site' is the one worth keeping). The
    record parse is the pruner's own."""
    try:
        with open(path, 'rb') as fp:
            data = fp.read()
    except OSError:
        return
    records = _history_records(data)
    kept = [record for record in records
            if _history_record_entry(record) != line]
    if len(kept) == len(records):
        return
    _write_history_records(path, kept)


def _dedup_readline(readline, line):
    """The readline recall drops the earlier copies of the line:
    the arrows find the newest invocation only. The fresh line sits
    at the end; the walk never touches it."""
    index = readline.get_current_history_length() - 1
    while index >= 1:
        # get_history_item is 1-based, remove_history_item 0-based
        if readline.get_history_item(index) == line:
            readline.remove_history_item(index - 1)
        index -= 1


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
                # A repeat drops its earlier records first: the
                # recall keeps the newest invocation only
                _dedup_history_file(self.filename, string)
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
                _dedup_readline(readline, line)
                try:
                    _dedup_history_file(repl.history_file, line)
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
        plain=False, untrusted=False, elevated=False):
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
                            confined=confined, history_max=history_max,
                            untrusted=untrusted)
    if elevated:
        # '--elevated': trusted, then elevated for the session's
        # duration; a session which cannot prove itself runs
        # untrusted, the note says why
        try:
            session.elevate_for_session()
        except ReplError as errmsg:
            print('note: the elevation failed: {}'.format(errmsg),
                  file=session._err)
    if runner is not None:
        return runner(session)
    if interactive:
        if plain:
            return run_interactive_plain(session)
        return run_interactive(session)
    return run_pipe(session, command)
