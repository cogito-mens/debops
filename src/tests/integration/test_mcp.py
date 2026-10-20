# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tests of the 'debops mcp' command line.

These tests drive the real ``python -m debops mcp`` entry point as a
subprocess, so they exercise the argument handling and the protocol
framing a user (or an MCP client) sees.
"""

from debops.mcp.server import McpServer
from debops.mcp.skills import SkillLibrary
from debops.mcp.transport import UnixSocketTransport
import json
import os
import shutil
import signal
import subprocess

from pathlib import Path
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture
def project_dir(tmp_path):
    """A minimal legacy DebOps project directory."""
    (tmp_path / ".debops.cfg").touch()
    return tmp_path


@pytest.fixture
def mcp_env(debops_env):
    """A hermetic environment for the debops subprocess.

    An ANSIBLE_CONFIG left in the ambient environment (for example by
    the shell the tests were started from) makes ProjectDir redirect
    the project path to its parent directory. Neutralize the variable
    so the subprocess uses the project directory it is given.
    """
    return dict(debops_env, ANSIBLE_CONFIG="")


# The per-request "_meta" fields the specification requires
REQUEST_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def disable_confirm_gate(project_dir):
    """Opt a test project out of the confirmation gate (on by default);
    the gate itself is covered by the unit tests."""
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\n")


def make_discover_request(request_id=1):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id,
        "method": "server/discover",
        "params": {"_meta": REQUEST_META},
    }) + "\n"


def read_json_lines(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_serve_stdio_round_trip(run_debops, mcp_env, project_dir):
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=make_discover_request())

    assert result.returncode == 0
    # Nothing except protocol messages may be written to stdout
    (response,) = read_json_lines(result.stdout)
    assert response["id"] == 1
    result = response["result"]
    assert result["supportedVersions"] == ["2026-07-28"]
    assert "tools" in result["capabilities"]
    meta = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert meta["name"] == "debops"


def test_serve_stdio_reports_parse_errors(run_debops, mcp_env,
                                          project_dir):
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text="not json\n")

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["error"]["code"] == -32700


def test_serve_stdio_reports_unknown_methods(run_debops, mcp_env,
                                             project_dir):
    request = (json.dumps({"jsonrpc": "2.0",
                           "id": 5, "method": "prompts/get",
                           "params": {"_meta": REQUEST_META,
                                      "name": "nosuch"}}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["id"] == 5
    assert response["error"]["code"] == -32602

    # A truly unknown method is still reported
    request = (json.dumps({"jsonrpc": "2.0",
                           "id": 6, "method": "nosuch/method",
                           "params": {"_meta": REQUEST_META}}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["id"] == 6
    assert response["error"]["code"] == -32601


def test_serve_verbose_logs_startup_notice(run_debops, mcp_env,
                                           project_dir):
    result = run_debops("mcp", "serve", "-v", cwd=str(project_dir),
                        env=mcp_env, input_text=make_discover_request())

    assert result.returncode == 0
    # stdout stays protocol-only
    (response,) = read_json_lines(result.stdout)
    assert response["id"] == 1
    # the startup notices go to stderr at the NOTICE level
    assert "Serving project" in result.stderr
    assert "view 'system'" in result.stderr
    assert "serving on stdio" in result.stderr


def test_serve_outside_a_project_serves_documentation(
        run_debops, mcp_env, tmp_path):
    """Outside of a project directory the server runs in the
    documentation mode: the bundled content and the collections of the
    effective ANSIBLE_COLLECTIONS_PATH, read-only."""
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/list",
        "params": {"_meta": REQUEST_META},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(tmp_path), env=mcp_env,
                        input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = sorted(tool["name"] for tool in response["result"]["tools"])
    assert names == ["collection.playbooks", "collection.roles",
                     "playbook.info", "playbook.list",
                     "role.argspec", "role.defaults", "role.files",
                     "role.handlers", "role.info", "role.tasks",
                     "role.templates", "role.variables"]


def test_serve_explicit_nonproject_dir_fails(run_debops, mcp_env,
                                             tmp_path):
    # An explicitly named directory must be a project; only the ambient
    # detection falls back to the documentation mode
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = run_debops("mcp", "serve", "--project-dir",
                        str(elsewhere), cwd=str(tmp_path), env=mcp_env,
                        input_text="")

    assert result.returncode == 1
    assert "project directory not found" in result.stderr
    # Nothing may be written to the protocol channel
    assert result.stdout == ""


def test_serve_docs_flag_inside_a_project(run_debops, mcp_env,
                                          project_dir):
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/list",
        "params": {"_meta": REQUEST_META},
    }) + "\n"
    result = run_debops("mcp", "serve", "--docs", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "project.read" not in names
    assert "role.info" in names


def test_serve_docs_rejects_view(run_debops, mcp_env, project_dir):
    result = run_debops("mcp", "serve", "--docs", "--view", "web",
                        cwd=str(project_dir), env=mcp_env, input_text="")

    assert result.returncode == 2
    assert "--docs" in result.stderr


def _hook_canary(hook_dir, marker):
    """An executable hook which leaves a marker file when it fires."""
    hook_dir.mkdir(parents=True)
    script = hook_dir / "00-canary"
    script.write_text("#!/bin/sh\ntouch {}\n".format(marker))
    script.chmod(0o755)


def test_docs_mode_never_runs_hooks(run_debops, mcp_env, tmp_path):
    # The documentation mode invokes no hooks — not even the
    # user-global ones, which run from any directory they are handed
    marker = tmp_path / "hook-fired"
    _hook_canary(Path(mcp_env["XDG_CONFIG_HOME"]) / "debops" / "hooks"
                 / "mcp-pre-start", marker)

    result = run_debops("mcp", "serve", cwd=str(tmp_path), env=mcp_env,
                        input_text=make_discover_request())
    assert result.returncode == 0
    assert "supportedVersions" in result.stdout
    assert not marker.exists()


def test_docs_flag_never_runs_hooks_inside_a_project(
        run_debops, mcp_env, project_dir):
    # Even inside a project, the documentation mode fires neither the
    # project-local nor the user-global hooks (the lifecycle test is
    # the control: in the project mode they all fire)
    project_marker = project_dir / "project-hook-fired"
    user_marker = project_dir / "user-hook-fired"
    _hook_canary(project_dir / ".debops" / "hooks" / "mcp-pre-start",
                 project_marker)
    _hook_canary(Path(mcp_env["XDG_CONFIG_HOME"]) / "debops" / "hooks"
                 / "mcp-pre-start", user_marker)

    result = run_debops("mcp", "serve", "--docs", cwd=str(project_dir),
                        env=mcp_env, input_text=make_discover_request())
    assert result.returncode == 0
    assert "supportedVersions" in result.stdout
    assert not project_marker.exists()
    assert not user_marker.exists()


class _LiveServer:
    """A long-lived 'mcp serve' process answering sequential tool calls.

    One process per call would lose the active view; the view tests
    need the same server to answer across calls. stderr is discarded:
    a pipe would deadlock the server once the buffer fills.
    """

    def __init__(self, project_dir, env, source_dir, serve_args=()):
        env = dict(env)
        env["PYTHONPATH"] = os.pathsep.join(
                [source_dir, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        self.process = subprocess.Popen(
                [sys.executable, "-m", "debops", "mcp", "serve",
                 *serve_args],
                cwd=str(project_dir), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True)
        self._counter = 0

    def call(self, name, arguments):
        self._counter += 1
        request = json.dumps({
            "jsonrpc": "2.0", "id": self._counter,
            "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": name,
                       "arguments": arguments},
        }) + "\n"
        self.process.stdin.write(request)
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise AssertionError(
                    "the server exited ({}) without answering".format(
                            self.process.poll()))
        return json.loads(line)

    def stop(self):
        self.process.terminate()
        self.process.wait(timeout=10)


def test_no_view_startup_and_recovery_via_stdio(run_debops, mcp_env,
                                                tmp_path,
                                                debops_source_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "-t", "modern", "--no-requirements",
               str(project_dir), env=mcp_env, check=True)
    # An empty default view: a deliberate configuration meaning no view
    # is active until one is selected explicitly
    (project_dir / ".debops" / "conf.d" / "zz-noview.yml").write_text(
            "---\nproject:\n  default_view: ''\n")

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        # The view-bound tools answer with the recovery path
        blocked = server.call("file.write",
                              {"path": "playbooks/x.yml",
                               "content": "---\n"})
        assert blocked["result"]["isError"]
        assert "no active infrastructure view" in \
            blocked["result"]["content"][0]["text"]
        assert "view.switch" in blocked["result"]["content"][0]["text"]

        # The discovery anchor works and names the registered view
        listing = server.call("view.list", {})
        assert "system" in listing["result"]["content"][0]["text"]
        assert "Active: none" in listing["result"]["content"][0]["text"]

        # Selecting the view unlocks the surface mid-session
        switched = server.call("view.switch", {"view": "system"})
        assert not switched["result"].get("isError", False)
        written = server.call("file.write",
                              {"path": "playbooks/x.yml",
                               "content": "---\n"})
        assert not written["result"].get("isError", False)
        assert (project_dir / "ansible" / "views" / "system"
                / "playbooks" / "x.yml").exists()
        # The _meta stamp reports the active view on every result
        assert switched["result"]["_meta"]["org.debops/view"] == \
            "system"
    finally:
        server.stop()


def test_view_create_and_switch_via_stdio(run_debops, mcp_env,
                                          tmp_path,
                                          debops_source_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "-t", "modern", "--no-requirements",
               str(project_dir), env=mcp_env, check=True)

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        created = server.call("view.create", {"view": "webservers"})
        assert not created["result"].get("isError", False)
        assert (project_dir / "ansible" / "views" / "webservers"
                ).is_dir()
        assert (project_dir / ".debops" / "conf.d"
                / "view-webservers.yml").exists()
        log = subprocess.run(["git", "log", "--format=%s"],
                             cwd=str(project_dir), capture_output=True,
                             text=True, check=True)
        assert "DebOps MCP: view.create webservers" in log.stdout

        switched = server.call("view.switch", {"view": "webservers"})
        assert not switched["result"].get("isError", False)
        status = server.call("project.status", {})
        assert "View: webservers" in \
            status["result"]["content"][0]["text"]
    finally:
        server.stop()


def test_view_switch_refused_with_view_lock(run_debops, mcp_env,
                                            tmp_path,
                                            debops_source_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "-t", "modern", "--no-requirements",
               str(project_dir), env=mcp_env, check=True)

    # The command line flag...
    server = _LiveServer(project_dir, mcp_env, debops_source_dir,
                         ("--view-lock",))
    try:
        refused = server.call("view.switch", {"view": "system"})
        assert refused["result"]["isError"]
        assert "locked" in refused["result"]["content"][0]["text"]
    finally:
        server.stop()

    # ...and the configuration key behave the same
    (project_dir / ".debops" / "conf.d" / "zz-lock.yml").write_text(
            "---\nproject:\n  mcp:\n    view_lock: true\n")
    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        refused = server.call("view.switch", {"view": "system"})
        assert refused["result"]["isError"]
        assert "locked" in refused["result"]["content"][0]["text"]
    finally:
        server.stop()


def test_view_switch_on_legacy_is_refused(run_debops, mcp_env,
                                          tmp_path,
                                          debops_source_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "--no-requirements",
               str(project_dir), env=mcp_env, check=True)

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        refused = server.call("view.switch", {"view": "system"})
        assert refused["result"]["isError"]
        assert "legacy" in refused["result"]["content"][0]["text"]
    finally:
        server.stop()


def test_view_switch_refused_while_secrets_unlocked(
        run_debops, tmp_path, gpg_env, debops_source_dir):
    if shutil.which("git-crypt") is None:
        pytest.skip("git-crypt is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "-t", "modern", "--encrypt",
               "git-crypt", "--keys", "debops-tests@example.org",
               "--no-requirements", str(project_dir), env=gpg_env,
               check=True)

    server = _LiveServer(project_dir, gpg_env, debops_source_dir)
    try:
        # git-crypt starts locked; the switch works
        switched = server.call("view.switch", {"view": "system"})
        assert not switched["result"].get("isError", False)
        created = server.call("view.create", {"view": "web"})
        assert not created["result"].get("isError", False)

        run_debops("project", "unlock", cwd=str(project_dir),
                   env=gpg_env, check=True)
        refused = server.call("view.switch", {"view": "web"})
        assert refused["result"]["isError"]
        text = refused["result"]["content"][0]["text"]
        assert "git-crypt" in text
        assert "debops project lock" in text

        run_debops("project", "lock", cwd=str(project_dir),
                   env=gpg_env, check=True)
        switched = server.call("view.switch", {"view": "web"})
        assert not switched["result"].get("isError", False)
    finally:
        server.stop()


def test_serve_exits_quietly_on_sigint(debops_source_dir, mcp_env,
                                       project_dir):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    process = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve"],
            cwd=str(project_dir), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(1)
        process.send_signal(signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 130
    assert "Traceback" not in stderr


def test_tools_list_via_stdio(run_debops, mcp_env, project_dir):
    request = (json.dumps({"jsonrpc": "2.0",
                           "id": 1, "method": "tools/list",
                           "params": {"_meta": REQUEST_META}}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert sorted(names) == ["ansible.facts", "ansible.ping", "check",
                             "collection.playbooks", "collection.remove",
                             "collection.require", "collection.roles",
                             "collection.sync", "config.get",
                             "environment.list",
                             "environment.set", "environment.unset",
                             "file.delete", "file.write",
                             "inventory.query",
                             "inventory.spec", "keyring.fetch",
                             "playbook.info",
                             "playbook.list", "project.files",
                             "project.pull", "project.push", "project.read",
                             "project.status", "role.argspec",
                             "role.defaults", "role.files",
                             "role.handlers", "role.info", "role.tasks",
                             "role.templates", "role.variables",
                             "secret.paths", "view.create", "view.list",
                             "view.switch"]


def test_role_defaults_via_stdio(run_debops, mcp_env, project_dir):
    brief = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "role.defaults",
                   "arguments": {"name": "elasticsearch",
                                 "filter": "base_packages"}},
    })
    full = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "role.defaults",
                   "arguments": {"name": "elasticsearch",
                                 "filter": "base_packages",
                                 "detail": "full"}},
    })
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=brief + "\n" + full + "\n")

    assert result.returncode == 0
    # Responses may arrive out of order; requests run concurrently
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    brief_text = responses[1]["result"]["content"][0]["text"]
    full_text = responses[2]["result"]["content"][0]["text"]
    assert "- **elasticsearch__base_packages**" in brief_text
    assert "```yaml" not in brief_text
    assert "## elasticsearch__base_packages" in full_text
    assert "```yaml" in full_text


def test_project_read_via_stdio(run_debops, mcp_env, project_dir):
    group_vars = project_dir / "ansible" / "inventory" / "group_vars" / "all"
    group_vars.mkdir(parents=True)
    (group_vars / "nginx.yml").write_text("---\nnginx__enabled: True\n")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "project.read",
                   "arguments": {"path": "ansible/inventory/group_vars/"
                                 "all/nginx.yml"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert "nginx__enabled: True" in response["result"]["content"][0]["text"]


def test_inventory_query_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("ansible-inventory") is None:
        pytest.skip("ansible-inventory is not installed")

    # A legacy project keeps its ansible.cfg in the project root
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text("[testgroup]\ntesthost\n")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "inventory.query", "arguments": {}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["result"]["isError"] is False
    assert "@testgroup" in response["result"]["content"][0]["text"]


def test_role_inspection_via_stdio(run_debops, mcp_env, project_dir):
    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"_meta": REQUEST_META, "name": "role.tasks",
                               "arguments": {"name": "nginx",
                                             "file": "main"}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2,
                      "method": "tools/call",
                      "params": {"_meta": REQUEST_META, "name": "role.templates",
                                 "arguments": {"name": "nginx"}}}) + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    tasks_text = responses[1]["result"]["content"][0]["text"]
    assert "# debops.nginx tasks/main.yml" in tasks_text
    assert "ansible.builtin.import_role" in tasks_text
    templates_text = responses[2]["result"]["content"][0]["text"]
    assert "etc/nginx/" in templates_text


def test_collection_roles_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("ansible-galaxy") is None:
        pytest.skip("ansible-galaxy is not installed")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "collection.roles", "arguments": {}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    # Whether or not the machine has external collections installed,
    # the call itself works
    assert response["result"]["isError"] is False
    text = response["result"]["content"][0]["text"]
    assert ("Collections" in text or "no collections" in text)


def test_playbooks_via_stdio(run_debops, mcp_env, project_dir):
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "playbook.list",
                   "arguments": {"filter": "service/nginx"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert "service/nginx" in text
    assert "Manage nginx webserver" in text


def test_playbook_info_via_stdio(run_debops, mcp_env, project_dir):
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "playbook.info",
                   "arguments": {"playbook": "service/nginx"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert "Execution target: `service/nginx`" in text
    assert "debops_service_nginx" in text
    assert "debops.debops.nginx" in text
    assert "debops.debops.keyring" in text


def test_check_tool_runs_a_playbook_in_check_mode(run_debops, mcp_env,
                                                  project_dir):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    disable_confirm_gate(project_dir)

    # A legacy project keeps its ansible.cfg in the project root
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from mcp
""")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "check",
                   "arguments": {"playbook": "custom"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False
    assert "exit code: 0" in text
    assert "hello from mcp" in text


def test_check_runs_multiple_playbooks_in_order(run_debops, mcp_env,
                                                project_dir):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    disable_confirm_gate(project_dir)

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    for name in ("first", "second"):
        (playbooks / (name + ".yml")).write_text("""---
- name: Playbook {0}
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say {0}
      ansible.builtin.debug:
        msg: "hello from {0}"
""".format(name))

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "check",
                   "arguments": {"playbook": ["first", "second"]}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    result_text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False
    assert "exit code: 0" in result_text
    # Both playbooks ran, in specification order
    first = result_text.index("hello from first")
    second = result_text.index("hello from second")
    assert first < second
    assert result_text.startswith("# debops check first second")


def test_write_check_run_loop_via_stdio(run_debops, mcp_env,
                                        project_dir):
    """The loop the write tools exist for: write a playbook, see it
    listed, run it in check mode; the writes are committed."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    disable_confirm_gate(project_dir)

    # The write tools require a git-tracked project
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="Test",
               GIT_COMMITTER_EMAIL="t@example.org")
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir), env=env,
                   check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")

    playbook = """---
- name: Written by the agent
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hi
      ansible.builtin.debug:
        msg: written by mcp
"""

    def call(name, arguments):
        # Dependent steps go in separate invocations: the server
        # handles a batch concurrently, with no ordering guarantee
        request = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"_meta": REQUEST_META, "name": name,
                           "arguments": arguments}}) + "\n"
        result = run_debops("mcp", "serve", cwd=str(project_dir),
                            env=mcp_env, input_text=request)
        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        return response["result"]

    written = call("file.write",
                   {"path": "playbooks/written.yml",
                    "content": playbook})
    assert "created: playbooks/written.yml" in written["content"][0]["text"]
    listed = call("playbook.list", {"filter": "written"})
    assert "written" in listed["content"][0]["text"]
    checked = call("check", {"playbook": "written"})
    assert checked["isError"] is False
    assert "written by mcp" in checked["content"][0]["text"]

    # The write was committed with MCP provenance
    log = subprocess.run(["git", "log", "--format=%s", "-1"],
                         cwd=str(project_dir), env=env,
                         capture_output=True, text=True)
    assert "DebOps MCP: file.write playbooks/written.yml" in log.stdout


def test_inventory_spec_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    if shutil.which("ansible-inventory") is None:
        pytest.skip("ansible-inventory is not installed")

    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="Test",
               GIT_COMMITTER_EMAIL="t@example.org")
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir), env=env,
                   check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")

    def call(name, arguments):
        # Dependent steps go in separate invocations: a batch runs
        # concurrently, without ordering
        request = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "tools/call",
                "params": {"_meta": REQUEST_META, "name": name,
                           "arguments": arguments}}) + "\n"
        result = run_debops("mcp", "serve", cwd=str(project_dir),
                            env=mcp_env, input_text=request)
        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        return response["result"]

    spec = ("version: 0\n"
            "files:\n"
            "  hosts: |\n"
            "    [spec_group]\n"
            "    spec1.example.com\n")
    preview = call("inventory.spec", {"spec": spec, "dry_run": True})
    assert "dry run" in preview["content"][0]["text"]
    assert "Planned writes" in preview["content"][0]["text"]
    # Nothing was written
    assert not (project_dir / "ansible" / "inventory" / "hosts").exists()

    applied = call("inventory.spec", {"spec": spec})
    assert "## Written (1)" in applied["content"][0]["text"]
    assert (project_dir / "ansible" / "inventory"
            / "hosts").read_text() == "[spec_group]\nspec1.example.com\n"

    graph = call("inventory.query", {"action": "graph"})
    assert "spec_group" in graph["content"][0]["text"]

    # Committed with MCP provenance
    log = subprocess.run(["git", "log", "--format=%s", "-1"],
                         cwd=str(project_dir), env=env,
                         capture_output=True, text=True)
    assert "DebOps MCP: inventory.spec" in log.stdout


def test_check_of_a_local_playbook_asks_via_stdio(run_debops, mcp_env,
                                                  project_dir):
    """A check run of a project-local playbook elicits confirmation;
    a check of a collection playbook does not."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Local playbook
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from mcp
""")

    elicit_meta = dict(REQUEST_META, **{
            "io.modelcontextprotocol/clientCapabilities":
            {"elicitation": {"form": {}}}})
    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"_meta": elicit_meta,
                               "name": "check",
                               "arguments": {"playbook": "custom"}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2,
                      "method": "tools/call",
                      "params": {
                          "_meta": elicit_meta,
                          "name": "check",
                          "arguments": {"playbook": "custom"},
                          "inputResponses": {
                              "confirmation": {
                                  "action": "accept",
                                  "content": {"confirmed": True}}}}})
        + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    first = responses[1]["result"]
    assert first["resultType"] == "input_required"
    confirmation = first["inputRequests"]["confirmation"]
    assert "even in check mode" in confirmation["params"]["message"]
    assert "custom" in confirmation["params"]["message"]
    second = responses[2]["result"]
    assert second["isError"] is False
    assert "hello from mcp" in second["content"][0]["text"]


def test_check_via_tasks_extension(run_debops, mcp_env, project_dir,
                                   tmp_path, debops_source_dir):
    """A task-ified check of a project-local playbook: handle, the
    mid-task confirmation via tasks/update, then the result."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Local playbook
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from a task
""")

    tasks_meta = dict(REQUEST_META, **{
            "io.modelcontextprotocol/clientCapabilities":
            {"extensions":
             {"io.modelcontextprotocol/tasks": {}}}})

    # The task registry lives in one process: the test drives a daemon
    # over a socket, not one-shot stdio invocations
    socket_path = str(tmp_path / "tasks.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path],
            cwd=str(project_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def call(method, params, request_id):
        import socket as socket_module
        connection = socket_module.socket(socket_module.AF_UNIX,
                                          socket_module.SOCK_STREAM)
        connection.connect(socket_path)
        connection.sendall((json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": method,
                "params": dict(params or {}, _meta=tasks_meta)})
                + "\n").encode())
        reader = connection.makefile("rb")
        response = json.loads(reader.readline())
        connection.close()
        return response

    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        response = call("tools/call",
                        {"name": "check",
                         "arguments": {"playbook": "custom"}}, 1)
        result = response["result"]
        assert result["resultType"] == "task"
        assert result["status"] == "working"
        task_id = result["taskId"]

        # Poll until the confirmation request is outstanding
        deadline = time.monotonic() + 30
        task = {}
        while time.monotonic() < deadline:
            task = call("tasks/get", {"taskId": task_id}, 2)["result"]
            if task["status"] == "input_required":
                break
            time.sleep(0.2)
        assert task["status"] == "input_required"
        assert "confirmation" in task["inputRequests"]

        # Answer, then collect the result
        ack = call("tasks/update", {
                "taskId": task_id,
                "inputResponses": {"confirmation": {
                    "action": "accept",
                    "content": {"confirmed": True}}}}, 3)
        assert ack["result"]["resultType"] == "complete"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            task = call("tasks/get", {"taskId": task_id}, 4)["result"]
            if task["status"] == "completed":
                break
            time.sleep(0.2)
        assert task["status"] == "completed"
        assert "hello from a task" in \
            task["result"]["content"][0]["text"]
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_run_asks_for_confirmation_via_stdio(run_debops, mcp_env,
                                             project_dir):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")

    # 'run' is registered only when the project policy allows changes
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True)
    (confd / "mcp.toml").write_text('[project.mcp]\nexecute = "all"\n')
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from mcp
""")

    # The client declares form-mode elicitation support
    elicit_meta = dict(REQUEST_META, **{
            "io.modelcontextprotocol/clientCapabilities":
            {"elicitation": {"form": {}}}})
    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"_meta": elicit_meta,
                               "name": "run",
                               "arguments": {"playbook": "custom"}}}) + "\n"
        # The retry carries the user's answer, with a fresh request id
        + json.dumps({"jsonrpc": "2.0", "id": 2,
                      "method": "tools/call",
                      "params": {
                          "_meta": elicit_meta,
                          "name": "run",
                          "arguments": {"playbook": "custom"},
                          "inputResponses": {
                              "confirmation": {
                                  "action": "accept",
                                  "content": {"confirmed": True}}}}})
        + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    first = responses[1]["result"]
    assert first["resultType"] == "input_required"
    confirmation = first["inputRequests"]["confirmation"]
    assert confirmation["method"] == "elicitation/create"
    assert confirmation["params"]["mode"] == "form"
    assert "custom" in confirmation["params"]["message"]
    second = responses[2]["result"]
    assert second["isError"] is False
    assert "hello from mcp" in second["content"][0]["text"]


