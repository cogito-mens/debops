# Copyright (C) 2020-2023 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2020-2023 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

from .exceptions import NoDefaultViewException, InventorySpecError
from .config import Configuration
from .constants import DEBOPS_PACKAGE_DATA
from .subcommands import Subcommands
from .projectdir import ProjectDir
from .projectvenv import ProjectVenv
from .ansiblerunner import AnsibleRunner
from .ansibleplaybookrunner import AnsiblePlaybookRunner
from .envrunner import EnvRunner
from .mcp.server import McpServer
from .mcp.policy import McpPolicy, KillSwitch
from .mcp.prompts import PromptLibrary
from .mcp.skills import SkillLibrary, match_skill
from .mcp.tools import (register_documentation_tools,
                        register_project_tools)
from .mcp.tools.collections import documentation_collection_roots
from .mcp.tools.common import account_ansible_binary
from .mcp.tools.doc import account_ansible_doc
from .mcp.tools.roles import COLLECTION_ROLES, project_role_paths
from .mcp.tools.playbooks import COLLECTION_PLAYBOOKS
from .mcp.role_skills import RoleSkillSource
from .mcp.playbook_skills import PlaybookSkillSource
from .mcp import client as mcp_client
from .mcp.client import UnixClient
from .mcp.transport import StdioTransport, UnixSocketTransport
from .mcp.transport import bridge_stdio_to_unix, ping_unix
from .mcp.harnesses import (configure_agents_md, configure_harness,
                            disconnect_harness, fragment_for_display,
                            remove_harness)
from .hooks import run_hooks
from .utils import set_process_name
from xdg.BaseDirectory import xdg_config_home
import contextlib
import json
import logging
from logging.handlers import SysLogHandler
import platform
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


