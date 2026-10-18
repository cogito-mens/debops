# Copyright (C) 2020-2023 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2020-2023 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

from .exceptions import NoDefaultViewException, InventorySpecError
from .config import Configuration
from .subcommands import Subcommands
from .projectdir import ProjectDir
from .projectvenv import ProjectVenv
from .ansiblerunner import AnsibleRunner
from .ansibleplaybookrunner import AnsiblePlaybookRunner
from .envrunner import EnvRunner
from .mcp.server import McpServer
from .mcp.policy import McpPolicy, KillSwitch
from .mcp.skills import SkillLibrary
from .mcp.tools import register_project_tools
from .mcp import client as mcp_client
from .mcp.client import UnixClient
from .mcp.transport import StdioTransport, UnixSocketTransport
from .mcp.transport import bridge_stdio_to_unix, ping_unix
from xdg.BaseDirectory import xdg_config_home
import json
import logging
import getpass
import sys
import os
import textwrap
import time

# The kill switch file written by 'debops mcp disable'
KILLSWITCH_FILENAME = 'mcp-killswitch.toml'
KILLSWITCH_CONTENT = '''\
# Written by 'debops mcp disable'. Remove this file to re-enable the
# MCP functionality.
[mcp]
enabled = false
'''

logger = logging.getLogger(__name__)


