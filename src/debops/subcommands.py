# Copyright (C) 2020-2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2020-2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import os
import re
import shutil
import sys

# Detect the DebOps version from the Python module. If the version is not
# available, we are in a development environment in which case we use a stub
# version number to avoid errors.
try:
    from .__version__ import __version__
except ModuleNotFoundError:
    __version__ = "0.0.0"


# Instance names name a project directory and, after the last dash, an
# infrastructure view; they land in socket file names and unit names
_MCP_INSTANCE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def parse_mcp_instance(name):
    """Split an MCP instance name into (project, view).

    '<project>-<view>' splits at the last dash, so project names may
    contain dashes and view names may not. A name without a dash leaves
    the view as None; the project's default view then decides. Raises
    ValueError for names which cannot name a directory and a socket.
    """
    name = str(name or '')
    if not _MCP_INSTANCE_PATTERN.match(name):
        raise ValueError(
                'invalid instance name: {!r} (expected '
                "'<project>-<view>' or '<project>')".format(name))
    if '-' not in name:
        return name, None
    project, view = name.rsplit('-', 1)
    if not view:
        raise ValueError(
                'invalid instance name: {!r} (the view after the last '
                "'-' is empty)".format(name))
    return project, view


# argparse wraps the help at the terminal width minus two; the full
# width of a modern terminal is a strain to read, so cap it at the
# width an 80-column terminal would get. A narrower terminal still
# wins
HELP_WIDTH = 78


class HelpFormatter(argparse.HelpFormatter):
    """The help formatter of the subcommands: the capped wrap width."""

    def __init__(self, prog, indent_increment=2, max_help_position=24,
                 width=None):
        if width is None:
            width = min(shutil.get_terminal_size().columns - 2,
                        HELP_WIDTH)
        super().__init__(prog, indent_increment=indent_increment,
                         max_help_position=max_help_position,
                         width=width)


def _argument_parser(**kwargs):
    """The subcommand parsers share the comfortable help width."""
    return argparse.ArgumentParser(formatter_class=HelpFormatter,
                                   **kwargs)