@contextlib.contextmanager
def _mcp_client_errors():
    """Turn MCP client connection failures into clean CLI errors."""
    try:
        yield
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
            elif self.parsed_args.command == 'configure':
                self.do_mcp_configure(self.parsed_args.args)
            elif self.parsed_args.command == 'skills':
                self.do_mcp_skills(self.parsed_args.args)
            elif self.parsed_args.command == 'disable':
                self.do_mcp_disable(self.parsed_args.args)
            else:
                # The parser accepted a command this dispatch does not
                # know; the two lists drifted
                print('Error: unimplemented mcp command:',
                      self.parsed_args.command, file=sys.stderr)
                sys.exit(1)

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

        # The server is confined to a project directory; outside of
        # one — or with --docs — it runs in the documentation mode
        docs_mode = args.docs
        project = None
        if not docs_mode:
            # ProjectDir chdirs into the directory it probes, even when
            # it then fails; capture the invocation directory first
            invoked_in = os.getcwd()
            try:
                project = ProjectDir(path=args.project_dir,
                                     config=self.config, view=args.view)
            except (IsADirectoryError, NotADirectoryError) as errmsg:
                # An explicitly named directory must be a project; the
                # ambient fallback is the documentation mode
                if os.path.abspath(args.project_dir) != invoked_in:
                    print('Error:', errmsg, file=sys.stderr)
                    sys.exit(1)
                docs_mode = True

        set_process_name('debops-mcp')
        if args.log is not None:
            # A harness swallows the server's stderr; the audit channel
            # goes to a file of its own, or to the syslog (the journal
            # under systemd). The logger itself must emit at NOTICE for
            # either to see the audit lines
            debops_log = logging.getLogger('debops')
            if args.log:
                try:
                    log_handler = logging.FileHandler(os.path.abspath(
                            os.path.expanduser(args.log)))
                except OSError as errmsg:
                    print('Error: cannot open the log file: {}'
                          .format(errmsg), file=sys.stderr)
                    sys.exit(1)
                log_handler.setFormatter(logging.Formatter(
                        '%(asctime)s %(levelname)s %(message)s'))
                debops_log.addHandler(log_handler)
            else:
                log_handler = next(
                        (handler for handler in debops_log.handlers
                         if isinstance(handler, SysLogHandler)), None)
                if log_handler is None:
                    print('Error: no syslog handler is configured',
                          file=sys.stderr)
                    sys.exit(1)
            log_handler.setLevel(logging.NOTICE)
            debops_log.setLevel(min(
                    debops_log.getEffectiveLevel() or logging.NOTICE,
                    logging.NOTICE))
        logger.notice('Interpreter: %s (%s %s)', sys.executable,
                      platform.python_implementation(),
                      platform.python_version())
        killswitch = KillSwitch()
        on_ready = None
        on_stop = None
        started = False

        if docs_mode:
            policy = McpPolicy.for_documentation()
            role_skills = RoleSkillSource([COLLECTION_ROLES])
            playbook_skills = PlaybookSkillSource(
                    COLLECTION_PLAYBOOKS, role_skills, execution=False)
            role_skills.playbooks = playbook_skills
            server = McpServer(
                    project=None, policy=policy,
                    skills=SkillLibrary(
                            sources=[role_skills, playbook_skills]),
                    prompts=PromptLibrary(
                            package_path=os.path.join(
                                    DEBOPS_PACKAGE_DATA, 'prompts')),
                    killswitch=killswitch,
                    transport='unix' if args.socket else 'stdio',
                    legacy=args.legacy)
            register_documentation_tools(server)
            logger.notice('Serving DebOps documentation (no project '
                          'directory, pid %s)', os.getpid())
            # One roots rule for every documentation-mode consumer —
            # the collection index, the ansible.doc tool, the role
            # and playbook resolvers — so the tools agree by
            # construction
            defined = 'ANSIBLE_COLLECTIONS_PATH' in os.environ
            roots = documentation_collection_roots()
            origin = ('from the environment' if defined else
                      'the account defaults and the bundled '
                      'collection')
            logger.notice('Collections: %s (%s)',
                          os.pathsep.join(roots) or '(none)', origin)
            for root in roots:
                if not os.path.isdir(root):
                    # An operator-chosen root which is missing is a
                    # configuration error; a missing account default
                    # is an account which has nothing there
                    (logger.warning if defined else logger.debug)(
                            'Collections root not found: %s', root)
            if account_ansible_binary('ansible-galaxy') is None:
                logger.warning('ansible-galaxy was not found; the '
                               'collection tools will not answer')
            # The ansible.doc tool resolves the account's own binary;
            # the resolution is in the log for debugging
            doc_binary = account_ansible_doc()
            if doc_binary is None:
                logger.warning('ansible-doc was not found; the '
                               'ansible.doc tool will not answer')
            else:
                logger.notice('ansible-doc: %s', doc_binary)
        else:
            logger.notice('Serving project \'%s\' at %s, view \'%s\' '
                          '(pid %s)', project.name, project.path,
                          project.view, os.getpid())
            policy = McpPolicy(project)
            # The command line lock layers over the configuration
            policy.view_lock = policy.view_lock or args.view_lock
            if args.view_lock and policy.view_root is None:
                print('Error: --view-lock with no active view leaves '
                      'the server without its view tools permanently',
                      file=sys.stderr)
                sys.exit(2)
            if policy.view_root is None:
                logger.notice('No active infrastructure view (the '
                              'default view is unset or unknown); the '
                              'view.list, view.switch and view.create '
                              'tools remain available')
            project_content = None
            prompts = None
            if policy.project_content:
                project_content = os.path.join(project.path, '.debops', 'mcp')
                prompts = PromptLibrary(
                        os.path.join(project_content, 'prompts'),
                        package_path=os.path.join(DEBOPS_PACKAGE_DATA,
                                                  'prompts'))
            # The same roles the tools resolve, so the briefs and the
            # tools agree; the bundled collection follows the policy
            roles_paths = project_role_paths(project)
            if policy.bundled_content:
                roles_paths.append(COLLECTION_ROLES)
            role_skills = RoleSkillSource(roles_paths)
            playbook_skills = PlaybookSkillSource(
                    COLLECTION_PLAYBOOKS if policy.bundled_content
                    else None, role_skills, policy=policy)
            role_skills.playbooks = playbook_skills
            server = McpServer(project=project, policy=policy,
                               skills=SkillLibrary(
                                   project_path=(os.path.join(project_content,
                                                              'skills')
                                                 if project_content else None),
                                   sources=[role_skills,
                                            playbook_skills]),
                               prompts=prompts, killswitch=killswitch,
                               transport='unix' if args.socket
                               else 'stdio',
                               legacy=args.legacy)
            register_project_tools(server,
                                   enabled_check=killswitch.is_enabled)

            def _rebuild(view=None):
                """Re-read the project configuration and rebuild the policy
                and the tool surface (a pull may have changed them, or a
                view switch is landing)."""
                fresh = ProjectDir(path=project.path,
                                   config=Configuration(),
                                   view=view or server.project.view)
                fresh_policy = McpPolicy(fresh)
                # The command line lock re-applies over the reloaded
                # configuration
                fresh_policy.view_lock = (fresh_policy.view_lock
                                          or args.view_lock)
                # Registration reads the project and the policy from the
                # server; the registry is built aside and swapped in
                # whole, so that a concurrent tools/list never sees a
                # partial table
                server.project = fresh
                server.policy = fresh_policy
                registry = {}
                register_project_tools(
                        server, enabled_check=killswitch.is_enabled,
                        registry=registry)
                server.tools = registry

            server._rebuild = _rebuild

            # The lifecycle hooks: mcp-pre-start may abort the startup; the
            # others only ever log a failure, so a hook cannot stall a
            # running server or its shutdown
            hook_env = {'DEBOPS_MCP_TRANSPORT':
                        'unix' if args.socket else 'stdio'}
            if args.socket:
                hook_env['DEBOPS_MCP_SOCKET'] = os.path.abspath(
                        os.path.expanduser(args.socket))
            if not run_hooks(project.path, 'mcp-pre-start', extra_env=hook_env):
                print('Error: the mcp-pre-start hook aborted the startup',
                      file=sys.stderr)
                sys.exit(1)

            def _on_ready():
                nonlocal started
                started = True
                # Reload the policy when the project configuration changes
                server.watch_config()
                if not run_hooks(project.path, 'mcp-post-start',
                                 extra_env=hook_env):
                    logger.warning('The mcp-post-start hook failed')

            def _on_stop():
                server.stop_config_watch()
                # A hung hook must not stall the shutdown
                if not run_hooks(project.path, 'mcp-pre-stop', timeout=30,
                                 extra_env=hook_env):
                    logger.warning('The mcp-pre-stop hook failed')

            on_ready = _on_ready
            on_stop = _on_stop

        try:
            if args.socket:
                transport = UnixSocketTransport(
                        args.socket, mode=args.socket_mode,
                        max_connections=args.max_connections)
            else:
                transport = StdioTransport()
            transport.serve(server, on_ready=on_ready,
                            on_stop=on_stop)
        except (ValueError, FileExistsError, PermissionError,
                OSError) as errmsg:
            # Errors go to stderr: in stdio mode stdout is reserved for
            # protocol messages
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            # 128 + SIGINT; the transport cleans up after itself
            sys.exit(130)
        finally:
            if started:
                if not run_hooks(project.path, 'mcp-post-stop', timeout=30,
                                 extra_env=hook_env):
                    logger.warning('The mcp-post-stop hook failed')

    def do_mcp_connect(self, args):
        self._mcp_refuse_if_disabled()
        set_process_name('debops-mcp-conn')
        if args.raw and not (args.ping or args.call):
            print('Error: --raw only makes sense with --ping or --call',
                  file=sys.stderr)
            sys.exit(2)
        if args.arguments and not args.call:
            print('Error: tool arguments require --call',
                  file=sys.stderr)
            sys.exit(2)
        if args.call:
            self._mcp_call(args.socket, args.call, args.arguments,
                           args.raw, timeout=args.timeout)
            return
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
        with _mcp_client_errors():
            raw = ping_unix(args.socket,
                            timeout=args.timeout if args.timeout
                            is not None else 5)
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
        capabilities = dict(result.get('capabilities', {}))
        extensions = sorted(capabilities.pop('extensions', {}))
        versions = result.get('supportedVersions', [])
        print('debops mcp server at {}:'.format(args.socket))
        print('    name: {}'.format(server_info.get('name', '?')))
        if meta.get('org.debops/project') is not None:
            print('    project: {}'.format(meta['org.debops/project']))
        if meta.get('org.debops/view') is not None:
            print('    view: {}'.format(meta['org.debops/view']))
        print('    version: {}'.format(server_info.get('version', '?')))
        print('    protocol: {}'.format(', '.join(versions) or '?'))
        print('    capabilities: {}'.format(
                ', '.join(sorted(capabilities)) or '(none)'))
        if extensions:
            print('    extensions: {}'.format(', '.join(extensions)))
        print('    round-trip: {:.0f} ms'.format(rtt * 1000))

    def _mcp_call(self, socket_path, tool_name, pairs, raw,
                  timeout=None):
        """Call a tool on a server and print its result ('--call')."""
        try:
            arguments = mcp_client.parse_call_arguments(pairs)
        except ValueError as errmsg:
            print('Error:', errmsg, file=sys.stderr)
            sys.exit(2)

        # Declaring elicitation support lets gated tools (a playbook run,
        # a push) ask for confirmation, which we prompt for below.
        # Executions run long, so there is no client-side timeout by
        # default; the server bounds them with 'execute_timeout'
        client = UnixClient(socket_path, timeout=timeout,
                            capabilities={'elicitation': {'form': {}}})
        try:
            with _mcp_client_errors():
                params = {'name': tool_name, 'arguments': arguments}
                while True:
                    try:
                        response = client.request('tools/call', params)
                    except KeyboardInterrupt:
                        # Tell the server to cancel the in-flight
                        # request instead of letting the work run on
                        try:
                            client.notify('notifications/cancelled',
                                          {'requestId':
                                           client.inflight_id})
                        except Exception:
                            # Best effort; the client is exiting anyway
                            pass
                        print('Interrupted; asked the server to cancel '
                              'the request', file=sys.stderr)
                        sys.exit(130)
                    if 'error' in response:
                        print('Error: the server returned an error: {}'
                              .format(response['error'].get('message')),
                              file=sys.stderr)
                        sys.exit(1)
                    result = response.get('result', {})
                    if raw:
                        print(json.dumps(response))
                        if result.get('resultType') == 'input_required':
                            print('The server asks for confirmation; '
                                  're-run without --raw to answer '
                                  'interactively', file=sys.stderr)
                            sys.exit(1)
                        break
                    if result.get('resultType') != 'input_required':
                        break
                    try:
                        answers = self._mcp_answer_elicitations(
                                result.get('inputRequests', {}))
                    except KeyboardInterrupt:
                        # Nothing in flight; just leave
                        print(file=sys.stderr)
                        sys.exit(130)
                    if answers is None:
                        sys.exit(1)
                    params = {'name': tool_name, 'arguments': arguments,
                              'inputResponses': answers}
                    if 'requestState' in result:
                        params['requestState'] = result['requestState']
        finally:
            client.close()

        if raw:
            return
        for block in result.get('content', []):
            if block.get('type') == 'text':
                print(block.get('text', ''))
        if result.get('isError'):
            sys.exit(1)

    def _mcp_answer_elicitations(self, input_requests):
        """Answer elicitation requests interactively.

        Returns the 'inputResponses' map for the retried request, or
        None when the requests cannot be answered here.
        """
        answers = {}
        for key, request in input_requests.items():
            if request.get('method') != 'elicitation/create':
                return None
            params = request.get('params', {})
            if params.get('mode', 'form') != 'form':
                return None
            if not sys.stdin.isatty():
                print('The server asks: {}'.format(
                        params.get('message', 'input required')),
                      file=sys.stderr)
                print('Re-run interactively to answer.',
                      file=sys.stderr)
                return None
            print(params.get('message', 'The server asks for input:'))
            schema = params.get('requestedSchema', {})
            content = {}
            for name, prop in schema.get('properties', {}).items():
                ptype = prop.get('type')
                title = prop.get('title', name)
                if ptype == 'boolean':
                    reply = input('    {} [y/N] '.format(title))
                    content[name] = reply.strip().lower() in ('y', 'yes')
                elif ptype == 'string':
                    default = str(prop.get('default', ''))
                    if default:
                        reply = input('    {} [{}] '.format(title,
                                                            default))
                        content[name] = reply.strip() or default
                    else:
                        content[name] = input('    {}: '.format(title))
                else:
                    print('Error: unsupported elicitation field: '
                          '{}'.format(name), file=sys.stderr)
                    return None
            answers[key] = {'action': 'accept', 'content': content}
        return answers

    def do_mcp_inspect(self, args):
        self._mcp_refuse_if_disabled()
        set_process_name('debops-mcp-conn')
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
            with _mcp_client_errors():
                for step in steps:
                    self._mcp_inspect_step(client, step, args)
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
            print('\n'.join(mcp_client.format_prompts(
                    result.get('prompts', []))))
        elif step == 'resources':
            print('\n'.join(mcp_client.format_resources(
                    result.get('resources', []))))

    def do_mcp_configure(self, args):
        self._mcp_refuse_if_disabled()
        # The account level needs no project, and printing needs no
        # directory at all; only writing the project-local file does.
        # Removals work wherever the file is
        project_path = None
        if not args.user:
            try:
                project = ProjectDir(path=args.project_dir,
                                     config=self.config)
            except (IsADirectoryError, NotADirectoryError) as errmsg:
                if not args.print_only:
                    print('Error:', errmsg, file=sys.stderr)
                    sys.exit(1)
            else:
                project_path = project.path
        if args.harness is not None:
            try:
                if args.disconnect is not None:
                    status, lines = disconnect_harness(
                            project_path, args.harness,
                            args.disconnect, filename=args.filename,
                            user=args.user)
                elif args.remove:
                    status, lines = remove_harness(
                            project_path, args.harness,
                            filename=args.filename, user=args.user,
                            name=args.name)
                else:
                    status, lines = configure_harness(
                            project_path, args.harness,
                            filename=args.filename, user=args.user,
                            print_only=args.print_only,
                            name=args.name or 'debops',
                            connect=args.connect,
                            verbosity=args.verbose or 0,
                            log=args.log)
            except ValueError as errmsg:
                print('Error:', errmsg, file=sys.stderr)
                if args.disconnect is None and not args.remove:
                    print()
                    print('The entry to add by hand:')
                    print()
                    print(fragment_for_display(
                            args.harness, connect=args.connect,
                            name=args.name or 'debops',
                            verbosity=args.verbose or 0,
                            log=args.log))
                sys.exit(1)
            for line in lines:
                print(line)
        if args.agents:
            try:
                _status, lines = configure_agents_md(
                        project_path, harness_name=args.harness,
                        print_only=args.print_only)
            except ValueError as errmsg:
                print('Error:', errmsg, file=sys.stderr)
                sys.exit(1)
            for line in lines:
                print(line)

    def do_mcp_skills(self, args):
        self._mcp_refuse_if_disabled()
        # Skills are shipped with the package and do not depend on
        # a project directory; the generated role and playbook briefs
        # cover the bundled collection, the same as in the
        # documentation mode
        role_skills = RoleSkillSource([COLLECTION_ROLES])
        playbook_skills = PlaybookSkillSource(
                COLLECTION_PLAYBOOKS, role_skills, execution=False)
        role_skills.playbooks = playbook_skills
        library = SkillLibrary(sources=[role_skills, playbook_skills])
        if args.skill:
            target = args.skill
            if '.' not in target.rsplit('/', 1)[-1]:
                # A skill is named by its directory; the SKILL.md is
                # explicit in the URI
                target += '/SKILL.md'
            try:
                content = library.read('skill://' + target)
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
            for entry in library.entries():
                path = entry['uri'][len('skill://'):-len('/SKILL.md')]
                if args.filter and not match_skill(args.filter, path):
                    continue
                if not first:
                    print()
                first = False
                frontmatter = entry['frontmatter']
                print(path)
                print(textwrap.fill(str(frontmatter.get('description',
                                                        '')),
                                    width=79, initial_indent='    ',
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