class Interpreter(object):

    def __init__(self, args=None):
        self.args = args
        self.config = Configuration()
        self.parsed_args = Subcommands(self.args)
        logger.debug('Command line interpreter initialized')
        logger.debug('Control user=' + getpass.getuser())

        if self.parsed_args.section == 'project':
            if self.parsed_args.command == 'init':
                self.do_project_init(self.parsed_args.args)
            elif self.parsed_args.command == 'refresh':
                self.do_project_refresh(self.parsed_args.args)
            elif self.parsed_args.command == 'lock':
                self.do_project_lock(self.parsed_args.args)
            elif self.parsed_args.command == 'unlock':
                self.do_project_unlock(self.parsed_args.args)
            elif self.parsed_args.command == 'mkview':
                self.do_project_mkview(self.parsed_args.args)
            elif self.parsed_args.command == 'commit':
                self.do_project_commit(self.parsed_args.args)
            elif self.parsed_args.command == 'pull':
                self.do_project_pull(self.parsed_args.args)
            elif self.parsed_args.command == 'push':
                self.do_project_push(self.parsed_args.args)
            elif self.parsed_args.command == 'venv':
                self.do_project_venv(self.parsed_args.args)

        elif self.parsed_args.section == 'exec':
            self.do_exec(self.parsed_args.args)

        elif self.parsed_args.section in ['run', 'check']:
            self.do_run(self.parsed_args.args)

        elif self.parsed_args.section == 'env':
            self.do_env(self.parsed_args.args)

        elif self.parsed_args.section == 'config':
            if self.parsed_args.command == 'list':
                self.do_config_list(self.parsed_args.args)
            elif self.parsed_args.command == 'get':
                self.do_config_get(self.parsed_args.args)

        elif self.parsed_args.section == 'mcp':
            if self.parsed_args.command == 'serve':
                self.do_mcp_serve(self.parsed_args.args)
            elif self.parsed_args.command == 'connect':
                self.do_mcp_connect(self.parsed_args.args)
            elif self.parsed_args.command == 'inspect':
                self.do_mcp_inspect(self.parsed_args.args)
            elif self.parsed_args.command == 'skills':
                self.do_mcp_skills(self.parsed_args.args)
            elif self.parsed_args.command == 'disable':
                self.do_mcp_disable(self.parsed_args.args)

    def do_project_init(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 create=True, **vars(args))
            project.create()
            if args.venv and args.requirements:
                venv = ProjectVenv(project, **vars(args))
                venv.install()
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, InventorySpecError, FileNotFoundError,
                ChildProcessError, ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_refresh(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 **vars(args))
            project.refresh()
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, InventorySpecError,
                ChildProcessError, ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_lock(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
            try:
                project.lock()
            except (NoDefaultViewException) as errmsg:
                print('Error:', errmsg)
                sys.exit(1)
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, ChildProcessError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_unlock(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
            try:
                project.unlock()
            except (NoDefaultViewException) as errmsg:
                print('Error:', errmsg)
                sys.exit(1)
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, ChildProcessError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_mkview(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 **vars(args))
            project.mkview(view=args.new_view)
        except (IsADirectoryError, NotADirectoryError,
                InventorySpecError, ChildProcessError,
                ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_commit(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config)
            project.commit(interactive=True)
        except (IsADirectoryError, NotADirectoryError,
                PermissionError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_project_pull(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config)
            report = project.pull()
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        if not report['changed']:
            print('Already up to date.')
            return
        print('Updated {} → {}'.format(report['before'][:12],
                                       report['after'][:12]))
        print('Files changed: {}'.format(report['files_changed']))
        for subject in report['commits']:
            print('- {}'.format(subject))

    def do_project_push(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config)
            report = project.push()
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        if not report['pushed']:
            print('Nothing to push, the project is up to date with '
                  'the origin.')
            return
        print('Pushed {} commit(s):'.format(len(report['commits'])))
        for subject in report['commits']:
            print('- {}'.format(subject))

    def do_project_venv(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 **vars(args))
            if args.action == 'enable':
                project.enable_venv(scope=args.scope)
                return
            venv = ProjectVenv(project, **vars(args))
            if args.action == 'install':
                venv.install(scope=args.scope)
            elif args.action == 'upgrade':
                venv.upgrade(scope=args.scope)
            elif args.action == 'recreate':
                venv.recreate(scope=args.scope)
            elif args.action == 'freeze':
                venv.freeze(scope=args.scope)
        except (IsADirectoryError, NotADirectoryError,
                PermissionError, FileNotFoundError,
                ChildProcessError, ValueError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

    def do_exec(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
        except (IsADirectoryError, NotADirectoryError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        try:
            runner = AnsibleRunner(project, **vars(args))
        except (NoDefaultViewException, ValueError,
                FileNotFoundError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        if args.eval:
            runner.eval()
            sys.exit(0)
        else:
            sys.exit(runner.execute())

    def do_run(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
        except (IsADirectoryError, NotADirectoryError, ChildProcessError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        try:
            runner = AnsiblePlaybookRunner(project, **vars(args))
        except (NoDefaultViewException, ValueError,
                FileNotFoundError, ChildProcessError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        if args.eval:
            runner.eval()
            sys.exit(0)
        else:
            try:
                sys.exit(runner.execute())
            except ChildProcessError as errmsg:
                print('Error:', errmsg)
                sys.exit(1)

    def do_env(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
        except (IsADirectoryError, NotADirectoryError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        try:
            runner = EnvRunner(project, **vars(args))
        except (NoDefaultViewException, ValueError,
                FileNotFoundError) as errmsg:
            print('Error:', errmsg)
            sys.exit(1)

        if args.command_args:
            sys.exit(runner.execute())
        else:
            sys.exit(runner.show_env(scope=args.scope))

    def _mcp_enabled(self):
        """Check the MCP kill switch against the ambient configuration.

        The project configuration is not loaded yet at this point, which
        is what keeps a project from re-enabling a disabled server.
        """
        return bool(self.config.raw.get('mcp', {}).get('enabled', True))

    def _mcp_refuse_if_disabled(self):
        if not self._mcp_enabled():
            print('Error: the DebOps MCP functionality is disabled '
                  '(mcp.enabled is false)', file=sys.stderr)
            sys.exit(1)

    def do_mcp_serve(self, args):
        self._mcp_refuse_if_disabled()
        # The server is confined to a project directory; refuse to start
        # outside of one
        try:
            project = ProjectDir(path=args.project_dir, config=self.config,
                                 view=args.view)
        except (IsADirectoryError, NotADirectoryError) as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)

        logger.notice('Serving project \'%s\' at %s, view \'%s\'',
                      project.name, project.path, project.view)
        killswitch = KillSwitch()
        policy = McpPolicy(project)
        server = McpServer(project=project, policy=policy,
                           skills=SkillLibrary(), killswitch=killswitch)
        register_project_tools(server,
                               enabled_check=killswitch.is_enabled)
        try:
            if args.socket:
                transport = UnixSocketTransport(
                        args.socket, mode=args.mode,
                        max_connections=args.max_connections)
            else:
                transport = StdioTransport()
            transport.serve(server)
        except (ValueError, FileExistsError, PermissionError,
                OSError) as errmsg:
            # Errors go to stderr: in stdio mode stdout is reserved for
            # protocol messages
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            # 128 + SIGINT; the transport cleans up after itself
            sys.exit(130)

    def do_mcp_connect(self, args):
        self._mcp_refuse_if_disabled()
        if args.raw and not args.ping:
            print('Error: --raw only makes sense with --ping',
                  file=sys.stderr)
            sys.exit(2)
        if not args.ping:
            try:
                bridge_stdio_to_unix(args.socket)
            except (ConnectionError, OSError) as errmsg:
                print('Error:', errmsg, file=sys.stderr)
                sys.exit(1)
            except KeyboardInterrupt:
                sys.exit(130)
            return

        started = time.monotonic()
        try:
            raw = ping_unix(args.socket)
        except ConnectionError as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        except EOFError:
            print('Error: the server closed the connection without '
                  'a response (the kill switch may be active)',
                  file=sys.stderr)
            sys.exit(1)
        except TimeoutError as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        rtt = time.monotonic() - started

        if args.raw:
            sys.stdout.write(raw.decode('utf-8') + '\n')
            return

        try:
            response = json.loads(raw)
        except ValueError:
            print('Error: the server response is not JSON: {}'.format(
                    raw[:200]), file=sys.stderr)
            sys.exit(1)
        if 'error' in response:
            print('Error: the server returned an error: {}'.format(
                    response['error'].get('message')), file=sys.stderr)
            sys.exit(1)
        result = response.get('result', {})
        meta = result.get('_meta', {})
        server_info = meta.get('io.modelcontextprotocol/serverInfo', {})
        capabilities = ', '.join(sorted(result.get('capabilities', {})))
        versions = result.get('supportedVersions', [])
        print('debops mcp server at {}:'.format(args.socket))
        print('    name: {}'.format(server_info.get('name', '?')))
        if meta.get('org.debops/project') is not None:
            print('    project: {}'.format(meta['org.debops/project']))
        if meta.get('org.debops/view') is not None:
            print('    view: {}'.format(meta['org.debops/view']))
        print('    version: {}'.format(server_info.get('version', '?')))
        print('    protocol: {}'.format(', '.join(versions) or '?'))
        print('    capabilities: {}'.format(capabilities or '(none)'))
        print('    round-trip: {:.0f} ms'.format(rtt * 1000))

    def do_mcp_inspect(self, args):
        self._mcp_refuse_if_disabled()
        # With no selection, show the discovery summary
        nothing = not any((args.discover, args.tools, args.tool,
                           args.resources, args.prompts))
        steps = []
        if args.discover or nothing:
            steps.append('discover')
        if args.tools:
            steps.append('tools')
        if args.tool:
            steps.append('tool')
        if args.prompts:
            steps.append('prompts')
        if args.resources:
            steps.append('resources')

        client = UnixClient(args.socket)
        try:
            for step in steps:
                self._mcp_inspect_step(client, step, args)
        except ConnectionError as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        except EOFError:
            print('Error: the server closed the connection without '
                  'a response (the kill switch may be active)',
                  file=sys.stderr)
            sys.exit(1)
        except TimeoutError as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        finally:
            client.close()

    def _mcp_inspect_step(self, client, step, args):
        """Run one inspection step and print its result."""
        request = {'discover': ('server/discover', None),
                   'tools': ('tools/list', None),
                   'tool': ('tools/list', None),
                   'prompts': ('prompts/list', None),
                   'resources': ('resources/list', None)}[step]
        response = client.request(*request)
        if 'error' in response:
            error = response['error']
            # A server announces missing primitives with Method not
            # found; that is an answer, not a failure
            if (error.get('code') == -32601
                    and step in ('prompts', 'resources')):
                print('{}: the server does not support them'
                      .format(step))
                return
            print('Error: the server returned an error: {}'.format(
                    error.get('message')), file=sys.stderr)
            sys.exit(1)
        if args.raw:
            print(json.dumps(response))
            return
        result = response.get('result', {})
        if step == 'discover':
            print('\n'.join(mcp_client.format_discover(result)))
        elif step == 'tools':
            print('\n'.join(mcp_client.format_tools(
                    result.get('tools', []))))
        elif step == 'tool':
            tools = {tool['name']: tool
                     for tool in result.get('tools', [])}
            if args.tool not in tools:
                print('Error: no such tool: {}'.format(args.tool),
                      file=sys.stderr)
                sys.exit(1)
            print('\n'.join(mcp_client.format_tool(tools[args.tool])))
        elif step == 'prompts':
            prompts = result.get('prompts', [])
            print('prompts ({}):'.format(len(prompts)))
            for prompt in prompts:
                print('    {}  {}'.format(
                        prompt['name'], prompt.get('description', '')))
        elif step == 'resources':
            print('\n'.join(mcp_client.format_resources(
                    result.get('resources', []))))

    def do_mcp_skills(self, args):
        self._mcp_refuse_if_disabled()
        # Skills are shipped with the package and do not depend on
        # a project directory
        library = SkillLibrary()
        if args.skill:
            try:
                content = library.read('skill://' + args.skill)
            except ValueError as errmsg:
                print('Error:', errmsg, file=sys.stderr)
                sys.exit(1)
            if content is None:
                print('Error: skill not found:', args.skill,
                      file=sys.stderr)
                sys.exit(1)
            print(content, end='')
        else:
            first = True
            for skill in library.list():
                if not first:
                    print()
                first = False
                print(skill['name'])
                print(textwrap.fill(skill['description'], width=79,
                                    initial_indent='    ',
                                    subsequent_indent='    '))

    def _killswitch_writable(self, path):
        """Check if a configuration directory is writable (or can be
        created)."""
        if os.path.isdir(path):
            return os.access(path, os.W_OK)
        parent = os.path.dirname(path)
        return os.path.isdir(parent) and os.access(parent, os.W_OK)

    def do_mcp_disable(self, args):
        # Deliberately not gated by the kill switch itself: disabling
        # must always be possible, and it is idempotent
        etc_conf = os.path.join('/etc', 'debops', 'conf.d')
        user_conf = os.path.join(xdg_config_home, 'debops', 'conf.d')
        if self._killswitch_writable(etc_conf):
            target_dir, level = etc_conf, 'system'
        else:
            target_dir, level = user_conf, 'user'
        target = os.path.join(target_dir, KILLSWITCH_FILENAME)

        if not self._mcp_enabled():
            if os.path.isfile(target):
                print('The DebOps MCP functionality is already disabled '
                      '(via {})'.format(target))
            else:
                print('The DebOps MCP functionality is already disabled '
                      'by the configuration (see debops config list)')
            return

        try:
            os.makedirs(target_dir, exist_ok=True)
            with open(target, 'w') as fp:
                fp.write(KILLSWITCH_CONTENT)
        except OSError as errmsg:
            print('Error: cannot write the kill switch:', errmsg,
                  file=sys.stderr)
            sys.exit(1)

        print('The DebOps MCP functionality is now disabled '
              '({} level: {})'.format(level, target))
        print('Running servers refuse new connections immediately and '
              'terminate in-flight executions within a second.')
        print('To re-enable it, remove the file.')

        # A later configuration layer (the user level is read after
        # /etc) could still override the kill switch
        if self._mcp_enabled_after_write():
            print('Warning: a later configuration layer still enables '
                  'the MCP functionality; see debops config list',
                  file=sys.stderr)

    def _mcp_enabled_after_write(self):
        return bool(Configuration().raw.get('mcp', {})
                    .get('enabled', True))

    def do_config_list(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config)
        except (IsADirectoryError, NotADirectoryError) as errmsg:
            # This is not a project directory, so no project-dependent
            # configuration is included
            pass

        self.config.config_list()

    def do_config_get(self, args):
        try:
            project = ProjectDir(path=args.project_dir, config=self.config)
        except (IsADirectoryError, NotADirectoryError) as errmsg:
            # This is not a project directory, so no project-dependent
            # configuration is included
            pass

        if args.key:
            for option_name in args.key:
                self.config.config_get(option_name, format=args.format,
                                       keys=args.keys)
        else:
            self.config.config_get('.', format=args.format, keys=args.keys)