def test_secret_paths_via_stdio(run_debops, mcp_env, project_dir):
    secret = (project_dir / "ansible" / "secret" / "credentials"
              / "host1.example.com")
    secret.mkdir(parents=True)
    (secret / "password").write_text("the password\n")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "secret.paths",
                   "arguments": {"pattern": "**/password"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    # The path is served, the content is not
    assert "credentials/host1.example.com/password" in text
    assert "the password" not in text


def test_ansible_ping_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("ansible") is None:
        pytest.skip("ansible is not installed")

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "ansible.ping",
                   "arguments": {"hosts": "local"}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["result"]["isError"] is False
    assert "pong" in response["result"]["content"][0]["text"]


def test_kill_switch_flow(run_debops, mcp_env, project_dir):
    # Enabled by default
    result = run_debops("config", "get", "mcp.enabled",
                        cwd=str(project_dir), env=mcp_env)
    assert result.stdout.strip() == "true"

    # The disable command writes the kill switch at the user level
    result = run_debops("mcp", "disable", cwd=str(project_dir),
                        env=mcp_env)
    assert result.returncode == 0
    killswitch = (project_dir / "config" / "debops" / "conf.d"
                  / "mcp-killswitch.toml")
    assert killswitch.is_file()
    assert "enabled = false" in killswitch.read_text()

    # Now the server and the other commands refuse to run
    for args in (("mcp", "serve"), ("mcp", "skills"),
                 ("mcp", "connect", "nosuch.sock")):
        result = run_debops(*args, cwd=str(project_dir), env=mcp_env,
                            input_text="")
        assert result.returncode == 1
        assert "disabled" in result.stderr

    # Disabling again is idempotent
    result = run_debops("mcp", "disable", cwd=str(project_dir),
                        env=mcp_env)
    assert result.returncode == 0
    assert "already disabled" in result.stdout


def test_connect_ping(run_debops, mcp_env, tmp_path):
    socket_path = str(tmp_path / "server.sock")
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(McpServer(),),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        result = run_debops("mcp", "connect", socket_path, "--ping",
                            env=mcp_env)
        assert result.returncode == 0
        assert "name: debops" in result.stdout
        assert "protocol: 2026-07-28" in result.stdout
        assert "capabilities: tools" in result.stdout
        assert "extensions: io.modelcontextprotocol/tasks" in \
            result.stdout
        # The test server is not confined to a project
        assert "project:" not in result.stdout

        result = run_debops("mcp", "connect", socket_path, "--ping",
                            "--raw", env=mcp_env)
        assert result.returncode == 0
        response = json.loads(result.stdout)
        meta = response["result"]["_meta"]
        server_info = meta["io.modelcontextprotocol/serverInfo"]
        assert server_info["name"] == "debops"
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_ping_shows_project_and_view(run_debops,
                                             debops_source_dir, mcp_env,
                                             project_dir, tmp_path):
    socket_path = str(tmp_path / "project.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path],
            cwd=str(project_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        result = run_debops("mcp", "connect", socket_path, "--ping",
                            env=mcp_env)
        assert result.returncode == 0
        assert "project: " + project_dir.name in result.stdout
        assert "view: system" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_serve_a_named_instance(run_debops, debops_source_dir, mcp_env,
                                tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    instances = tmp_path / "instances"
    project_dir = instances / "containers"
    run_debops("project", "init", "--no-requirements", str(project_dir),
               env=mcp_env, check=True)
    socket_path = str(tmp_path / "containers-system.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    # The server runs from an unrelated directory: the instance name
    # alone must resolve the project
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--instance", "containers-system", "--instance-root",
             str(instances), "--socket", socket_path],
            cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        result = run_debops("mcp", "connect", socket_path, "--ping",
                            env=mcp_env)
        assert result.returncode == 0
        assert "project: containers" in result.stdout
        assert "view: system" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_serve_a_missing_instance_fails(run_debops, mcp_env, tmp_path):
    result = run_debops("mcp", "serve", "--instance", "nosuch-system",
                        "--instance-root", str(tmp_path), env=mcp_env)
    assert result.returncode == 1
    assert "nosuch" in result.stderr


def test_inspect_via_socket(run_debops, mcp_env, tmp_path):
    socket_path = str(tmp_path / "server.sock")
    server = McpServer(skills=SkillLibrary())
    server.register_tool(
            "mop", "Mop the floors. Repeatedly.",
            {"type": "object",
             "properties": {"bucket": {"type": "string",
                                       "description": "The bucket."}},
             "required": ["bucket"]},
            lambda arguments: {"content": [], "isError": False},
            annotations={"destructiveHint": False,
                         "idempotentHint": True})
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(server,),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        # The default inspection is the discovery summary
        result = run_debops("mcp", "inspect", socket_path, env=mcp_env)
        assert result.returncode == 0
        assert "name: debops" in result.stdout
        assert "instructions:" in result.stdout
        assert "capabilities:" in result.stdout

        result = run_debops("mcp", "inspect", socket_path, "--tools",
                            env=mcp_env)
        assert result.returncode == 0
        assert "tools (1):" in result.stdout
        assert "mop" in result.stdout
        assert "[non-destructive, idempotent]" in result.stdout

        result = run_debops("mcp", "inspect", socket_path,
                            "--tool", "mop", env=mcp_env)
        assert result.returncode == 0
        assert "mop — Mop the floors." in result.stdout
        assert "bucket, required" in result.stdout

        result = run_debops("mcp", "inspect", socket_path, "--resources",
                            env=mcp_env)
        assert result.returncode == 0
        assert "debops-cli" in result.stdout

        # The prompts stub answers gracefully
        result = run_debops("mcp", "inspect", socket_path, "--prompts",
                            env=mcp_env)
        assert result.returncode == 0
        assert "does not support" in result.stdout

        # Raw responses are one JSON line per selected step
        result = run_debops("mcp", "inspect", socket_path, "--raw",
                            "--discover", "--tools", env=mcp_env)
        assert result.returncode == 0
        first, second = read_json_lines(result.stdout)
        assert first["result"]["supportedVersions"] == ["2026-07-28"]
        assert second["result"]["tools"][0]["name"] == "mop"
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_call_via_socket(run_debops, mcp_env, tmp_path):
    socket_path = str(tmp_path / "server.sock")
    server = McpServer()
    server.register_tool(
            "mop", "Mop the floors.",
            {"type": "object",
             "properties": {"bucket": {"type": "string"}}},
            lambda arguments: {"content": [{"type": "text",
                                            "text": "mopping with "
                                            + arguments["bucket"]}],
                               "isError": False})
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(server,),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        result = run_debops("mcp", "connect", socket_path,
                            "--call", "mop", "bucket=zinc", env=mcp_env)
        assert result.returncode == 0
        assert result.stdout.strip() == "mopping with zinc"

        result = run_debops("mcp", "connect", socket_path,
                            "--call", "mop", "bucket=zinc", "--raw",
                            env=mcp_env)
        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["result"]["isError"] is False

        # The short forms
        result = run_debops("mcp", "connect", socket_path,
                            "-c", "mop", "bucket=zinc", "-t", "5",
                            env=mcp_env)
        assert result.returncode == 0
        assert "mopping with zinc" in result.stdout

        # A malformed pair is a usage error
        result = run_debops("mcp", "connect", socket_path,
                            "--call", "mop", "bogus", env=mcp_env)
        assert result.returncode == 2
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_call_asks_for_confirmation(run_debops, mcp_env,
                                            tmp_path):
    socket_path = str(tmp_path / "server.sock")
    policy = type("Policy", (), {"confirm": {"run"}})()
    server = McpServer(policy=policy)
    server.register_tool(
            "run", "Run things.", {"type": "object"},
            lambda arguments: {"content": [{"type": "text",
                                            "text": "ran"}],
                               "isError": False})
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(server,),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        # A non-interactive caller cannot answer the confirmation
        result = run_debops("mcp", "connect", socket_path,
                            "--call", "run", "playbook=site",
                            env=mcp_env, input_text="")
        assert result.returncode == 1
        assert "The server asks:" in result.stderr
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_call_timeout(run_debops, mcp_env, tmp_path):
    socket_path = str(tmp_path / "server.sock")
    server = McpServer()

    def slow(arguments, context):
        # A slow tool which notices cancellation and reports
        if context.cancelled.wait(3):
            raise RequestCancelled()
        return {"content": [{"type": "text", "text": "finally"}],
                "isError": False}

    server.register_tool("slow", "A slow tool.", {"type": "object"},
                         slow, needs_context=True)
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(server,),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        # With a timeout, the client gives up while the tool runs on
        result = run_debops("mcp", "connect", socket_path,
                            "--call", "slow", "--timeout", "0.5",
                            env=mcp_env)
        assert result.returncode == 1
        assert "no response within" in result.stderr

        # Without one, the client waits for the tool to finish
        result = run_debops("mcp", "connect", socket_path,
                            "--call", "slow", env=mcp_env)
        assert result.returncode == 0
        assert "finally" in result.stdout
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_call_interrupt_cancels(debops_source_dir, mcp_env,
                                        tmp_path):
    socket_path = str(tmp_path / "server.sock")
    server = McpServer()
    cancelled = threading.Event()

    def slow(arguments, context):
        if context.cancelled.wait(30):
            cancelled.set()
            raise RequestCancelled()
        return {"content": [{"type": "text", "text": "finally"}],
                "isError": False}

    server.register_tool("slow", "A slow tool.", {"type": "object"},
                         slow, needs_context=True)
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(server,),
                              daemon=True)
    thread.start()

    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        process = subprocess.Popen(
                [sys.executable, "-m", "debops", "mcp", "connect",
                 socket_path, "--call", "slow"],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
        time.sleep(1)
        process.send_signal(signal.SIGINT)
        _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130
        assert b"asked the server to cancel" in stderr
        # The cancellation reached the server: the tool noticed
        assert cancelled.wait(10)
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_serve_lifecycle_hooks(run_debops, mcp_env, project_dir):
    """The serve hooks fire in order across a stdio serve."""
    marker = project_dir / "hooks.log"
    for event in ("mcp-pre-start", "mcp-post-start", "mcp-pre-stop", "mcp-post-stop"):
        hook_dir = project_dir / ".debops" / "hooks" / event
        hook_dir.mkdir(parents=True)
        script = hook_dir / "00-mark"
        script.write_text("#!/bin/sh\n"
                          "echo {} \"$DEBOPS_MCP_TRANSPORT\" >> \"{}\"\n"
                          .format(event, marker))
        script.chmod(0o755)

    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=make_discover_request())
    assert result.returncode == 0
    lines = marker.read_text().splitlines()
    assert [line.split(" ")[0] for line in lines] == [
            "mcp-pre-start", "mcp-post-start", "mcp-pre-stop", "mcp-post-stop"]
    # The transport lands in the hook environment
    assert all(line.endswith(" stdio") for line in lines)


def test_serve_pre_start_hook_aborts_startup(run_debops, mcp_env,
                                             project_dir):
    hook_dir = project_dir / ".debops" / "hooks" / "mcp-pre-start"
    hook_dir.mkdir(parents=True)
    script = hook_dir / "00-refuse"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)

    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=make_discover_request())
    assert result.returncode == 1
    assert "mcp-pre-start hook aborted" in result.stderr
    # Nothing was served
    assert "supportedVersions" not in result.stdout


def test_connect_ping_missing_socket(run_debops, mcp_env, tmp_path):
    result = run_debops("mcp", "connect", str(tmp_path / "nosuch.sock"),
                        "--ping", env=mcp_env)
    assert result.returncode == 1
    assert "cannot connect" in result.stderr


def test_connect_raw_without_ping(run_debops, mcp_env, tmp_path):
    result = run_debops("mcp", "connect", "nosuch.sock", "--raw",
                        env=mcp_env)
    assert result.returncode == 2
    assert "--ping" in result.stderr


def test_project_sync_via_stdio(run_debops, mcp_env, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    project_dir = tmp_path / "example"
    run_debops("project", "init", "--no-requirements", str(project_dir),
               env=mcp_env, check=True)
    origin_dir = tmp_path / "origin.git"
    git_env = {**os.environ, **mcp_env}
    subprocess.run(["git", "init", "--bare", str(origin_dir)],
                   env=git_env, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project_dir), "remote", "add",
                    "origin", str(origin_dir)], env=git_env, check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(project_dir), "push", "-u",
                    "origin", "HEAD"], env=git_env, check=True,
                   capture_output=True)

    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"_meta": REQUEST_META, "name": "project.pull",
                               "arguments": {}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2,
                      "method": "tools/call",
                      "params": {"_meta": REQUEST_META, "name": "project.pull",
                                 "arguments": {}}}) + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0, result.stderr
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    # Requests on one connection run concurrently: either request may
    # win the exclusive lock and do the pull, leaving the other one
    # throttled
    results = [responses[1]["result"], responses[2]["result"]]
    pulled = [result for result in results if not result["isError"]]
    throttled = [result for result in results if result["isError"]]
    assert len(pulled) == 1, results
    assert len(throttled) == 1, results
    assert "Already up to date" in pulled[0]["content"][0]["text"], \
        results
    assert "retry in" in throttled[0]["content"][0]["text"], results