class Subcommands(object):

    # The login-shell names ('debops-shell' in /etc/passwd): the
    # program name selects the MCP front end, since a login shell is
    # invoked without arguments — ssh runs the command form as
    # '<name> -c <commands>'.
    LOGIN_SHELLS = {'debops-repl': 'repl',
                    'debops-shell': 'shell',
                    'debops-console': 'console'}

    def __init__(self, args=None):
        name = os.path.basename(args[0]).lstrip('-')
        self.login_shell = self.LOGIN_SHELLS.get(name)
        if self.login_shell is not None:
            args = [args[0], 'mcp', self.login_shell] + list(args[1:])
        self.args = args

        self.global_parser = _argument_parser(add_help=False)
        self.global_parser.add_argument('-v', '--verbose', action="count",
                                        help='increase output verbosity '
                                             '(e.g., -vv is more than -v)')
        self.global_parser.add_argument('--project-dir', type=str,
                                        nargs='?', default=os.getcwd(),
                                        help='path to the project directory')

        parser = _argument_parser(
                parents=[self.global_parser],
                description="DebOps CLI",
                usage='''debops <section> [<args>]

Sections:
    project  manage project directories
    exec     run Ansible commands directly against hosts
    run      run Ansible playbook(s) against hosts
    check    run Ansible playbook(s) in check mode
    env      run shell commands in project environment
    config   display DebOps configuration options
    mcp      run a Model Context Protocol server or client''')

        parser.add_argument('section', nargs='?',
                            help='Section to run')
        parser.add_argument('--version', action='version',
                            version='%(prog)s {version}'
                                    .format(version=__version__))

        self._section = parser.parse_args(self.args[1:2])
        self.section = self._section.section
        if self.section is None:
            # A bare 'debops': the project's own start configuration
            # (or the help) is the Interpreter's call — the parser
            # is kept for it
            self._top_parser = parser
            self.args = self._section
            return
        if not hasattr(self, 'do_' + self._section.section):
            print('Error: unrecognized section:', self._section.section, file=sys.stderr)
            parser.print_help()
            sys.exit(1)
        getattr(self, 'do_' + self._section.section)()

    def add_bool_argument(self, parser, name, default=False,
                          required=False, help=None, no_help=None):
        group = parser.add_mutually_exclusive_group(
                required=required)
        group.add_argument('--' + name, dest=name,
                           help=help, action='store_true')
        group.add_argument('--no-' + name, dest=name,
                           help=no_help, action='store_false')
        parser.set_defaults(**{name: default})

    def do_project(self):
        parser = _argument_parser(
                description='manage project directory',
                usage='''debops project <command> [<args>]

Commands:
    init    initialize new project directory
    mkview  create a new infrastructure view
    commit  commit current changes in git repository
    pull    pull project updates from the git origin
    push    push project commits to the git origin
    refresh refresh existing project directory
    unlock  decrypt secrets in project directory
    lock    encrypt secrets in project directory
    venv    manage Python virtual environment''')
        parser.add_argument('command', help='project command to run')
        self._command = parser.parse_args(self.args[2:3])
        self.command = self._command.command
        if not hasattr(self, 'do_project_' + self._command.command):
            print('Error: unrecognized command:', self._command.command, file=sys.stderr)
            parser.print_help()
            sys.exit(1)
        getattr(self, 'do_project_' + self._command.command)()

    def add_spec_arguments(self, parser):
        """Add the options which read and apply an inventory file
        specification. Shared by the 'project' subcommands which can create
        or refresh an Ansible inventory."""
        parser.add_argument('--template', type=str, dest='spec_sources',
                            action='append', metavar='SPEC',
                            help='apply an inventory spec from '
                                 'template, file or stdin (multiple)')
        parser.add_argument('-n', '--dry-run', action='store_true',
                            help='only simulate changes from inventory '
                                 'spec')
        parser.add_argument('--force', action='store_true',
                            help='overwrite, clear or remove paths which '
                                 'already exist')
        parser.add_argument('--allow-io', action='store_true',
                            help='allow commands or file includes in '
                                 'inventory spec')
        inspect = parser.add_mutually_exclusive_group()
        inspect.add_argument('--graph', action='store_true',
                             help='display the Ansible inventory graph')
        inspect.add_argument('--list', action='store_true',
                             help='display the Ansible inventory as JSON')
        inspect.add_argument('--host', type=str, metavar='HOST',
                             help='display selected host variables as JSON')

    def do_project_init(self):
        parser = _argument_parser(
                description='initialize new project directory',
                usage='debops project init [<args>] <project_dir>')
        parser.add_argument('-t', '--type', type=str, nargs='?',
                            choices=['legacy', 'modern'],
                            default='legacy',
                            help='select project type (default: %(default)s)')
        parser.add_argument('-V', '--default-view', type=str,
                            default='system',
                            help='name of the default infrastructure view '
                                 '(default: %(default)s)')
        self.add_bool_argument(parser, 'git',
                               help='enable git support (default)',
                               default=True,
                               no_help='disable git support')
        self.add_bool_argument(parser, 'requirements',
                               help='install Ansible Collections after '
                                    'initialization (default)',
                               default=True,
                               no_help="don't install Ansible Collections")
        self.add_bool_argument(parser, 'venv',
                               help='enable Python virtual environment '
                                    '(modern projects only)',
                               default=False,
                               no_help="don't enable Python virtual "
                                       "environment")
        parser.add_argument('--encrypt', type=str, nargs='?',
                            choices=['encfs', 'git-crypt'],
                            help='enable encrypted secrets')
        parser.add_argument('--config', type=str, dest='config_templates',
                            action='append', metavar='NAME',
                            help='write the configuration template NAME '
                                 'into .debops/conf.d (modern projects '
                                 'only; may be given multiple times)')
        parser.add_argument('--keys', type=str,
                            help='list of GPG recipients with secret access, '
                                 'delimited by commas')
        self.add_spec_arguments(parser)
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_refresh(self):
        parser = _argument_parser(
                description='refresh existing project directory',
                usage='debops project refresh [<args>] <project_dir>')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to refresh')
        parser.add_argument('--config', type=str, dest='config_templates',
                            action='append', metavar='NAME',
                            help='write the configuration template NAME '
                                 'into .debops/conf.d (modern projects '
                                 'only; may be given multiple times; '
                                 '--force replaces an existing file)')
        self.add_spec_arguments(parser)
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_lock(self):
        parser = _argument_parser(
                description='encrypt secrets inside project directory',
                usage='debops project lock [<args>] <project_dir>')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to lock')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_unlock(self):
        parser = _argument_parser(
                description='decrypt secrets inside project directory',
                usage='debops project unlock [<args>] <project_dir>')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to unlock')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_mkview(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description='create new infrastructure view',
                usage='debops project mkview [<args>] <new_view>')
        parser.add_argument('--encrypt', type=str, nargs='?',
                            choices=['encfs', 'git-crypt'],
                            help='enable encrypted secrets')
        parser.add_argument('--keys', type=str,
                            help='list of GPG recipients with secret access, '
                                 'delimited by commas')
        self.add_spec_arguments(parser)
        parser.add_argument('new_view', type=str, nargs='?',
                            help='name of the new infrastructure view')
        self.args = parser.parse_args(self.args[3:])

    def do_project_commit(self):
        parser = _argument_parser(
                description='commit current changes in git repository',
                usage='debops project commit [<args>] <project_dir>')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_pull(self):
        parser = _argument_parser(
                description='pull project updates from the git origin',
                usage='debops project pull [<args>] <project_dir>')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_push(self):
        parser = _argument_parser(
                description='push project commits to the git origin',
                usage='debops project push [<args>] <project_dir>')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_project_venv(self):
        parser = _argument_parser(
                description='manage Python virtual environment in project '
                            'directory',
                usage='''debops project venv <action> [<args>] [<project_dir>]

Actions:
    enable    activate the environment in an existing project
    install   create the environment and install requirements
    upgrade   upgrade the seed Python packages and Ansible Collections
    recreate  destroy the environment and install it from scratch
    freeze    write the installed package versions to the requirements file''')
        parser.add_argument('action', type=str,
                            choices=['enable', 'install', 'upgrade',
                                     'recreate', 'freeze'],
                            help='action to perform on the environment')
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument('--python', dest='scope', action='store_const',
                           const='python',
                           help='manage only the Python packages')
        scope.add_argument('--collections', dest='scope',
                           action='store_const', const='collections',
                           help='manage only the Ansible Collections')
        scope.add_argument('--all', dest='scope', action='store_const',
                           const='all',
                           help='manage everything (default)')
        parser.set_defaults(scope='all')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        # The 'action' and 'project_dir' positionals need to be matched
        # around the options, which parse_args() cannot do on its own
        self.args = parser.parse_intermixed_args(self.args[3:])

    def do_exec(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                usage='debops exec [<args>] [--] <[ansible_args]>',
                description='run Ansible commands directly against hosts')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to use for the "ansible" command')
        parser.add_argument('-E', '--bell', default=False,
                            help='notify the user at the end '
                                 'of Ansible execution',
                            action='store_true')
        parser.add_argument('--eval', default=False,
                            help='print the Ansible command '
                                 'generated by DebOps',
                            action='store_true')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('ansible_args', type=str, nargs=argparse.REMAINDER,
                            help='arguments for the '
                                 '"ansible" command')
        self.args = parser.parse_args(self.args[2:])

    def do_run(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                usage='debops run [<args>] [--] <[<namespace>.<collection>/]'
                      'playbook> [playbook] ... [ansible_args]',
                description='run Ansible playbook(s) against hosts',
                epilog='The Ansible options must be specified after the '
                       'playbook name(s).')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to use for the "ansible-playbook" command')
        parser.add_argument('-E', '--bell', default=False,
                            help='notify the user at the end '
                                 'of Ansible execution',
                            action='store_true')
        parser.add_argument('--eval', default=False,
                            help='print the Ansible command '
                                 'generated by DebOps',
                            action='store_true')
        parser.add_argument('--debug', default=False,
                            help='run with debugging output: -vv, '
                                 'ANSIBLE_DEBUG, the remote module '
                                 'files kept on the hosts',
                            action='store_true')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('ansible_args', type=str, nargs=argparse.REMAINDER,
                            help='arguments for the '
                                 '"ansible-playbook" command')
        self.args = parser.parse_args(self.args[2:])

    def do_check(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                usage='debops check [<args>] [--] <[<namespace>.<collection>/]'
                      'playbook> [playbook] ... [ansible_args]',
                description='run Ansible playbook(s) in check mode',
                epilog='The Ansible options must be specified after the '
                       'playbook name(s).')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to use for the "ansible-playbook" command')
        parser.add_argument('-E', '--bell', default=False,
                            help='notify the user at the end '
                                 'of Ansible execution',
                            action='store_true')
        parser.add_argument('--eval', default=False,
                            help='print the Ansible command '
                                 'generated by DebOps',
                            action='store_true')
        parser.add_argument('--debug', default=False,
                            help='run with debugging output: -vv, '
                                 'ANSIBLE_DEBUG, the remote module '
                                 'files kept on the hosts',
                            action='store_true')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('ansible_args', type=str,
                            nargs=argparse.REMAINDER,
                            help='arguments for the '
                                 '"ansible-playbook" command')
        self.args = parser.parse_args(self.args[2:])

        # Run in check mode with diff output; add the flags only if the user
        # did not already specify them, in either long or short form
        ansible_args = self.args.ansible_args
        if '--diff' not in ansible_args and '-D' not in ansible_args:
            ansible_args.append('--diff')
        if '--check' not in ansible_args and '-C' not in ansible_args:
            ansible_args.append('--check')

    def do_env(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                usage='debops env [<args>] [command_args]',
                description='run shell commands in project environment')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to use for the command')
        parser.add_argument('--scope', type=str, nargs='?',
                            choices=['full', 'local'],
                            default='local',
                            help='specify which environment '
                                 'variables to show (default: %(default)s)')
        parser.add_argument('--wait', action='store_true',
                            help='wait for the project lock instead '
                                 'of failing on contention')
        parser.add_argument('command_args', type=str, nargs=argparse.REMAINDER,
                            help='command and arguments to execute')
        self.args = parser.parse_args(self.args[2:])

    def do_config(self):
        parser = _argument_parser(
                description='manage DebOps configuration state',
                usage='''debops config <command> [<args>]

Commands:
    get     return value of a specific configuration option
    list    list configuration files parsed by DebOps''')
        parser.add_argument('command', help='config command to run')
        self._command = parser.parse_args(self.args[2:3])
        self.command = self._command.command
        if not hasattr(self, 'do_config_' + self._command.command):
            print('Error: unrecognized command:', self._command.command, file=sys.stderr)
            parser.print_help()
            sys.exit(1)
        getattr(self, 'do_config_' + self._command.command)()

    def do_mcp(self):
        parser = _argument_parser(
                description='run a Model Context Protocol server or client',
                usage='''debops mcp <command> [<args>]

Commands:
    serve    run an MCP server over stdio or a UNIX socket
    stop     stop the project's MCP daemon (SIGTERM, draining;
             --kill for a wedged one)
    repl     talk to the project's MCP daemon: an interactive
             interpreter for the MCP tools (pipes work too)
    shell    the interpreter with a status bar (the place, the
             trust state) under the input line
    console  the fullscreen cockpit: the scrollback, the status bar,
             the F2 side pane
    connect  bridge stdio to an MCP server UNIX socket
    inspect  inspect an MCP server on a UNIX socket
    configure  write the client configuration of an LLM harness
               into the project directory
    skills   list or print the DebOps skills shipped with the package
    disable  disable the MCP functionality on this host or account
             (the kill switch)''')
        parser.add_argument('command', help='MCP command to run')
        self._command = parser.parse_args(self.args[2:3])
        self.command = self._command.command
        if not hasattr(self, 'do_mcp_' + self._command.command):
            print('Error: unrecognized command:', self._command.command, file=sys.stderr)
            parser.print_help()
            sys.exit(1)
        getattr(self, 'do_mcp_' + self._command.command)()

    def do_mcp_serve(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description='run an MCP server over stdio or a UNIX socket',
                usage='debops mcp serve [<args>]')
        parser.add_argument('-V', '--view', type=str,
                            help='select the infrastructure view '
                                 'to serve')
        parser.add_argument('--docs', action='store_true',
                            help='run in the documentation mode even '
                                 'inside a project directory: only the '
                                 'bundled DebOps content and the '
                                 'collections of ANSIBLE_COLLECTIONS_PATH '
                                 'are served, read-only')
        parser.add_argument('--view-lock', action='store_true',
                            help='pin the active infrastructure view: '
                                 'the view.switch tool is refused (same '
                                 'as project.mcp.view_lock in the '
                                 'project configuration)')
        parser.add_argument('--instance', type=str, metavar='NAME',
                            help='serve the named instance from the '
                                 'instances root: NAME is '
                                 "'<project>-<view>' (split at the last "
                                 "dash) or '<project>' for the default "
                                 'view; the project directory resolves '
                                 'to <root>/<project>')
        parser.add_argument('--instance-root', type=str, metavar='PATH',
                            help='the directory which holds the named '
                                 'project instances (required with '
                                 '--instance)')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help='listen on a UNIX socket instead of '
                                 'serving on stdio; the well-known '
                                 'runtime path ($XDG_RUNTIME_DIR/debops/'
                                 'mcp/<project-key>.sock) when PATH is '
                                 'omitted')
        parser.add_argument('--socket-mode',
                            type=lambda value: int(value, 8),
                            default=0o660, metavar='MODE',
                            help='file mode of the UNIX socket, in octal '
                                 '(default: %(default)04o)')
        parser.add_argument('--detach', action='store_true',
                            help='detach the socket daemon from the '
                                 'terminal; the command returns once the '
                                 'daemon is up (or fails to start); a '
                                 'pid file rides next to a well-known '
                                 'socket')
        parser.add_argument('--idle-timeout', type=int, default=None,
                            metavar='SECONDS',
                            help='shut the daemon down after this many '
                                 'seconds without a client (0: run '
                                 'forever; default: the '
                                 'project.mcp.idle_timeout policy, or '
                                 'an hour for a client-spawned daemon)')
        parser.add_argument('--stdio', action='store_true',
                            help='serve stdio as well as the socket: '
                                 'the harness launching the server '
                                 'answers on stdio, the other clients '
                                 'use the socket')
        parser.add_argument('--max-connections', type=int, default=32,
                            metavar='COUNT',
                            help='maximum number of concurrent connections '
                                 '(default: %(default)s)')
        queue = parser.add_mutually_exclusive_group()
        queue.add_argument('--queue', dest='queue',
                           action='store_true', default=None,
                           help='enable the execution queue (the '
                                'default for a daemon started by '
                                'hand)')
        queue.add_argument('--no-queue', dest='queue',
                           action='store_false',
                           help='disable the execution queue (the '
                                'default for a spawned daemon)')
        parser.add_argument('--log', nargs='?', const='', default=None,
                            metavar='PATH',
                            help='the server audit log (startup, tool '
                                 'calls and their outcomes): appended to '
                                 'PATH, or to the syslog (the journal) '
                                 'when PATH is omitted; a harness swallows '
                                 'the server\'s stderr')
        parser.add_argument('--legacy', action='store_true',
                            help='answer the 2025-era (stateful) '
                                 'revision of the protocol instead of '
                                 '2026-07-28: the initialize handshake, '
                                 'no per-request _meta envelope, the old '
                                 'result shape. For harnesses which do '
                                 'not speak the stateless revision (Pi, '
                                 'Codex)')
        self.args = parser.parse_args(self.args[3:])

        if self.args.docs and self.args.view is not None:
            parser.error('--docs serves no project view; '
                         'do not combine it with --view')
        if self.args.docs and self.args.instance is not None:
            parser.error('--docs and --instance exclude each other')
        if self.args.docs and self.args.view_lock:
            parser.error('--docs has no views to lock')
        if self.args.detach and self.args.socket is None:
            parser.error('--detach detaches the socket daemon; '
                         'it needs --socket')
        if self.args.detach and self.args.stdio:
            parser.error('a detached daemon has no one to serve stdio '
                         'to; drop --stdio or --detach')

        if self.args.instance is not None:
            if self.args.view is not None:
                parser.error('--instance already names the view; '
                             'do not combine it with --view')
            # The global parser's default is the current directory;
            # anything else means the user passed --project-dir
            if self.args.project_dir != os.getcwd():
                parser.error('--instance and --project-dir exclude '
                             'each other')
            # No default: where the instances live is a deployment
            # decision, made by whoever provisions the daemon
            if self.args.instance_root is None:
                parser.error('--instance requires --instance-root')
            try:
                instance_project, instance_view = parse_mcp_instance(
                        self.args.instance)
            except ValueError as errmsg:
                parser.error(str(errmsg))
            self.args.project_dir = os.path.join(
                    self.args.instance_root, instance_project)
            self.args.view = instance_view

    def do_mcp_repl(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="talk to the project's MCP daemon: an "
                            'interactive interpreter for the MCP tools '
                            '(pipes work: commands on stdin, answers '
                            'on stdout)',
                usage='debops mcp repl [<args>]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='talk to a documentation-mode daemon '
                                 '(no project)')
        parser.add_argument('--legacy', action='store_true',
                            help='speak the 2025-era protocol '
                                 'revision, matching a daemon started '
                                 'with --legacy')
        parser.add_argument('-c', '--command', type=str,
                            metavar='COMMANDS',
                            help='run the commands (newline-separated) '
                                 'and exit; the ssh forced-command form '
                                 'of a login shell')
        parser.add_argument('--plain', action='store_true',
                            help='the plain reader, even when '
                                 'prompt_toolkit is available (a '
                                 'friend to slow links)')
        parser.add_argument('--confined', action='store_true',
                            help='run the client in the confined mode '
                                 '(no editor, no local shell escape), '
                                 'whatever the account configuration '
                                 'says — for testing the confined UI')
        spawn = parser.add_mutually_exclusive_group()
        spawn.add_argument('--spawn', dest='spawn',
                           action='store_true', default=None,
                           help='spawn the daemon without asking when '
                                'none is running')
        spawn.add_argument('--no-spawn', dest='spawn',
                           action='store_false',
                           help='never spawn the daemon; fail instead')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_stop(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="stop the project's MCP daemon: a "
                            'SIGTERM, which drains the in-flight '
                            'work first; the pid comes from the '
                            "socket's sidecar and is correlated "
                            'against /proc before any signal',
                usage='debops mcp stop [<args>]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='stop the documentation-mode daemon '
                                 '(no project)')
        parser.add_argument('--kill', action='store_true',
                            help='SIGKILL instead — for a wedged '
                                 'daemon; its stale files are cleaned '
                                 'up by the next one')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_shell(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="talk to the project's MCP daemon: the "
                            'interpreter with a status bar (the place, '
                            'the trust state) under the input line; a '
                            'pipe stays the plain interpreter',
                usage='debops mcp shell [<args>]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='talk to a documentation-mode daemon '
                                 '(no project)')
        parser.add_argument('--legacy', action='store_true',
                            help='speak the 2025-era protocol '
                                 'revision, matching a daemon started '
                                 'with --legacy')
        parser.add_argument('-c', '--command', type=str,
                            metavar='COMMANDS',
                            help='run the commands (newline-separated) '
                                 'and exit; the ssh forced-command form '
                                 'of a login shell')
        parser.add_argument('--plain', action='store_true',
                            help='the plain reader, even when '
                                 'prompt_toolkit is available (a '
                                 'friend to slow links)')
        parser.add_argument('--confined', action='store_true',
                            help='run the client in the confined mode '
                                 '(no editor, no local shell escape), '
                                 'whatever the account configuration '
                                 'says — for testing the confined UI')
        spawn = parser.add_mutually_exclusive_group()
        spawn.add_argument('--spawn', dest='spawn',
                           action='store_true', default=None,
                           help='spawn the daemon without asking when '
                                'none is running')
        spawn.add_argument('--no-spawn', dest='spawn',
                           action='store_false',
                           help='never spawn the daemon; fail instead')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_console(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="talk to the project's MCP daemon: the "
                            'fullscreen cockpit (the scrollback, the '
                            'status bar, the F2 side pane); a pipe '
                            'stays the plain interpreter',
                usage='debops mcp console [<args>]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='talk to a documentation-mode daemon '
                                 '(no project)')
        parser.add_argument('--legacy', action='store_true',
                            help='speak the 2025-era protocol '
                                 'revision, matching a daemon started '
                                 'with --legacy')
        parser.add_argument('-c', '--command', type=str,
                            metavar='COMMANDS',
                            help='run the commands (newline-separated) '
                                 'and exit; the ssh forced-command form '
                                 'of a login shell')
        parser.add_argument('--plain', action='store_true',
                            help='the plain reader, even when '
                                 'prompt_toolkit is available (a '
                                 'friend to slow links)')
        parser.add_argument('--confined', action='store_true',
                            help='run the client in the confined mode '
                                 '(no editor, no local shell escape), '
                                 'whatever the account configuration '
                                 'says — for testing the confined UI')
        spawn = parser.add_mutually_exclusive_group()
        spawn.add_argument('--spawn', dest='spawn',
                           action='store_true', default=None,
                           help='spawn the daemon without asking when '
                                'none is running')
        spawn.add_argument('--no-spawn', dest='spawn',
                           action='store_false',
                           help='never spawn the daemon; fail instead')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_connect(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="bridge stdio to the project's MCP daemon "
                            '(or to a server on a given socket)',
                usage='debops mcp connect [<args>] [<key=value>...]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='bridge to a documentation-mode '
                                 'daemon (no project)')
        action = parser.add_mutually_exclusive_group()
        action.add_argument('--ping', dest='ping', action='store_true',
                            help='probe the server with a server/discover '
                                 'request and print the result')
        action.add_argument('-c', '--call', dest='call', type=str,
                            metavar='TOOL',
                            help='call a tool on the server and print '
                                 'the result; its arguments follow as '
                                 'key=value pairs')
        parser.add_argument('arguments', type=str, nargs='*',
                            metavar='key=value',
                            help='arguments for --call')
        parser.add_argument('-t', '--timeout', dest='timeout', type=float,
                            metavar='SECONDS',
                            help='how long to wait for the server '
                                 '(default: 5 s for --ping; no limit '
                                 'for --call, Ctrl-C cancels the '
                                 'request on the server)')
        parser.add_argument('--raw', dest='raw', action='store_true',
                            help='print the server response as received '
                                 '(only with --ping or --call)')
        spawn = parser.add_mutually_exclusive_group()
        spawn.add_argument('--spawn', dest='spawn',
                           action='store_true', default=None,
                           help='spawn the daemon without asking when '
                                'none is running')
        spawn.add_argument('--no-spawn', dest='spawn',
                           action='store_false',
                           help='never spawn the daemon; fail instead')
        # The key=value pairs follow optionals, which plain parse_args()
        # cannot interleave with the socket positional
        self.args = parser.parse_intermixed_args(self.args[3:])

    def do_mcp_inspect(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description="inspect the project's MCP daemon (or "
                            'the one on a given socket)',
                usage='debops mcp inspect [<args>]')
        parser.add_argument('--socket', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="the daemon's socket; the well-known "
                                 'runtime path when PATH is omitted')
        parser.add_argument('--docs', action='store_true',
                            help='inspect a documentation-mode daemon '
                                 '(no project)')
        parser.add_argument('--legacy', action='store_true',
                            help='speak the 2025-era protocol '
                                 'revision, matching a daemon started '
                                 'with --legacy')
        parser.add_argument('--discover', dest='discover',
                            action='store_true',
                            help='show the server identity, capabilities '
                                 'and instructions (the default)')
        parser.add_argument('--tools', dest='tools', action='store_true',
                            help='list the tools the server exposes')
        parser.add_argument('--tool', dest='tool', type=str,
                            metavar='NAME',
                            help='describe one tool in detail')
        parser.add_argument('--resources', dest='resources',
                            action='store_true',
                            help='list the resources the server exposes')
        parser.add_argument('--prompts', dest='prompts',
                            action='store_true',
                            help='list the prompts the server exposes')
        parser.add_argument('--raw', dest='raw', action='store_true',
                            help='print the server responses as received')
        spawn = parser.add_mutually_exclusive_group()
        spawn.add_argument('--spawn', dest='spawn',
                           action='store_true', default=None,
                           help='spawn the daemon without asking when '
                                'none is running')
        spawn.add_argument('--no-spawn', dest='spawn',
                           action='store_false',
                           help='never spawn the daemon; fail instead')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_configure(self):
        from .mcp.harnesses import HARNESSES
        parser = _argument_parser(
                parents=[self.global_parser],
                description='write the client configuration of an LLM '
                            'harness into the project directory',
                usage='debops mcp configure [<args>]',
                epilog='Default files: ' + '; '.join(
                        '{}: {}'.format(name,
                                        harness.default_filename
                                        or '(print only)')
                        for name, harness in sorted(HARNESSES.items())))
        parser.add_argument('-H', '--harness', metavar='HARNESS',
                            choices=sorted(HARNESSES),
                            help='the harness to configure: '
                                 + ', '.join(sorted(HARNESSES)))
        parser.add_argument('-f', '--filename', type=str, metavar='PATH',
                            help='write to PATH instead of the harness '
                                 'default (relative to the project '
                                 'directory, or absolute)')
        parser.add_argument('--user', action='store_true',
                            help='write the account-level configuration '
                                 'of the harness (no project needed); '
                                 'the spawned server serves a project '
                                 'when launched inside one and the '
                                 'documentation mode otherwise')
        parser.add_argument('--agents', action='store_true',
                            help='write the MCP guidance section into '
                                 'the project AGENTS.md (honored by '
                                 'OpenCode, Codex and Claude Code)')
        parser.add_argument('--name', type=str, metavar='NAME',
                            help="the entry name (default: 'debops'; "
                                 "letters, digits, '-' and '_')")
        parser.add_argument('--connect', type=str, metavar='PATH',
                            nargs='?', const='',
                            help="bridge the harness to the daemon's "
                                 'UNIX socket instead of launching '
                                 'the server — PATH, or a portable '
                                 'bridge to the well-known socket of '
                                 'the project the harness runs in '
                                 'when bare (the form to commit to '
                                 'git); requires --name (never the '
                                 'default entry)')
        parser.add_argument('--docs', action='store_true',
                            help='the spawned server always runs in '
                                 'the documentation mode, even inside '
                                 'a project (read-only; no project '
                                 'directory is served); the harness '
                                 'file needs no project directory '
                                 'either')
        parser.add_argument('--with-socket', dest='with_socket',
                            action='store_true',
                            help='the launched server also answers the '
                                 'well-known socket, so a `debops mcp '
                                 'repl` attaches while the harness '
                                 'session lives (never with --connect)')
        parser.add_argument('--disconnect', type=str, metavar='NAME',
                            help='remove the named bridge entry')
        parser.add_argument('--remove', action='store_true',
                            help='remove all of our entries (with '
                                 '--name, only that one)')
        parser.add_argument('--log', nargs='?', const='', default=None,
                            metavar='PATH',
                            help='launch the server with the audit log '
                                 '(to PATH, or the syslog when bare)')
        parser.add_argument('--print', dest='print_only',
                            action='store_true',
                            help='print the configuration instead of '
                                 'writing it')
        self.args = parser.parse_args(self.args[3:])
        if self.args.harness is None and not self.args.agents:
            parser.error('nothing to do: name a harness with -H, '
                         'or pass --agents (or both)')
        if self.args.agents and self.args.user:
            parser.error('--agents writes the project AGENTS.md; '
                         'there is no account-level convention')
        managing = (self.args.connect is not None
                    or self.args.disconnect is not None
                    or self.args.remove)
        if self.args.connect is not None:
            if self.args.name is None:
                parser.error('a bridge entry needs its own --name')
            if self.args.name == 'debops':
                parser.error("a bridge entry needs its own --name: "
                             "'debops' is the launcher (a management "
                             'project holds the bridges; the default '
                             'entry keeps the documentation mode '
                             'outside projects)')
            if self.args.docs:
                parser.error("--docs enforces the documentation mode "
                             "of the spawned server; a --connect "
                             "bridge takes the daemon's mode")
            if self.args.with_socket:
                parser.error('--with-socket adds the socket to a '
                             'launched entry; a --connect bridge '
                             'launches nothing')
        if self.args.disconnect is not None and (
                self.args.connect is not None or self.args.remove):
            parser.error('--disconnect does not combine with '
                         '--connect or --remove')
        if self.args.remove and self.args.connect is not None:
            parser.error('--remove does not combine with --connect')
        if managing and self.args.print_only:
            parser.error('--print writes nothing; it does not combine '
                         'with --disconnect or --remove')

    def do_mcp_skills(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description='list or print the DebOps skills shipped '
                            'with the package',
                usage='debops mcp skills [<args>] [<skill>]')
        parser.add_argument('skill', type=str, nargs='?',
                            help='print this skill instead of listing')
        parser.add_argument('--filter', type=str, metavar='PATTERN',
                            help='list only skills matching PATTERN: '
                                 'a plain word matches anywhere in the '
                                 'skill path, glob characters make it '
                                 'an fnmatch ("roles/*", "apt*")')
        self.args = parser.parse_args(self.args[3:])

    def do_mcp_disable(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                description='disable the DebOps MCP functionality on this '
                            'host or account (the kill switch)',
                usage='debops mcp disable [<args>]')
        self.args = parser.parse_args(self.args[3:])

    def do_config_list(self):
        parser = _argument_parser(
                usage='debops config list [<args>] <project_dir>',
                description='list configuration files parsed by DebOps')
        parser.add_argument('-v', '--verbose', action="count",
                            help='increase output verbosity '
                                 '(e.g., -vv is more than -v)')
        parser.add_argument('project_dir', type=str, nargs='?',
                            default=os.getcwd(),
                            help='path to the project directory')
        self.args = parser.parse_args(self.args[3:])

    def do_config_get(self):
        parser = _argument_parser(
                parents=[self.global_parser],
                usage='debops config get [<args>] [--] <key>',
                description='return value of specific DebOps option')
        parser.add_argument('--format', type=str, nargs='?',
                            choices=['json', 'toml', 'unix', 'yaml'],
                            default='unix',
                            help='output format (default: %(default)s)')
        parser.add_argument('-k', '--keys', default=False,
                            help='list configuration keys '
                                 'at the specified level',
                            action='store_true')
        parser.add_argument('key', type=str,
                            nargs=argparse.REMAINDER,
                            help='name of the '
                                 'configuration option')
        self.args = parser.parse_args(self.args[3:])
