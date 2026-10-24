# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the command line parser in ``debops.subcommands``.

``Subcommands`` parses the whole command line in its constructor and stores
the resulting namespace in ``self.args``, so each test builds one and inspects
what the user would get.
"""

import os

import pytest

from debops.subcommands import Subcommands


def parse_cli(*argv):
    """Parse ``debops <argv>`` and return the completed Subcommands object."""
    return Subcommands(["debops", *argv])


def test_section_and_command_are_detected():
    subcommands = parse_cli("project", "init", "/tmp/example")

    assert subcommands.section == "project"
    assert subcommands.command == "init"


def test_unknown_section_exits_with_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_cli("bogus")

    assert exc.value.code == 1
    assert "unrecognized section" in capsys.readouterr().err


def test_unknown_project_command_exits_with_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_cli("project", "bogus")

    assert exc.value.code == 1
    assert "unrecognized command" in capsys.readouterr().err


def test_login_shell_names_select_the_front_end():
    for name, command in [("debops-repl", "repl"),
                          ("debops-shell", "shell"),
                          ("debops-console", "console")]:
        subcommands = Subcommands([name])
        assert subcommands.section == "mcp"
        assert subcommands.command == command
        assert subcommands.login_shell == command


def test_login_shell_form_strips_the_dash():
    # A login shell's argv[0] carries the '-' marker
    subcommands = Subcommands(["-debops-shell"])
    assert subcommands.login_shell == "shell"


def test_login_shell_passes_the_command_form_through():
    subcommands = Subcommands(["debops-shell", "-c", "status"])
    assert subcommands.args.command == "status"


def test_plain_invocation_is_not_a_login_shell():
    subcommands = parse_cli("mcp", "shell")
    assert subcommands.login_shell is None


def test_command_form_on_all_front_ends():
    for command in ("repl", "shell", "console"):
        subcommands = parse_cli("mcp", command, "-c", "ls /")
        assert subcommands.args.command == "ls /"


def test_mcp_stop_parses_its_arguments():
    subcommands = parse_cli("mcp", "stop", "--kill")
    assert subcommands.command == "stop"
    assert subcommands.args.kill is True
    assert subcommands.args.docs is False


def test_mcp_inspect_parses_its_arguments():
    subcommands = parse_cli("mcp", "inspect", "--socket", "/tmp/x.sock",
                            "--tools", "--no-spawn")
    assert subcommands.command == "inspect"
    assert subcommands.args.socket == "/tmp/x.sock"
    assert subcommands.args.tools is True
    assert subcommands.args.spawn is False
    # The defaults: the well-known path, spawn allowed
    subcommands = parse_cli("mcp", "inspect")
    assert subcommands.args.socket is None
    assert subcommands.args.spawn is None
    assert subcommands.args.docs is False


def test_mcp_connect_parses_its_arguments():
    subcommands = parse_cli("mcp", "connect", "--socket", "/tmp/x.sock",
                            "--call", "run", "playbook=site")
    assert subcommands.command == "connect"
    assert subcommands.args.socket == "/tmp/x.sock"
    assert subcommands.args.call == "run"
    # The key=value pairs land in the arguments either way around
    assert subcommands.args.arguments == ["playbook=site"]
    subcommands = parse_cli("mcp", "connect", "--call", "run",
                            "playbook=site", "--socket", "/tmp/x.sock")
    assert subcommands.args.socket == "/tmp/x.sock"
    assert subcommands.args.arguments == ["playbook=site"]
    # The defaults: the well-known path, spawn allowed
    subcommands = parse_cli("mcp", "connect")
    assert subcommands.args.socket is None
    assert subcommands.args.spawn is None


def test_mcp_repl_no_longer_has_stop():
    # The daemon stop moved to its own command: 'debops mcp stop'
    with pytest.raises(SystemExit):
        parse_cli("mcp", "repl", "--stop")


def test_a_bare_debops_parses_to_no_section():
    # The project's own start configuration is the Interpreter's call
    subcommands = Subcommands(["debops"])
    assert subcommands.section is None
    assert subcommands.login_shell is None


def test_plain_flag_on_all_front_ends():
    for command in ("repl", "shell", "console"):
        subcommands = parse_cli("mcp", command, "--plain")
        assert subcommands.args.plain is True
    # ... and off by default
    assert parse_cli("mcp", "repl").args.plain is False


@pytest.mark.parametrize("section", ["run", "check"])
def test_debug_flag_on_the_run_sections(section):
    subcommands = parse_cli(section, "--debug", "site")

    assert subcommands.args.debug is True
    assert "site" in subcommands.args.ansible_args
    # ... and off by default
    assert parse_cli(section, "site").args.debug is False


def test_unknown_config_command_exits_with_an_error(capsys):
    with pytest.raises(SystemExit) as exc:
        parse_cli("config", "bogus")

    assert exc.value.code == 1
    assert "unrecognized command" in capsys.readouterr().err


def test_project_init_defaults():
    subcommands = parse_cli("project", "init", "/tmp/example")
    args = subcommands.args

    assert args.type == "legacy"
    assert args.default_view == "system"
    assert args.project_dir == "/tmp/example"
    # Both are enabled by default
    assert args.git is True
    assert args.requirements is True
    # The Python virtual environment is opt-in
    assert args.venv is False
    assert args.encrypt is None
    assert args.spec_sources is None
    assert args.dry_run is False


def test_project_init_disable_flags():
    subcommands = parse_cli("project", "init", "--no-git",
                            "--no-requirements", "/tmp/example")
    args = subcommands.args

    assert args.git is False
    assert args.requirements is False


def test_project_init_enables_the_virtual_environment():
    subcommands = parse_cli("project", "init", "-t", "modern", "--venv",
                            "/tmp/example")

    assert subcommands.args.venv is True


def test_project_init_accepts_all_options():
    subcommands = parse_cli(
        "project", "init", "-t", "modern", "-V", "webservers",
        "--encrypt", "git-crypt", "--keys", "A,B", "--template", "spec.yml",
        "--dry-run", "--force", "--allow-io", "/tmp/example")
    args = subcommands.args

    assert args.type == "modern"
    assert args.default_view == "webservers"
    assert args.encrypt == "git-crypt"
    assert args.keys == "A,B"
    assert args.spec_sources == ["spec.yml"]
    assert args.dry_run is True
    assert args.force is True
    assert args.allow_io is True


def test_project_init_collects_several_specifications():
    subcommands = parse_cli("project", "init", "--template", "first.yml",
                            "--template", "second.yml")
    args = subcommands.args

    assert args.spec_sources == ["first.yml", "second.yml"]


def test_project_init_rejects_an_unknown_type():
    with pytest.raises(SystemExit) as exc:
        parse_cli("project", "init", "-t", "bogus")

    assert exc.value.code == 2


def test_project_refresh_options():
    subcommands = parse_cli("project", "refresh", "-V", "webservers",
                            "--force", "/tmp/example")
    args = subcommands.args

    assert args.view == "webservers"
    assert args.force is True
    assert args.project_dir == "/tmp/example"


@pytest.mark.parametrize("command", ["lock", "unlock"])
def test_project_secret_commands_take_a_view(command):
    subcommands = parse_cli("project", command, "-V", "webservers",
                            "/tmp/example")

    assert subcommands.args.view == "webservers"
    assert subcommands.args.project_dir == "/tmp/example"


def test_project_mkview_options():
    subcommands = parse_cli("project", "mkview", "--encrypt", "encfs",
                            "--keys", "A", "newview")

    assert subcommands.args.new_view == "newview"
    assert subcommands.args.encrypt == "encfs"
    assert subcommands.args.keys == "A"


def test_project_commit_defaults_to_the_current_directory():
    subcommands = parse_cli("project", "commit")

    assert subcommands.args.project_dir == os.getcwd()


def test_project_venv_defaults():
    subcommands = parse_cli("project", "venv", "install", "/tmp/example")
    args = subcommands.args

    assert args.action == "install"
    assert args.scope == "all"
    assert args.project_dir == "/tmp/example"


def test_project_venv_defaults_to_the_current_directory():
    subcommands = parse_cli("project", "venv", "install")

    assert subcommands.args.project_dir == os.getcwd()


@pytest.mark.parametrize(("option", "scope"), [
    ("--python", "python"),
    ("--collections", "collections"),
    ("--all", "all"),
])
def test_project_venv_scope_options(option, scope):
    subcommands = parse_cli("project", "venv", "upgrade", option)

    assert subcommands.args.scope == scope


def test_project_venv_rejects_an_unknown_action():
    with pytest.raises(SystemExit) as exc:
        parse_cli("project", "venv", "bogus")

    assert exc.value.code == 2


def test_project_venv_enable():
    subcommands = parse_cli("project", "venv", "enable", "/tmp/example")
    args = subcommands.args

    assert args.action == "enable"
    assert args.scope == "all"
    assert args.project_dir == "/tmp/example"


@pytest.mark.parametrize("action", ["enable", "install", "upgrade",
                                    "recreate", "freeze"])
def test_project_venv_scope_options_on_all_actions(action):
    subcommands = parse_cli("project", "venv", action, "--python")

    assert subcommands.args.scope == "python"


def test_exec_captures_the_ansible_arguments():
    subcommands = parse_cli("exec", "-V", "webservers", "-E", "--eval",
                            "all", "-m", "ping")
    args = subcommands.args

    assert args.view == "webservers"
    assert args.bell is True
    assert args.eval is True
    assert args.ansible_args == ["all", "-m", "ping"]


def test_run_captures_several_playbooks():
    subcommands = parse_cli("run", "-V", "webservers", "site.yml",
                            "extra.yml", "--limit", "web")
    args = subcommands.args

    assert args.view == "webservers"
    assert args.ansible_args == ["site.yml", "extra.yml", "--limit", "web"]


def test_check_adds_the_check_mode_flags():
    subcommands = parse_cli("check", "site.yml")
    args = subcommands.args

    assert args.ansible_args == ["site.yml", "--diff", "--check"]


def test_check_adds_only_the_missing_flag():
    subcommands = parse_cli("check", "site.yml", "--diff")
    args = subcommands.args

    assert args.ansible_args == ["site.yml", "--diff", "--check"]


def test_check_does_not_duplicate_the_flags():
    subcommands = parse_cli("check", "site.yml", "--diff", "--check")
    args = subcommands.args

    assert args.ansible_args == ["site.yml", "--diff", "--check"]


def test_check_does_not_duplicate_the_short_flags():
    subcommands = parse_cli("check", "site.yml", "-D", "-C")
    args = subcommands.args

    assert args.ansible_args == ["site.yml", "-D", "-C"]


def test_env_defaults_to_the_local_scope():
    subcommands = parse_cli("env", "ls", "-l")
    args = subcommands.args

    assert args.scope == "local"
    assert args.command_args == ["ls", "-l"]


def test_env_accepts_the_full_scope():
    subcommands = parse_cli("env", "--scope", "full", "env")

    assert subcommands.args.scope == "full"


def test_config_list_defaults_to_the_current_directory():
    subcommands = parse_cli("config", "list")

    assert subcommands.args.project_dir == os.getcwd()


def test_config_get_defaults():
    subcommands = parse_cli("config", "get", "project.name")
    args = subcommands.args

    assert args.format == "unix"
    assert args.keys is False
    assert args.key == ["project.name"]


def test_config_get_keys_flag():
    subcommands = parse_cli("config", "get", "-k", "project")

    assert subcommands.args.keys is True


def test_config_get_format_choice():
    subcommands = parse_cli("config", "get", "--format", "json",
                            "project.name")

    assert subcommands.args.format == "json"


def test_mcp_serve_defaults():
    args = parse_cli("mcp", "serve").args

    assert args.instance is None
    assert args.view is None
    assert args.socket is None
    # The socket gets 0660 by default, so that the members of the
    # service account's group can connect
    assert args.socket_mode == 0o660
    assert args.project_dir == os.getcwd()


def test_mcp_serve_instance_derives_the_project_and_view():
    args = parse_cli("mcp", "serve", "--instance", "containers-system",
                     "--instance-root", "/srv/projects").args

    assert args.project_dir == "/srv/projects/containers"
    assert args.view == "system"


def test_mcp_serve_instance_splits_at_the_last_dash():
    args = parse_cli("mcp", "serve", "--instance", "my-infra-prod",
                     "--instance-root", "/srv/projects").args

    assert args.project_dir == "/srv/projects/my-infra"
    assert args.view == "prod"


def test_mcp_serve_instance_without_a_view_uses_the_project_default():
    args = parse_cli("mcp", "serve", "--instance", "containers",
                     "--instance-root", "/srv/projects").args

    assert args.project_dir == "/srv/projects/containers"
    assert args.view is None


def test_mcp_serve_instance_requires_the_root():
    # The instances root is a deployment decision; there is no default
    with pytest.raises(SystemExit):
        parse_cli("mcp", "serve", "--instance", "containers-system")


def test_mcp_serve_instance_rejects_an_explicit_view():
    with pytest.raises(SystemExit):
        parse_cli("mcp", "serve", "--instance", "containers-system",
                  "-V", "other")


def test_mcp_serve_instance_rejects_an_explicit_project_dir():
    with pytest.raises(SystemExit):
        parse_cli("mcp", "serve", "--instance", "containers-system",
                  "--project-dir", "/elsewhere")


@pytest.mark.parametrize("name", ["", "x-", "-x", "-", "a/b", ".x"])
def test_mcp_serve_rejects_bad_instance_names(name):
    with pytest.raises(SystemExit):
        parse_cli("mcp", "serve", "--instance", name)


def test_parse_mcp_instance():
    from debops.subcommands import parse_mcp_instance

    assert parse_mcp_instance("containers-system") == \
        ("containers", "system")
    assert parse_mcp_instance("my-infra-prod") == ("my-infra", "prod")
    assert parse_mcp_instance("containers") == ("containers", None)
    with pytest.raises(ValueError):
        parse_mcp_instance("x-")


def test_mcp_configure_parses():
    args = parse_cli("mcp", "configure", "-H", "opencode").args

    assert args.harness == "opencode"
    assert args.filename is None
    assert args.user is False
    assert args.print_only is False


def test_mcp_configure_options():
    args = parse_cli("mcp", "configure", "--harness", "claude",
                     "-f", "custom.json",
                     "--user", "--print").args

    assert args.harness == "claude"
    assert args.filename == "custom.json"
    assert args.user is True
    assert args.print_only is True


def test_mcp_configure_requires_a_known_harness():
    with pytest.raises(SystemExit):
        parse_cli("mcp", "configure", "-H", "windsurf")


def _help_text(monkeypatch, columns):
    from debops import subcommands

    parser = subcommands._argument_parser(prog='debops example')
    parser.add_argument('--example', help=('a help text long enough to '
                                           'need the wrap ' * 8).strip())
    monkeypatch.setattr(subcommands.shutil, 'get_terminal_size',
                        lambda: os.terminal_size((columns, 24)))
    return parser.format_help()


def test_help_wraps_at_a_comfortable_width(monkeypatch):
    # A wide terminal no longer sprawls: the cap is the width an
    # 80-column terminal would get
    help_text = _help_text(monkeypatch, 200)
    assert all(len(line) <= 78 for line in help_text.splitlines())
    assert 'a help text long enough' in help_text


def test_help_a_narrower_terminal_still_wins(monkeypatch):
    # The cap never widens: the terminal width applies below it
    help_text = _help_text(monkeypatch, 60)
    assert all(len(line) <= 58 for line in help_text.splitlines())