def test_pull_refreshes_the_project_via_stdio(run_debops, mcp_env,
                                              tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    project_dir = tmp_path / "example"
    run_debops("project", "init", "--no-requirements", str(project_dir),
               env=mcp_env, check=True)
    origin_dir = tmp_path / "origin.git"
    git_env = {**os.environ, **mcp_env,
               "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@x.org",
               "GIT_COMMITTER_NAME": "Test",
               "GIT_COMMITTER_EMAIL": "t@x.org"}

    def git(*args, cwd=None):
        subprocess.run(["git"] + list(args), env=git_env, check=True,
                       cwd=cwd, capture_output=True)

    git("init", "--bare", str(origin_dir))
    git("-C", str(project_dir), "remote", "add", "origin",
        str(origin_dir))
    git("-C", str(project_dir), "push", "-u", "origin", "HEAD")

    # The generated files are removed: the pull must regenerate them
    (project_dir / "ansible.cfg").unlink()
    shutil.rmtree(project_dir / "ansible" / "playbooks")

    # The origin advances with a configuration change
    clone = tmp_path / "clone"
    git("clone", str(origin_dir), str(clone))
    with open(str(clone / ".debops.cfg"), "a") as fp:
        fp.write("[ansible defaults]\nforce_color = true\n")
    git("-C", str(clone), "add", ".debops.cfg")
    git("-C", str(clone), "commit", "-q", "-m", "Configure color")
    git("-C", str(clone), "push", "-q")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "project.pull",
                   "arguments": {}}}) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0, result.stderr
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False, response["result"]
    assert "project refreshed" in text, response["result"]
    # ansible.cfg was regenerated and carries the pulled configuration
    ansible_cfg = (project_dir / "ansible.cfg").read_text()
    assert "force_color" in ansible_cfg
    # The skeleton directory is back
    assert (project_dir / "ansible" / "playbooks").is_dir()


def test_check_with_limit_resolves_hosts(run_debops, mcp_env, project_dir):
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    disable_confirm_gate(project_dir)

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from mcp
""")

    requests = (
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "tools/call",
                    "params": {"_meta": REQUEST_META, "name": "check",
                               "arguments": {"playbook": "custom",
                                             "limit": "local"}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2,
                      "method": "tools/call",
                      "params": {"_meta": REQUEST_META, "name": "check",
                                 "arguments": {"playbook": "custom",
                                               "limit": "nosuch"}}})
        + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    first = responses[1]["result"]["content"][0]["text"]
    assert "hosts: localhost" in first
    second = responses[2]["result"]
    assert second["isError"] is True
    assert "no hosts to target" in second["content"][0]["text"]


def test_policy_deny_hides_a_tool(run_debops, mcp_env, project_dir):
    conf_d = project_dir / ".debops" / "conf.d"
    conf_d.mkdir(parents=True)
    (conf_d / "mcp.yml").write_text(
            "---\nproject:\n  mcp:\n    deny: ['role.info']\n")

    request = (json.dumps({"jsonrpc": "2.0",
                           "id": 1, "method": "tools/list",
                           "params": {"_meta": REQUEST_META}}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "role.info" not in names
    assert "role.variables" in names


def test_connect_exits_quietly_on_sigint(debops_source_dir, mcp_env,
                                         tmp_path):
    socket_path = str(tmp_path / "server.sock")
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(McpServer(),),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
                [debops_source_dir,
                 env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
        env.update(mcp_env)
        process = subprocess.Popen(
                [sys.executable, "-m", "debops", "mcp", "connect",
                 socket_path],
                cwd=str(tmp_path), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)
            process.send_signal(signal.SIGINT)
            _stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()

        assert process.returncode == 130
        assert "Fatal Python error" not in stderr
        assert "Traceback" not in stderr
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_connect_to_missing_socket_fails(run_debops, mcp_env, tmp_path):
    result = run_debops("mcp", "connect", str(tmp_path / "missing.sock"),
                        env=mcp_env)

    assert result.returncode == 1
    assert "Error" in result.stderr


def test_connect_bridges_stdio_to_unix_socket(run_debops, mcp_env,
                                              project_dir, tmp_path):
    socket_path = str(tmp_path / "server.sock")
    transport = UnixSocketTransport(socket_path)
    thread = threading.Thread(target=transport.serve, args=(McpServer(),),
                              daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if os.path.exists(socket_path):
                break
            time.sleep(0.01)

        result = run_debops("mcp", "connect", socket_path,
                            cwd=str(project_dir), env=mcp_env,
                            input_text=make_discover_request())

        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        assert response["id"] == 1
        result = response["result"]
        assert result["supportedVersions"] == ["2026-07-28"]
        meta = result["_meta"]["io.modelcontextprotocol/serverInfo"]
        assert meta["name"] == "debops"
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_configure_harness(run_debops, mcp_env, project_dir):
    result = run_debops("mcp", "configure", "-H", "opencode",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "Created" in result.stdout

    config_path = project_dir / ".opencode" / "opencode.json"
    config = json.loads(config_path.read_text())
    entry = config["mcp"]["servers"]["debops"]
    assert entry["command"] == ["debops", "mcp", "serve"]
    assert entry["protocol"] == "2026-07-28"

    # A rerun notices there is nothing to do
    result = run_debops("mcp", "configure", "-H", "opencode",
                        cwd=str(project_dir), env=mcp_env)
    assert "Unchanged" in result.stdout


def test_configure_harness_refuses_a_non_project(run_debops, mcp_env,
                                                 tmp_path):
    result = run_debops("mcp", "configure", "-H", "claude",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 1
    assert "not found" in result.stderr


def test_configure_harness_user_mode_needs_no_project(run_debops,
                                                      mcp_env,
                                                      tmp_path):
    # From any directory, the account-level file (XDG_CONFIG_HOME in
    # the test environment) gets the spawn entry: the server serves a
    # project when the harness opens one, the documentation mode
    # otherwise
    result = run_debops("mcp", "configure", "-H", "opencode", "--user",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 0
    config_path = (tmp_path / "config" / "opencode" / "opencode.json")
    config = json.loads(config_path.read_text())
    entry = config["mcp"]["servers"]["debops"]
    assert entry["command"] == ["debops", "mcp", "serve"]


def test_configure_harness_rejects_the_removed_socket_option(
        run_debops, mcp_env, project_dir):
    # The bridge shape is configured by hand; the writer offers only
    # the spawn entry
    result = run_debops("mcp", "configure", "-H", "opencode",
                        "--socket", "/run/x.sock",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 2
    assert "--socket" in result.stderr


def test_configure_print_needs_no_project(run_debops, mcp_env,
                                          tmp_path):
    # Printing asks for no directory at all
    result = run_debops("mcp", "configure", "-H", "opencode", "--print",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 0
    assert "not written to .opencode/opencode.json" in result.stdout
    assert '"mcp"' in result.stdout
    assert "serve" in result.stdout
    # The removed advisory stays gone
    assert "--socket" not in result.stdout


def test_configure_fires_no_hooks(run_debops, mcp_env, project_dir):
    # Configuration writes are not hook events — in a project or (for
    # printing) outside of one
    marker = project_dir / "hook-fired"
    _hook_canary(project_dir / ".debops" / "hooks" / "mcp-pre-start",
                 marker)
    _hook_canary(Path(mcp_env["XDG_CONFIG_HOME"]) / "debops" / "hooks"
                 / "mcp-pre-start", marker)

    result = run_debops("mcp", "configure", "-H", "opencode",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    result = run_debops("mcp", "configure", "-H", "opencode", "--print",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    result = run_debops("mcp", "configure", "-H", "opencode", "--print",
                        cwd=str(project_dir.parent), env=mcp_env)
    assert result.returncode == 0
    assert not marker.exists()


def test_configure_agents_writes_the_guidance(run_debops, mcp_env,
                                              project_dir):
    # The harness-agnostic nudge, in the file every harness reads
    result = run_debops("mcp", "configure", "--agents",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "Created: AGENTS.md" in result.stdout
    content = (project_dir / "AGENTS.md").read_text()
    assert "## MCP server" in content
    assert "playbook.check" in content

    result = run_debops("mcp", "configure", "--agents",
                        cwd=str(project_dir), env=mcp_env)
    assert "Unchanged: AGENTS.md" in result.stdout

    # Without -H there is no harness file; with it, both
    result = run_debops("mcp", "configure", "-H", "opencode",
                        "--agents", cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert (project_dir / ".opencode" / "opencode.json").exists()

    # And a projectless print shows the section
    result = run_debops("mcp", "configure", "--agents", "--print",
                        cwd=str(project_dir.parent), env=mcp_env)
    assert result.returncode == 0
    assert "## MCP server" in result.stdout
