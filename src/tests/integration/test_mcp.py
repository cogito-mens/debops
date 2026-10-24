# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tests of the 'debops mcp' command line.

These tests drive the real ``python -m debops mcp`` entry point as a
subprocess, so they exercise the argument handling and the protocol
framing a user (or an MCP client) sees.
"""

from debops.mcp.client import socket_pid_path
from debops.mcp.server import McpServer
from debops.mcp.skills import SkillLibrary
from debops.mcp.tools import DOCS_STUB_TOOLS
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
def mcp_env(debops_env, tmp_path):
    """A hermetic environment for the debops subprocess.

    An ANSIBLE_CONFIG left in the ambient environment (for example by
    the shell the tests were started from) makes ProjectDir redirect
    the project path to its parent directory. Neutralize the variable
    so the subprocess uses the project directory it is given. The run
    logs go to the XDG cache directory, which is confined here too.
    """
    cache_home = tmp_path / "xdg-cache"
    cache_home.mkdir()
    return dict(debops_env, ANSIBLE_CONFIG="",
                XDG_CACHE_HOME=str(cache_home))


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


def test_serve_legacy_mode_speaks_the_2025_protocol(run_debops, mcp_env,
                                                    project_dir):
    """A 2025-era client (the Pi/Codex shape): the initialize handshake,
    no per-request _meta envelope, the old result shape."""
    messages = [
        # The handshake
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "probe", "version": "0.1"}}},
        # The client is ready
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
         "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "project.status", "arguments": {}}},
    ]
    input_text = "".join(json.dumps(message) + "\n" for message in messages)
    result = run_debops("mcp", "serve", "--legacy",
                        cwd=str(project_dir), env=mcp_env,
                        input_text=input_text)

    assert result.returncode == 0
    responses = read_json_lines(result.stdout)
    handshake, listing, call = responses
    assert handshake["result"]["protocolVersion"] == "2025-11-25"
    assert handshake["result"]["serverInfo"]["name"] == "debops"
    assert "instructions" in handshake["result"]
    assert "resultType" not in handshake["result"]
    # The 2025 shape: no resultType, no caching metadata anywhere
    assert "resultType" not in listing["result"]
    assert "ttlMs" not in listing["result"]
    assert any(tool["name"] == "project.status"
               for tool in listing["result"]["tools"])
    assert call["result"]["isError"] is False
    assert "resultType" not in call["result"]


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
    assert names == sorted(
            ["ansible.doc", "collection.playbooks",
             "collection.roles",
             "playbook.info", "playbook.list",
             "role.argspec", "role.defaults", "role.files",
             "role.handlers", "role.info", "role.tasks",
             "role.templates", "role.vars"]
            + list(DOCS_STUB_TOOLS))


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
    assert "role.info" in names
    # The project tools stay visible as stubs whose answer names the
    # mode
    assert "project.read" in names
    stub_call = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "project.read",
                   "arguments": {"path": "ansible.cfg"}},
    }) + "\n"
    result = run_debops("mcp", "serve", "--docs", cwd=str(project_dir),
                        env=mcp_env, input_text=stub_call)
    (response,) = read_json_lines(result.stdout)
    assert response["result"]["isError"] is True
    assert "needs a project directory" in \
        response["result"]["content"][0]["text"]


def test_serve_docs_rejects_view(run_debops, mcp_env, project_dir):
    result = run_debops("mcp", "serve", "--docs", "--view", "web",
                        cwd=str(project_dir), env=mcp_env, input_text="")

    assert result.returncode == 2
    assert "--docs" in result.stderr


def _ansible_doc_stub(home):
    """A stub ansible-doc in the account's user bin, which reports the
    environment it was called with."""
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    stub = user_bin / "ansible-doc"
    stub.write_text("#!/bin/sh\n"
                    "printf '%s\\n' \"stub says: "
                    "$ANSIBLE_COLLECTIONS_PATH\"\n")
    stub.chmod(0o755)
    return stub


def _account_env(mcp_env, debops_source_dir, home):
    """A server environment with a staged HOME.

    The fake home hides the account's user site-packages from the
    debops subprocess (its dependencies may live there), so the real
    one rides the PYTHONPATH explicitly.
    """
    import site
    return dict(mcp_env, HOME=str(home),
                PYTHONPATH=os.pathsep.join(
                        [debops_source_dir, site.getusersitepackages()]))


def test_docs_mode_consults_the_accounts_ansible_doc(
        run_debops, mcp_env, project_dir, tmp_path, monkeypatch,
        debops_source_dir):
    """Without an operator-chosen ANSIBLE_COLLECTIONS_PATH the tool
    runs the account's own ansible-doc (a pipx user install lives at
    ~/.local/bin), serving the account's collection roots next to the
    bundled one."""
    monkeypatch.delenv("ANSIBLE_COLLECTIONS_PATH", raising=False)
    home = tmp_path / "home"
    stub = _ansible_doc_stub(home)
    env = _account_env(mcp_env, debops_source_dir, home)
    log_path = tmp_path / "server.log"

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "ansible.doc",
                   "arguments": {"plugin": "ansible.builtin.service"}},
    }) + "\n"
    result = run_debops("mcp", "serve", "--docs", "--log",
                        str(log_path), cwd=str(project_dir),
                        env=env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert response["result"].get("isError", False) is False
    # The stub ran, not any system ansible-doc; the presentation
    # masks the account's home directory in the output
    assert text.startswith("stub says: ")
    assert "~/.ansible/collections" in text
    assert str(home) not in text
    assert "/usr/share/ansible/collections" in text
    # The resolution and the roots are in the log for debugging
    log_content = log_path.read_text()
    assert "ansible-doc: {}".format(stub) in log_content
    assert "the account defaults and the bundled collection" \
        in log_content


def test_docs_mode_relies_on_an_operator_chosen_path(
        run_debops, mcp_env, project_dir, tmp_path, debops_source_dir):
    """With ANSIBLE_COLLECTIONS_PATH defined the variable rules the
    lookup; the account roots are not added."""
    home = tmp_path / "home"
    stub = _ansible_doc_stub(home)
    env = _account_env(mcp_env, debops_source_dir, home)
    env["ANSIBLE_COLLECTIONS_PATH"] = "/srv/operator/collections"
    log_path = tmp_path / "server.log"

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "ansible.doc",
                   "arguments": {"plugin": "ansible.builtin.service"}},
    }) + "\n"
    result = run_debops("mcp", "serve", "--docs", "--log",
                        str(log_path), cwd=str(project_dir),
                        env=env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    # The variable rules the lookup; the bundled collection rides last
    from debops.mcp.tools.collections import BUNDLED_COLLECTIONS_ROOT
    assert text.rstrip() == "stub says: " + os.pathsep.join(
            ["/srv/operator/collections", BUNDLED_COLLECTIONS_ROOT])
    # The log names the binary and the operator-chosen origin
    log_content = log_path.read_text()
    assert "ansible-doc: {}".format(stub) in log_content
    assert "from the environment" in log_content


def test_docs_mode_tools_agree_on_the_account_collections(
        run_debops, mcp_env, project_dir, tmp_path, monkeypatch,
        debops_source_dir):
    """The parity of the documentation mode: with no operator-chosen
    ANSIBLE_COLLECTIONS_PATH the ansible.doc, collection.* and role.*
    tools all serve the account's own collections."""
    from debops.mcp.tools.collections import BUNDLED_COLLECTIONS_ROOT
    monkeypatch.delenv("ANSIBLE_COLLECTIONS_PATH", raising=False)
    home = tmp_path / "home"
    account_root = home / ".ansible" / "collections"

    # The account's collection: one role with one default
    role = (account_root / "ansible_collections" / "fake" / "ns"
            / "roles" / "myrole")
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text("myrole__answer: 42\n")

    # The stubs: the discovery, the doc lookup and the galaxy index
    # of the account's ansible installation
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    (user_bin / "ansible-config").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"COLLECTIONS_PATHS(default) = "
            "['{}', '/usr/share/ansible/collections']\"\n".format(
                    account_root))
    (user_bin / "ansible-doc").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"stub says: "
            "$ANSIBLE_COLLECTIONS_PATH\"\n")
    galaxy_json = json.dumps(
            {str(account_root / "ansible_collections"):
             {"fake.ns": {"version": "1.0.0"}}})
    (user_bin / "ansible-galaxy").write_text(
            "#!/bin/sh\nprintf '%s\\n' '{}'\n".format(galaxy_json))
    for stub in ("ansible-config", "ansible-doc", "ansible-galaxy"):
        (user_bin / stub).chmod(0o755)
    env = _account_env(mcp_env, debops_source_dir, home)

    def request(request_id, name, arguments):
        return json.dumps({
            "jsonrpc": "2.0", "id": request_id,
            "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": name,
                       "arguments": arguments}}) + "\n"

    requests = (
            request(1, "ansible.doc",
                    {"plugin": "ansible.builtin.service"})
            + request(2, "collection.roles", {})
            + request(3, "role.defaults", {"name": "fake.ns.myrole"})
            + request(4, "role.defaults", {"name": "myrole"}))
    result = run_debops("mcp", "serve", "--docs", cwd=str(project_dir),
                        env=env, input_text=requests)

    assert result.returncode == 0
    by_id = {}
    for response in read_json_lines(result.stdout):
        if "result" in response:
            by_id[response["id"]] = response["result"]

    # ansible.doc got the account root and the bundled collection;
    # the presentation masks the account's home directory
    assert by_id[1].get("isError", False) is False
    text = by_id[1]["content"][0]["text"]
    assert "~/.ansible/collections" in text
    assert str(home) not in text
    assert BUNDLED_COLLECTIONS_ROOT in text

    # The collection index lists the account's collection
    assert by_id[2].get("isError", False) is False
    text = by_id[2]["content"][0]["text"]
    assert "fake.ns" in text
    assert "myrole" in text

    # The role tools resolve the account's role, by the fully
    # qualified name (the index) and by the short one (the roles
    # directories of the roots)
    for request_id in (3, 4):
        assert by_id[request_id].get("isError", False) is False, \
            by_id[request_id]["content"][0]["text"]
        assert "myrole__answer" in \
            by_id[request_id]["content"][0]["text"]


def test_serve_log_file(run_debops, mcp_env, project_dir):
    # A harness swallows the server's stderr; the audit log is the file
    log_path = project_dir.parent / "server.log"
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "project.status",
                   "arguments": {}},
    }) + "\n"
    result = run_debops("mcp", "serve", "--log", str(log_path),
                        cwd=str(project_dir), env=mcp_env,
                        input_text=request)
    assert result.returncode == 0
    content = log_path.read_text()
    assert "Serving project" in content
    assert "Tool project.status() -> ok" in content
    # A tool error is an audit line, not silence
    request = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"_meta": REQUEST_META, "name": "project.read",
                   "arguments": {"path": "nosuch.yml"}},
    }) + "\n"
    result = run_debops("mcp", "serve", "--log", str(log_path),
                        cwd=str(project_dir), env=mcp_env,
                        input_text=request)
    assert "Tool project.read(path) -> error" in log_path.read_text()


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
        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        run_env["PYTHONPATH"] = os.pathsep.join(
                [source_dir, run_env.get("PYTHONPATH", "")]).rstrip(
                        os.pathsep)
        self.process = subprocess.Popen(
                [sys.executable, "-m", "debops", "mcp", "serve",
                 *serve_args],
                cwd=str(project_dir), env=run_env, stdin=subprocess.PIPE,
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
    assert sorted(names) == ["ansible.doc", "ansible.facts",
                             "ansible.ping", "batch.create",
                             "batch.info", "batch.pending",
                             "batch.prune", "batch.remove", "check",
                             "collection.playbooks", "collection.remove",
                             "collection.require", "collection.roles",
                             "collection.sync", "config.get",
                             "environment.list",
                             "environment.set", "environment.unset",
                             "execution.list", "execution.output",
                             "execution.purge",
                             "file.delete", "file.move", "file.write",
                             "inventory.query",
                             "inventory.spec", "job.add", "job.del",
                             "job.prune", "keyring.fetch",
                             "playbook.info",
                             "playbook.list", "project.files",
                             "project.list", "project.mkdir",
                             "project.pull", "project.push", "project.read",
                             "project.status", "queue.clear",
                             "queue.list", "queue.start", "queue.stop",
                             "role.argspec",
                             "role.defaults", "role.files",
                             "role.handlers", "role.info", "role.tasks",
                             "role.templates", "role.vars",
                             "run", "secret.paths", "view.create",
                             "view.list", "view.switch"]


def test_tools_list_without_the_queue(run_debops, mcp_env,
                                      project_dir):
    """A spawned daemon carries no queue; --no-queue pins the same
    for a hand-started one."""
    request = (json.dumps({"jsonrpc": "2.0",
                           "id": 1, "method": "tools/list",
                           "params": {"_meta": REQUEST_META}}) + "\n")
    result = run_debops("mcp", "serve", "--no-queue",
                        cwd=str(project_dir), env=mcp_env,
                        input_text=request)
    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "queue.list" not in names
    assert "job.add" not in names
    assert "batch.create" not in names


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


def test_check_with_a_user_override(run_debops, mcp_env, project_dir):
    """The bootstrap escape hatch: the policy names the playbooks and
    the usernames; the local connection ignores the user, so the
    plumbing is what this proves."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\n"
            "confirm_required = false\n"
            "execute_user_override = { \"bootstrap*\" = \"root\" }\n")

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "bootstrap-test.yml").write_text("""---
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from mcp
""")
    (playbooks / "unrelated.yml").write_text(
            (playbooks / "bootstrap-test.yml").read_text())

    requests = "".join(json.dumps(request) + "\n" for request in (
        {"jsonrpc": "2.0", "id": 1,
         "method": "tools/call",
         "params": {"_meta": REQUEST_META, "name": "check",
                    "arguments": {"playbook": "bootstrap-test",
                                  "user": "root"}}},
        # A playbook outside the table refuses the override
        {"jsonrpc": "2.0", "id": 2,
         "method": "tools/call",
         "params": {"_meta": REQUEST_META, "name": "check",
                    "arguments": {"playbook": "unrelated",
                                  "user": "root"}}},
    ))
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    # The requests run concurrently: the refusal answers at once, the
    # check run when the playbook finishes — match by request id
    by_id = {}
    for response in read_json_lines(result.stdout):
        by_id[response.get('id')] = response

    allowed = by_id[1]["result"]
    assert allowed["isError"] is False
    text = allowed["content"][0]["text"]
    assert "exit code: 0" in text
    assert "user: root (policy override)" in text
    assert allowed["structuredContent"]["user"] == "root"

    refused = by_id[2]["result"]
    assert refused["isError"] is True
    assert "execute_user_override" in refused["content"][0]["text"]


def test_detached_run_survives_the_death_of_the_server(
        run_debops, mcp_env, project_dir, tmp_path, debops_source_dir):
    """The harness case: the client (or its session) dies mid-run.

    A stdio server is a child of its client; when the harness goes
    away it shoots the whole process group. The playbook is detached
    from the server, so it completes anyway and its run log records
    the outcome.
    """
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\nexecute = \"all\"\n")

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Slow play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Take some time
      ansible.builtin.command: sleep 5
    - name: Report the survival
      ansible.builtin.debug:
        msg: survived the harness
""")

    # The server is its own session leader, as when a harness spawns
    # it; killing the group then models the harness dying
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir, env.get("PYTHONPATH", "")]).rstrip(
                    os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve"],
            cwd=str(project_dir), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            start_new_session=True)
    try:
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": "run",
                       "arguments": {"playbook": "custom"}},
        }) + "\n"
        server.stdin.write(request)
        server.stdin.flush()

        # Wait for the run to start: its log file appears and grows
        runs_dir = (tmp_path / "xdg-cache" / "debops" / "mcp" / "runs")
        log = None
        for _ in range(400):
            if log is None or not log.exists():
                logs = sorted(runs_dir.glob("*/execution-*.log"),
                              key=lambda path: path.stat().st_mtime) \
                        if runs_dir.exists() else []
                if logs:
                    log = logs[-1]
            if log is not None and log.exists() \
                    and log.stat().st_size > 0:
                break
            time.sleep(0.05)
        assert log is not None and log.exists() and \
            log.stat().st_size > 0, "the run log never appeared"

        # The harness is shot: the channel closes and the whole
        # process group is killed, before the playbook has finished
        server.stdin.close()
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=10)
        assert server.returncode == -signal.SIGKILL

        # The detached run completes anyway, and the log is the record
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            text = log.read_text()
            if "debops-mcp: the execution finished" in text:
                break
            time.sleep(0.2)
        else:
            pytest.fail("the detached run never finalized; the log:\n"
                        + log.read_text())
        text = log.read_text()
        assert "survived the harness" in text
        assert "PLAY RECAP" in text
        # The finalization ran: the project was committed and locked
        # again by the wrapper, in place of the dead server
        assert "the finalize failed" not in text
    finally:
        try:
            os.killpg(server.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        server.wait(timeout=10)


def test_an_orphaned_run_keeps_the_project_locked(
        run_debops, mcp_env, project_dir, tmp_path, debops_source_dir):
    """A run which outlived its server still holds the project lock:
    a new server must refuse to mutate the project — collections,
    environment, files — until the orphaned run has finished."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\nexecute = \"all\"\n")
    # The write tools require a git-tracked project
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir),
                   env=dict(os.environ, **mcp_env), check=True)

    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Slow play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Take some time
      ansible.builtin.command: sleep 5
""")

    # Server A starts a run and is then killed with its whole process
    # group (the harness scenario); the detached run survives
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir, env.get("PYTHONPATH", "")]).rstrip(
                    os.pathsep)
    env.update(mcp_env)
    server_a = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve"],
            cwd=str(project_dir), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            start_new_session=True)
    log = None
    try:
        request = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": "run",
                       "arguments": {"playbook": "custom"}},
        }) + "\n"
        server_a.stdin.write(request)
        server_a.stdin.flush()

        runs_dir = tmp_path / "xdg-cache" / "debops" / "mcp" / "runs"
        for _ in range(400):
            logs = list(runs_dir.glob("*/execution-1.log"))
            if logs and logs[0].stat().st_size > 0:
                log = logs[0]
                break
            time.sleep(0.05)
        assert log is not None, "the run log never appeared"

        os.killpg(server_a.pid, signal.SIGKILL)
        server_a.wait(timeout=10)

        # A new server on the same project: the mutation tools refuse
        # while the orphaned run holds the lock
        server_b = _LiveServer(project_dir, mcp_env, debops_source_dir)
        try:
            refused = server_b.call("environment.set",
                                    {"name": "MCP_TEST", "value": "1"})
            assert refused["result"]["isError"] is True
            assert "another process is working on this project" in \
                refused["result"]["content"][0]["text"]

            # The listing reports the run without an outcome yet
            listing = server_b.call("execution.list", {})
            (entry,) = listing["result"]["structuredContent"]
            assert entry["id"] == 1
            assert "exitCode" not in entry

            # The orphan finishes; the lock releases with its finalize
            deadline = time.monotonic() + 90
            while "debops-mcp: the execution finished" \
                    not in log.read_text():
                assert time.monotonic() < deadline, \
                    "the orphaned run never finalized"
                time.sleep(0.2)

            # Now the mutation goes through (the first attempt may race
            # the wrapper's exit by a moment; poll)
            deadline = time.monotonic() + 15
            while True:
                allowed = server_b.call(
                        "environment.set",
                        {"name": "MCP_TEST", "value": "1"})
                if not allowed["result"]["isError"]:
                    break
                assert time.monotonic() < deadline, \
                    allowed["result"]["content"][0]["text"]
                time.sleep(0.2)

            # The execution ids span the two server processes
            later = server_b.call("check", {"playbook": "custom"})
            assert later["result"]["isError"] is False
            assert later["result"]["structuredContent"][
                "executionId"] == 2
        finally:
            server_b.stop()
    finally:
        try:
            os.killpg(server_a.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        server_a.wait(timeout=10)


def test_check_verifies_a_playbook_without_running_it(
        run_debops, mcp_env, project_dir, debops_source_dir):
    """The write→verify→fix loop: the model checks a playbook it wrote
    through ansible-playbook itself, inside the confinement — instead
    of running ansible-playbook locally, off the record."""
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
    (playbooks / "broken.yml").write_text(
            "- name: Broken\n  hosts: localhost\n  tasks: nope\n")
    (playbooks / "good.yml").write_text("""---
- name: Good play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Works
      ansible.builtin.debug:
        msg: fine
""")

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        # The broken playbook answers with its parse error
        result = server.call("check", {"playbook": "broken",
                                       "syntax_check": True})
        assert result["result"]["isError"] is True
        assert "ERROR" in result["result"]["content"][0]["text"]

        # The good one parses clean, and lists its tasks without
        # running anything
        result = server.call("check", {"playbook": "good",
                                       "syntax_check": True})
        assert result["result"]["isError"] is False
        assert "exit code: 0" in result["result"]["content"][0]["text"]
        result = server.call("check", {"playbook": "good",
                                       "list_tasks": True})
        assert result["result"]["isError"] is False
        assert "Works" in result["result"]["content"][0]["text"]
    finally:
        server.stop()


def test_execution_log_holds_exactly_one_execution(
        run_debops, mcp_env, project_dir, debops_source_dir):
    """A playbook list runs each playbook as its own invocation; the
    log aggregates their output once each — never duplicated, never
    reused by a later run."""
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
    - name: Task of {0}
      ansible.builtin.debug:
        msg: "hello from {0}"
""".format(name))

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        result = server.call("check", {"playbook": ["first", "second"]})
        assert result["result"]["isError"] is False
        log = result["result"]["structuredContent"]["log"]
        with open(log) as fp:
            content = fp.read()
        lines = content.splitlines()

        def count(prefix):
            return sum(1 for line in lines if line.startswith(prefix))

        # Two invocations, one recap each, one completion marker
        assert count("PLAY RECAP ") == 2
        assert count("PLAY [") == 2
        assert count("TASK [") == 2
        assert count("debops-mcp: the execution finished") == 1
        # No section appears twice
        assert content.count("hello from first") == 1
        assert content.count("hello from second") == 1

        # The canned recap query returns both invocation blocks
        answer = server.call("execution.output",
                             {"id": 1, "kind": "recap"})
        recaps = [line for line in
                  answer["result"]["structuredContent"]["lines"]
                  if line.startswith("PLAY RECAP ")]
        assert len(recaps) == 2
    finally:
        server.stop()


def test_execution_output_answers_what_changed(run_debops, mcp_env,
                                               project_dir,
                                               debops_source_dir):
    """The drill-down after a run: which tasks changed, on which hosts
    — without re-executing anything."""
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
    - name: Pretend to change something
      ansible.builtin.command: /bin/true
      check_mode: false
      changed_when: true
""")

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        result = server.call("check", {"playbook": "custom"})
        assert result["result"]["isError"] is False, \
            result["result"]["content"][0]["text"]
        text = result["result"]["content"][0]["text"]
        assert "exit code: 0" in text
        execution_id = result["result"]["structuredContent"][
            "executionId"]

        # The drill-down query: the changed tasks with their hosts
        answer = server.call("execution.output",
                             {"id": execution_id, "kind": "changed"})
        assert answer["result"]["isError"] is False
        text = answer["result"]["content"][0]["text"]
        assert "Pretend to change something" in text
        assert "changed: [localhost]" in text
        assert "Say hello" not in text
        # A harness which shows only the structured content still gets
        # the lines
        lines = answer["result"]["structuredContent"]["lines"]
        assert any("Pretend to change something" in line
                   for line in lines)
        assert any(line == "changed: [localhost]" for line in lines)

        # The listing addresses the run afterwards
        listing = server.call("execution.list", {})
        assert "result" in listing, json.dumps(listing)[:400]
        (entry,) = listing["result"]["structuredContent"]
        assert entry["id"] == execution_id
        assert entry["mode"] == "check"
        assert entry["exitCode"] == 0
    finally:
        server.stop()


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


def test_socket_mode_masks_the_server_paths(
        run_debops, mcp_env, tmp_path, debops_source_dir):
    """Over a socket the server may live outside the harness' reach:
    the client-facing paths mask the server account's home."""
    home = tmp_path / "home"
    project = home / "project"
    (project / "ansible" / "inventory").mkdir(parents=True)
    (project / ".debops.cfg").touch()
    (project / "ansible" / "inventory" / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    socket_path = str(tmp_path / "serve.sock")
    env = _account_env(mcp_env, debops_source_dir, home)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path],
            cwd=str(project), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def call(method, params, request_id):
        import socket as socket_module
        connection = socket_module.socket(socket_module.AF_UNIX,
                                          socket_module.SOCK_STREAM)
        connection.connect(socket_path)
        connection.sendall((json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "method": method,
                "params": dict(params or {}, _meta=REQUEST_META)})
                + "\n").encode())
        reader = connection.makefile("rb")
        response = json.loads(reader.readline())
        connection.close()
        return response

    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                response = call("tools/call",
                                {"name": "project.status",
                                 "arguments": {}}, 1)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        result = response["result"]
        structured = result["structuredContent"]
        # The project lives under the server account's home; the
        # client sees the masked form
        assert structured["path"] == "~/project"
        assert structured["viewRoot"] == "~/project/ansible"
        assert str(home) not in result["content"][0]["text"]
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
        result = run_debops("mcp", "inspect", "--socket", socket_path, env=mcp_env)
        assert result.returncode == 0
        assert "name: debops" in result.stdout
        assert "instructions:" in result.stdout
        assert "capabilities:" in result.stdout

        result = run_debops("mcp", "inspect", "--socket", socket_path, "--tools",
                            env=mcp_env)
        assert result.returncode == 0
        assert "tools (1):" in result.stdout
        assert "mop" in result.stdout
        assert "[non-destructive, idempotent]" in result.stdout

        result = run_debops("mcp", "inspect", "--socket", socket_path,
                            "--tool", "mop", env=mcp_env)
        assert result.returncode == 0
        assert "mop — Mop the floors." in result.stdout
        assert "bucket, required" in result.stdout

        result = run_debops("mcp", "inspect", "--socket", socket_path, "--resources",
                            env=mcp_env)
        assert result.returncode == 0
        assert "debops-cli" in result.stdout

        # The prompts stub answers gracefully
        result = run_debops("mcp", "inspect", "--socket", socket_path, "--prompts",
                            env=mcp_env)
        assert result.returncode == 0
        assert "does not support" in result.stdout

        # Raw responses are one JSON line per selected step
        result = run_debops("mcp", "inspect", "--socket", socket_path, "--raw",
                            "--discover", "--tools", env=mcp_env)
        assert result.returncode == 0
        first, second = read_json_lines(result.stdout)
        assert first["result"]["supportedVersions"] == ["2026-07-28"]
        assert second["result"]["tools"][0]["name"] == "mop"
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_inspect_finds_the_projects_daemon(run_debops, mcp_env,
                                           project_dir, tmp_path):
    """A bare 'mcp inspect' resolves the well-known socket of the
    project and spawns the daemon when it is missing — the same
    daemon a repl would attach to."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    try:
        result = run_debops("mcp", "inspect", cwd=str(project_dir),
                            env=env)
        assert result.returncode == 0
        assert "name: debops" in result.stdout
        assert "Spawning the MCP daemon" in result.stderr
        # A second inspection connects; nothing is spawned twice
        result = run_debops("mcp", "inspect", "--tools",
                            cwd=str(project_dir), env=env)
        assert result.returncode == 0
        assert "project.read" in result.stdout
        assert "Spawning" not in result.stderr
        sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
        assert len(sockets) == 1
    finally:
        run_debops("mcp", "stop", env=env, cwd=str(project_dir))


def test_inspect_no_spawn_refuses(run_debops, mcp_env, project_dir,
                                  tmp_path):
    """--no-spawn: a missing daemon is a clean refusal, naming the
    socket it looked at."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "inspect", "--no-spawn",
                        cwd=str(project_dir), env=env)
    assert result.returncode == 1
    assert "no MCP daemon is running" in result.stderr


def test_inspect_explicit_socket_never_spawns(run_debops, mcp_env,
                                              tmp_path):
    """An explicit socket is connect-only: no daemon there is an
    error, nothing is spawned."""
    result = run_debops("mcp", "inspect", "--socket",
                        str(tmp_path / "nosuch.sock"), env=mcp_env)
    assert result.returncode == 1
    assert "no MCP daemon is running" in result.stderr
    assert not (tmp_path / "nosuch.sock").exists()


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
    assert "role.vars" in names


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


def test_configure_harness_zed(run_debops, mcp_env, project_dir):
    # The legacy-era harnesses write their own file and key path
    result = run_debops("mcp", "configure", "-H", "zed",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "legacy support only" in result.stdout
    config = json.loads(
            (project_dir / ".zed" / "settings.json").read_text())
    entry = config["context_servers"]["debops"]
    assert entry["command"] == "debops"
    assert entry["args"] == ["mcp", "serve", "--legacy"]


def test_configure_goose_prints_for_a_hand_merge(run_debops, mcp_env,
                                                 project_dir):
    # No writable file is known for Goose: the snippet prints
    before = set(os.listdir(str(project_dir)))
    result = run_debops("mcp", "configure", "-H", "goose",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "to add by hand" in result.stdout
    assert "cmd: debops" in result.stdout
    assert '["mcp", "serve", "--legacy"]' in result.stdout
    # Nothing was written
    assert set(os.listdir(str(project_dir))) == before


def test_configure_amp_prints_the_cli_command(run_debops, mcp_env,
                                              project_dir):
    result = run_debops("mcp", "configure", "-H", "amp",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "amp mcp add debops -- debops mcp serve --legacy" \
        in result.stdout


def test_configure_help_names_the_print_only_harnesses(run_debops,
                                                       mcp_env,
                                                       project_dir):
    result = run_debops("mcp", "configure", "--help",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "goose: (print only)" in result.stdout
    assert "zed: .zed" in result.stdout


def test_configure_harness_docs_outside_a_project(run_debops, mcp_env,
                                                  tmp_path):
    # The documentation mode serves content without a project; the
    # harness file lands in the current directory
    result = run_debops("mcp", "configure", "-H", "opencode", "--docs",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 0
    assert "Created" in result.stdout
    config_path = tmp_path / ".opencode" / "opencode.json"
    config = json.loads(config_path.read_text())
    entry = config["mcp"]["servers"]["debops"]
    assert entry["command"] == ["debops", "mcp", "serve", "--docs"]


def test_configure_harness_docs_inside_a_project(run_debops, mcp_env,
                                                 project_dir):
    # The enforcement holds where the launcher would otherwise serve
    # the project
    result = run_debops("mcp", "configure", "-H", "opencode", "--docs",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    config_path = project_dir / ".opencode" / "opencode.json"
    config = json.loads(config_path.read_text())
    entry = config["mcp"]["servers"]["debops"]
    assert entry["command"] == ["debops", "mcp", "serve", "--docs"]


def test_configure_docs_refuses_a_bridge(run_debops, mcp_env,
                                         project_dir):
    # A bridge takes the daemon's mode; the enforcement flag must not
    # silently drop
    result = run_debops("mcp", "configure", "-H", "opencode",
                        "--docs", "--connect", "/run/x.sock",
                        "--name", "prod",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 2
    assert "--docs" in result.stderr


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


def test_help_wraps_at_a_comfortable_width(run_debops, mcp_env,
                                           project_dir):
    # A wide terminal no longer sprawls the option help
    result = run_debops("mcp", "configure", "--help",
                        cwd=str(project_dir),
                        env=dict(mcp_env, COLUMNS="200"))
    assert result.returncode == 0
    assert all(len(line) <= 78 for line in result.stdout.splitlines())
    assert "--docs" in result.stdout


def _serve_daemon(run_args, project_dir, socket_path, env):
    """A daemon for the REPL tests; the caller terminates it."""
    server = subprocess.Popen(run_args, cwd=str(project_dir), env=env,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    import socket as socket_module
    deadline = time.monotonic() + 15
    while True:
        try:
            probe = socket_module.socket(socket_module.AF_UNIX,
                                         socket_module.SOCK_STREAM)
            probe.connect(socket_path)
            probe.close()
            return server
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() > deadline:
                server.kill()
                raise RuntimeError("the daemon did not come up")
            probe.close()
            time.sleep(0.05)


def test_repl_over_a_pipe_against_a_daemon(run_debops, mcp_env,
                                           project_dir, tmp_path,
                                           debops_source_dir):
    """The scripted REPL: commands on stdin, answers on stdout."""
    socket_path = str(tmp_path / "repl.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="status\nexit\n", capture_output=True, text=True,
                timeout=60)
        assert result.returncode == 0
        assert "Project:" in result.stdout
        # The exit status notes a failing command
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="read ../outside\n", capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 1
        assert "error:" in result.stderr
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_command_form_and_login_shell(mcp_env, project_dir, tmp_path,
                                      debops_source_dir,
                                      tmp_path_factory):
    """The '-c' form runs the commands and exits; invoked under a
    login-shell name from a foreign directory, the project comes
    from the ambient 'mcp.login.project' knob."""
    socket_path = str(tmp_path / "login.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    # A directory which is not the project, and a launcher script
    # carrying the login-shell name in its argv[0]
    outside = tmp_path_factory.mktemp("outside")
    launcher = outside / "debops-shell"
    launcher.write_text(
            "#!{}\n"
            "import sys\n"
            "sys.path.insert(0, {!r})\n"
            "from debops.__main__ import run\n"
            "run()\n".format(sys.executable, str(debops_source_dir)))
    launcher.chmod(0o755)
    try:
        # The '-c' form of a directly invoked front end
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path, "-c", "status"],
                cwd=str(project_dir), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 0
        assert "Project:" in result.stdout
        # ... and a failing command notes itself in the exit status
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path, "-c", "read ../outside"],
                cwd=str(project_dir), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 1
        assert "error:" in result.stderr

        # The login shell from a foreign directory: no project to
        # probe, no knob yet
        result = subprocess.run(
                [str(launcher), "--socket", socket_path,
                 "-c", "status"],
                cwd=str(outside), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 1
        assert "Error" in result.stderr

        # The ambient knob points the login shell at the project
        config_dir = tmp_path / "config" / "debops" / "conf.d"
        config_dir.mkdir(parents=True)
        (config_dir / "00-login.toml").write_text(
                "[mcp.login]\nproject = '{}'\n".format(project_dir))
        result = subprocess.run(
                [str(launcher), "--socket", socket_path,
                 "-c", "status"],
                cwd=str(outside), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 0
        assert "Project:" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_the_confined_login_shell(mcp_env, project_dir, tmp_path,
                                  debops_source_dir):
    """The ambient 'mcp.login.confined' knob closes the escape
    hatches of the account's sessions — the editor verb — on the
    front end and the daemon alike."""
    socket_path = str(tmp_path / "confined.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # Unconfined: the refusal is the terminal check, not the
        # confinement
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "shell",
                 "--socket", socket_path, "-c", "edit inventory"],
                cwd=str(project_dir), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 1
        assert "edit needs a terminal" in result.stderr

        # The ambient knob confines the account
        config_dir = tmp_path / "config" / "debops" / "conf.d"
        config_dir.mkdir(parents=True)
        (config_dir / "00-login.toml").write_text(
                "[mcp.login]\nconfined = true\n")
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "shell",
                 "--socket", socket_path, "-c", "edit inventory"],
                cwd=str(project_dir), env=env, capture_output=True,
                text=True, timeout=60)
        assert result.returncode == 1
        assert "a confined session has no editor" in result.stderr
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_bare_debops_autostarts_the_configured_front_end(
        run_debops, mcp_env, project_dir, tmp_path):
    """A bare 'debops' in a project with 'project.autostart' starts
    the configured front end; the autospawn carries the filtered
    daemon options of the project."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    # Unconfigured: the bare invocation is the help it always was
    result = run_debops(env=env, cwd=str(project_dir))
    assert result.returncode == 2
    assert "mcp" in result.stderr

    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True)
    (confd / "00-autostart.toml").write_text(
            "[project]\n"
            'autostart = "shell"\n'
            "autospawn = true\n"
            "[project.autospawn_options]\n"
            'socket_mode = "0600"\n'
            "spawn = false\n"
            'socket = "/tmp/debops-test-evil.sock"\n')
    try:
        result = run_debops(env=env, cwd=str(project_dir),
                            input_text="status\n")
        assert result.returncode == 0
        assert "Project:" in result.stdout
        # The filtered keys are noted, and none of them took effect
        assert "dropped" in result.stderr
        assert not os.path.exists("/tmp/debops-test-evil.sock")
        sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
        assert len(sockets) == 1
        assert sockets[0].stat().st_mode & 0o777 == 0o600
    finally:
        run_debops("mcp", "stop", env=env, cwd=str(project_dir))


def test_bare_debops_autospawn_false_never_spawns(run_debops, mcp_env,
                                                  project_dir,
                                                  tmp_path):
    """'project.autospawn = false': no daemon, no spawn — a clean
    refusal."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True)
    (confd / "00-autostart.toml").write_text(
            "[project]\n"
            'autostart = "repl"\n'
            "autospawn = false\n")
    result = run_debops(env=env, cwd=str(project_dir),
                        input_text="status\n")
    assert result.returncode == 1
    assert "no MCP daemon is running" in result.stderr
    assert not list((runtime / "debops" / "mcp").glob("*.sock"))


def test_bare_debops_autospawn_legacy_drives_both_sides(
        run_debops, mcp_env, project_dir, tmp_path):
    """'legacy' in the spawn options drives the daemon and the
    client alike — a mismatch would refuse the connection."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True)
    (confd / "00-autostart.toml").write_text(
            "[project]\n"
            'autostart = "repl"\n'
            "[project.autospawn_options]\n"
            "legacy = true\n")
    try:
        result = run_debops(env=env, cwd=str(project_dir),
                            input_text="status\n")
        assert result.returncode == 0
        assert "Project:" in result.stdout
        # The daemon really speaks the old era: a current client is
        # refused
        result = run_debops("mcp", "repl", env=env,
                            cwd=str(project_dir), input_text="status\n")
        assert result.returncode == 1
        assert "2025-era" in result.stderr
    finally:
        run_debops("mcp", "stop", env=env, cwd=str(project_dir))


def test_a_peer_client_is_guarded_and_follows(run_debops, mcp_env,
                                              tmp_path,
                                              debops_source_dir):
    """The review scenario end to end: client A switches the view
    while client B is attached; B's mutation on the stale view is
    refused, the refusal moves B, and the retry lands."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    project_dir = tmp_path / "example"
    run_debops("project", "init", "-t", "modern", "--no-requirements",
               str(project_dir), env=mcp_env, check=True)
    run_debops("project", "mkview", "webservers",
               cwd=str(project_dir), env=mcp_env, check=True)
    socket_path = str(tmp_path / "view.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # Client B attaches while the daemon sits on the default view
        peer = subprocess.Popen(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
        try:
            # The status answer proves the session is live and current
            peer.stdin.write("status\n")
            peer.stdin.flush()
            for _ in range(20):
                if "Project:" in peer.stdout.readline():
                    break
            else:
                pytest.fail("the peer never answered")

            # Client A moves the daemon's view under B's feet
            moved = run_debops(
                    "mcp", "repl", "--socket", socket_path,
                    cwd=str(project_dir), env=env,
                    input_text="view webservers\n")
            assert moved.returncode == 0

            # B's write still names the old view: refused, and the
            # refusal's own answer moves B to the current view
            peer.stdin.write("mkdir resources/tag\n")
            peer.stdin.write("mkdir resources/tag2\n")
            peer.stdin.flush()
            out, err = peer.communicate(timeout=60)
        finally:
            if peer.poll() is None:
                peer.kill()
                peer.wait()
        assert "the view is now webservers" in err
        # The refused write landed nowhere
        assert not (project_dir / "ansible" / "views" / "webservers"
                    / "resources" / "tag").exists()
        # And the retry after the adoption went to the current view
        assert (project_dir / "ansible" / "views" / "webservers"
                / "resources" / "tag2").is_dir()
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_reads_during_a_run(run_debops, mcp_env, project_dir,
                            debops_source_dir, tmp_path):
    """A run holds the project lock for its whole duration; the
    content reads answer anyway, the tree readers refuse with the
    watch pointer, and the status names the run."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\n"
            'execute = "all"\n')
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir),
                   env=dict(os.environ, **mcp_env), check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text("""---
- name: Slow play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Take some time
      ansible.builtin.command: sleep 30
""")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir, env.get("PYTHONPATH", "")]).rstrip(
                    os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve"],
            cwd=str(project_dir), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
    try:
        # Start the run; its answer comes whenever, the point is the
        # lock is held meanwhile
        server.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": "run",
                       "arguments": {"playbook": "custom"}},
        }) + "\n")
        server.stdin.flush()
        runs_dir = tmp_path / "xdg-cache" / "debops" / "mcp" / "runs"
        log = None
        for _ in range(400):
            logs = list(runs_dir.glob("*/execution-1.log"))
            if logs and logs[0].stat().st_size > 0:
                log = logs[0]
                break
            time.sleep(0.05)
        assert log is not None, "the run log never appeared"

        def call(name, arguments):
            request = json.dumps({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"_meta": REQUEST_META, "name": name,
                           "arguments": arguments},
            }) + "\n"
            server.stdin.write(request)
            server.stdin.flush()
            while True:
                line = server.stdout.readline()
                assert line, "the server closed the answer"
                answer = json.loads(line)
                if answer.get("id") == 2:
                    assert "result" in answer, json.dumps(answer)[:400]
                    return answer

        # The tree reader refuses, with the watch pointer
        refused = call("project.read", {"path": "ansible.cfg"})
        assert refused["result"]["isError"] is True
        assert "execution.list" in \
            refused["result"]["content"][0]["text"]
        # The playbook listing reads what a run never mutates
        listed = call("playbook.list", {})
        assert listed["result"]["isError"] is False
        assert "custom" in \
            listed["result"]["content"][0]["text"]
        # And the status names the run
        status = call("project.status", {})
        assert status["result"]["isError"] is False
        assert "Run: in flight" in \
            status["result"]["content"][0]["text"]
    finally:
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=10)


def test_the_queue_runs_batches_in_order(run_debops, mcp_env,
                                         project_dir, tmp_path,
                                         debops_source_dir):
    """A queued batch runs its jobs in order through the normal
    execution path; the sidecars carry the job details."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\n"
            'execute = "all"\n')
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir),
                   env=dict(os.environ, **mcp_env), check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    for name in ('first', 'second'):
        (playbooks / "{}.yml".format(name)).write_text(
                "---\n- hosts: localhost\n  gather_facts: false\n"
                "  tasks:\n    - ansible.builtin.command: "
                "sleep 1\n")

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        created = server.call("batch.create", {"name": "work"})
        assert not created["result"].get("isError", False)
        for playbook in ("first", "second"):
            added = server.call("job.add", {"batch": "work",
                                            "playbook": playbook,
                                            "mode": "run"})
            assert not added["result"].get("isError", False), \
                added["result"]["content"][0]["text"]
        job = added["result"]["structuredContent"]["job"]
        assert job["id"] == "job-2"
        assert job["tally"]["taskCount"] == 1
        listed = server.call("queue.list", {})
        listed_batches = listed["result"]["structuredContent"][
                "batches"]
        assert listed_batches[0]["jobs"] == 2
        started = server.call("queue.start", {})
        assert not started["result"].get("isError", False)

        # The two runs land in order, the sidecars carry the job
        runs_dir = tmp_path / "xdg-cache" / "debops" / "mcp" / "runs"
        deadline = time.monotonic() + 120
        while True:
            finished = list(runs_dir.glob("*/execution-*.json"))
            done = [path for path in finished
                    if "finishedAt" in json.loads(path.read_text())]
            if len(done) >= 2:
                break
            assert time.monotonic() < deadline, \
                "the queue never ran the jobs"
            time.sleep(0.5)
        sidecars = sorted(
                (json.loads(path.read_text()) for path in done),
                key=lambda meta: meta["executionId"])
        assert [meta["job"]["id"] for meta in sidecars] == \
            ["job-1", "job-2"]
        assert sidecars[0]["job"]["batch"] == "work"
        assert sidecars[0]["taskCount"] == 1
        # The batch completes on the scheduler's next tick after the
        # last job — poll for it
        deadline = time.monotonic() + 30
        while True:
            listed = server.call("queue.list", {})
            if not listed["result"]["structuredContent"]["batches"]:
                break
            assert time.monotonic() < deadline, \
                "the batch never left the queue"
            time.sleep(0.5)
    finally:
        server.stop()


def test_the_queue_pending_gates_and_the_failure_cascades(
        run_debops, mcp_env, project_dir, tmp_path,
        debops_source_dir):
    """A pending batch gates itself and the ones behind it; a job
    failing fails its batch and the queue moves on."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\n"
            'execute = "all"\n')
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir),
                   env=dict(os.environ, **mcp_env), check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "broken.yml").write_text(
            "---\n- hosts: localhost\n  gather_facts: false\n"
            "  tasks:\n    - ansible.builtin.fail:\n"
            "        msg: broken on purpose\n")
    (playbooks / "fine.yml").write_text(
            "---\n- hosts: localhost\n  gather_facts: false\n"
            "  tasks:\n    - ansible.builtin.command: sleep 1\n")

    server = _LiveServer(project_dir, mcp_env, debops_source_dir)
    try:
        server.call("batch.create", {"name": "work"})
        server.call("job.add", {"batch": "work",
                                "playbook": "broken", "mode": "run"})
        server.call("batch.create", {"name": "after"})
        server.call("job.add", {"batch": "after",
                                "playbook": "fine", "mode": "run"})
        server.call("batch.create", {"name": "review"})
        server.call("job.add", {"batch": "review",
                                "playbook": "fine", "mode": "run"})
        server.call("batch.pending", {"name": "review"})
        server.call("queue.start", {})

        # The broken job fails its batch; the next batch runs; the
        # pending one gates
        runs_dir = tmp_path / "xdg-cache" / "debops" / "mcp" / "runs"
        deadline = time.monotonic() + 120
        while True:
            finished = list(runs_dir.glob("*/execution-*.json"))
            done = [json.loads(path.read_text())
                    for path in finished]
            done = [meta for meta in done if "finishedAt" in meta]
            states = [batch["name"] for batch in
                      server.call("queue.list", {})
                      ["result"]["structuredContent"]["batches"]]
            if len(done) >= 2:
                break
            # The pending batch never runs while pending
            assert all(meta.get("job", {}).get("batch") != "review"
                       for meta in done)
            assert time.monotonic() < deadline, \
                "the queue never ran the two non-pending batches"
            time.sleep(0.5)
        assert [meta["job"]["batch"] for meta in
                sorted(done, key=lambda meta:
                       meta["executionId"])] == ["work", "after"]
        # The failure is on record; the pending batch still gates.
        # The completed batch leaves on the scheduler's next tick —
        # poll for it
        deadline = time.monotonic() + 30
        while True:
            listing = server.call("queue.list", {})
            answer = listing["result"]["structuredContent"]
            if [batch["name"] for batch in answer["batches"]] == \
                    ["review"]:
                break
            assert time.monotonic() < deadline, \
                "the finished batch never left the queue"
            time.sleep(0.5)
        assert answer["lastFailure"]["batch"] == "work"
        # Rejecting it clears the queue
        server.call("batch.remove", {"name": "review"})
        assert server.call("queue.list", {})["result"][
                "structuredContent"]["batches"] == []
    finally:
        server.stop()


def test_the_queue_review_flow_over_the_front_end(
        run_debops, mcp_env, project_dir, tmp_path,
        debops_source_dir):
    """The human review of a staged batch, driven through the repl:
    pending gates the queue, the trusted accept releases it, and the
    run's sidecar carries the job."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\n")
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir),
                   env=dict(os.environ, **mcp_env), check=True)
    (project_dir / "ansible.cfg").write_text(
            "[defaults]\ninventory = ansible/inventory\n")
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text(
            "[local]\nlocalhost ansible_connection=local\n")
    playbooks = project_dir / "ansible" / "playbooks"
    playbooks.mkdir(parents=True)
    (playbooks / "custom.yml").write_text(
            "---\n- hosts: localhost\n  gather_facts: false\n"
            "  tasks:\n    - ansible.builtin.command: sleep 1\n")
    socket_path = str(tmp_path / "queue.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # The agent stages a batch and flags it for review
        result = run_debops(
                "mcp", "repl", "--socket", socket_path,
                cwd=str(project_dir), env=env,
                input_text=("batch create review-me\n"
                            "job add review-me custom mode=check\n"
                            "batch pending review-me\n"
                            "queue start\n"
                            "queue\n"))
        assert result.returncode == 0, result.stderr
        assert "pending review" in result.stdout
        # The gate holds: nothing runs while it is marked
        time.sleep(3)
        runs_dir = tmp_path / "xdg-cache" / "debops" / "mcp" / "runs"
        assert list(runs_dir.glob("*/execution-*.json")) == []
        # The operator accepts; the trust bootstrap of the front end
        # answers the passphrase check
        result = run_debops(
                "mcp", "repl", "--socket", socket_path,
                cwd=str(project_dir), env=env,
                input_text="batch accept review-me\n")
        assert result.returncode == 0, result.stderr
        assert "accepted" in result.stdout
        deadline = time.monotonic() + 90
        while True:
            done = [path for path in
                    runs_dir.glob("*/execution-*.json")
                    if "finishedAt" in json.loads(path.read_text())]
            if done:
                break
            assert time.monotonic() < deadline, \
                "the accepted batch never ran"
            time.sleep(0.5)
        metadata = json.loads(done[0].read_text())
        assert metadata["job"]["batch"] == "review-me"
        assert metadata["mode"] == "check"
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_repl_navigates_the_project(run_debops, mcp_env, project_dir,
                                    tmp_path, debops_source_dir):
    """The working-directory verbs against a live daemon: pwd, cd, ls,
    mkdir, cp, mv, rm — with the view edge enforced."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    env.update(GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="Test",
               GIT_COMMITTER_EMAIL="t@example.org")
    subprocess.run(["git", "init", "-q"], cwd=str(project_dir), env=env,
                   check=True)
    inventory = project_dir / "ansible" / "inventory"
    inventory.mkdir(parents=True)
    (inventory / "hosts").write_text("[all]\nlocalhost\n")

    socket_path = str(tmp_path / "nav.sock")
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input=("pwd\n"
                       "cd inventory\n"
                       "pwd\n"
                       "ls\n"
                       "cd ..\n"
                       "mkdir resources/scripts\n"
                       "cp inventory/hosts resources/scripts/copy.yml\n"
                       "mv resources/scripts/copy.yml "
                       "resources/moved.yml\n"
                       "rm resources/moved.yml\n"
                       "exit\n"),
                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        lines = result.stdout
        # pwd answers the project-rooted path of the working directory
        assert "/ansible\n" in lines
        assert "/ansible/inventory\n" in lines
        # ls listed the inventory, one level
        assert "- hosts" in lines
        assert "created: resources/scripts" in lines
        assert "moved: resources/scripts/copy.yml -> " \
               "resources/moved.yml" in lines
        assert "deleted: resources/moved.yml" in lines
        # The moves landed on disk and in the git record
        assert not (project_dir / "ansible" / "resources" /
                    "moved.yml").exists()
        assert not (project_dir / "ansible" / "resources" /
                    "scripts" / "copy.yml").exists()
        log = subprocess.run(["git", "log", "--format=%s"],
                             cwd=str(project_dir), env=env,
                             capture_output=True, text=True,
                             check=True)
        assert "DebOps MCP: file.move resources/scripts/copy.yml -> " \
               "resources/moved.yml" in log.stdout

        # The view edge refuses an unelevated escape
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="cd ..\n", capture_output=True, text=True,
                timeout=60)
        assert result.returncode == 1
        assert "the view's edge; elevate to go further" in result.stderr
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_repl_runs_a_check_through_the_daemon(run_debops, mcp_env,
                                              project_dir, tmp_path,
                                              debops_source_dir):
    """The task flow end to end: the check becomes a task the REPL
    polls; the confirmation gate declines in a pipe."""
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook is not installed")
    confd = project_dir / ".debops" / "conf.d"
    confd.mkdir(parents=True, exist_ok=True)
    (confd / "mcp-test.toml").write_text(
            "[project.mcp]\nconfirm_required = false\nconfirm = []\n")
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
        msg: hello from the repl
""")

    socket_path = str(tmp_path / "repl.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="check custom\nexit\n", capture_output=True,
                text=True, timeout=120)
        assert result.returncode == 0
        assert "exit code: 0" in result.stdout
        assert "hello from the repl" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_repl_speaks_the_legacy_era(run_debops, mcp_env, project_dir,
                                    tmp_path, debops_source_dir):
    """A daemon started with '--legacy' answers the REPL started the
    same way; the era mismatch is caught and named."""
    socket_path = str(tmp_path / "legacy.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path, "--legacy"],
            project_dir, socket_path, env)
    try:
        # The native REPL is taught the flag
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="status\nexit\n", capture_output=True, text=True,
                timeout=60)
        assert result.returncode == 1
        assert "retry with --legacy" in result.stderr

        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path, "--legacy"],
                cwd=str(project_dir), env=env,
                input="status\nexit\n", capture_output=True, text=True,
                timeout=60)
        assert result.returncode == 0
        assert "Project:" in result.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_trust_flow_over_a_socket(run_debops, mcp_env, project_dir,
                                  tmp_path, debops_source_dir):
    """The trust model end to end: the daemon generates the
    passphrase; authenticating with it marks the client trusted;
    elevating skips the confirmation gate."""
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
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from the repl
""")

    socket_path = str(tmp_path / "trust.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # The daemon generated the trust root, unreadable by the
        # tools and invisible to git
        trust_file = (project_dir / ".debops" / "mcp" / "secret"
                      / "trust")
        assert trust_file.exists()
        passphrase = trust_file.read_text()

        def call(method, params, request_id, token=None):
            import socket as socket_module
            connection = socket_module.socket(socket_module.AF_UNIX,
                                              socket_module.SOCK_STREAM)
            connection.connect(socket_path)
            meta = dict(REQUEST_META)
            meta["io.modelcontextprotocol/clientCapabilities"] = {
                    "elicitation": {}}
            if token is not None:
                meta["org.debops/token"] = token
            connection.sendall((json.dumps({
                    "jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": dict(
                            params or {}, _meta=meta)})
                    + "\n").encode())
            reader = connection.makefile("rb")
            response = json.loads(reader.readline())
            connection.close()
            return response

        # The local playbook's check asks the untrusted client
        ask = call("tools/call", {"name": "check",
                                  "arguments": {"playbook": "custom"}},
                   1)
        assert ask["result"].get("resultType") == "input_required" \
            or ask["result"].get("isError")

        # Authenticate: the passphrase is exchanged for a bearer
        answer = call("server/authenticate",
                      {"passphrase": passphrase}, 2)
        assert "error" not in answer
        token = answer["result"]["token"]

        # Trusted alone still asks; elevated runs
        gated = call("tools/call", {"name": "check",
                                    "arguments": {"playbook":
                                                  "custom"}},
                     3, token=token)
        assert gated["result"]["resultType"] == "input_required"
        answer = call("server/elevate",
                      {"passphrase": passphrase}, 4, token=token)
        assert answer["result"]["elevated"] is True
        ran = call("tools/call", {"name": "check",
                                  "arguments": {"playbook": "custom"}},
                   5, token=token)
        # No tasks capability declared: the run answers synchronously
        assert ran["result"]["resultType"] == "complete"
        assert ran["result"].get("isError") is False
        assert "exit code: 0" in ran["result"]["content"][0]["text"]
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_repl_elevate_skips_the_gate(run_debops, mcp_env, project_dir,
                                     tmp_path, debops_source_dir):
    """The pipe REPL elevates with the passphrase file and the gated
    check just runs."""
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
- name: Test play
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Say hello
      ansible.builtin.debug:
        msg: hello from the repl
""")

    socket_path = str(tmp_path / "elevate.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # The defaults gate the local playbook's check; the pipe
        # declines the question
        declined = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="check custom\n", capture_output=True, text=True,
                timeout=60)
        assert declined.returncode == 1
        assert "declined" in declined.stderr

        # Elevated, the same check runs
        elevated = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="elevate\ncheck custom\nexit\n",
                capture_output=True, text=True, timeout=120)
        assert elevated.returncode == 0
        assert "elevated" in elevated.stderr
        assert "exit code: 0" in elevated.stdout
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_repl_watch_without_a_trust_root(run_debops, mcp_env,
                                         project_dir, tmp_path,
                                         debops_source_dir):
    """No trust passphrase configured: 'watch' refuses cleanly
    instead of asking for one that cannot work."""
    socket_path = str(tmp_path / "notrust.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = _serve_daemon(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path], project_dir, socket_path, env)
    try:
        # The daemon generated the trust root at startup; deleting it
        # models a daemon which never did
        trust_file = (project_dir / ".debops" / "mcp" / "secret"
                      / "trust")
        assert trust_file.exists()
        trust_file.unlink()
        result = subprocess.run(
                [sys.executable, "-m", "debops", "mcp", "repl",
                 "--socket", socket_path],
                cwd=str(project_dir), env=env,
                input="watch\n", capture_output=True, text=True,
                timeout=60)
        assert result.returncode == 1
        assert "no trust passphrase" in result.stderr
        assert "Traceback" not in result.stderr
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_serve_split_stdio_and_socket(run_debops, mcp_env, project_dir,
                                      tmp_path, debops_source_dir):
    """'--socket --stdio': one server answers the harness' channel
    and the socket; the end of stdio ends both."""
    socket_path = str(tmp_path / "split.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path, "--stdio"],
            cwd=str(project_dir), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    discover = (json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "server/discover",
            "params": {"_meta": REQUEST_META}}) + "\n").encode()

    def readline_with_deadline(stream, deadline_s=15):
        import select as select_module
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ready, _w, _x = select_module.select([stream], [], [], 0.2)
            if ready:
                return stream.readline()
        raise RuntimeError("no answer in time")

    try:
        _serve_daemon_wait(socket_path)
        # The socket answers
        import socket as socket_module
        connection = socket_module.socket(socket_module.AF_UNIX,
                                          socket_module.SOCK_STREAM)
        connection.connect(socket_path)
        connection.sendall(discover)
        answer = json.loads(connection.makefile("rb").readline())
        assert "supportedVersions" in answer["result"]
        connection.close()
        # stdio answers
        server.stdin.write(discover)
        server.stdin.flush()
        answer = json.loads(readline_with_deadline(server.stdout))
        assert "supportedVersions" in answer["result"]
        # EOF on stdio ends both channels
        server.stdin.close()
        server.wait(timeout=20)
        assert server.returncode == 0
        assert not os.path.exists(socket_path)
    finally:
        server.kill()
        server.wait(timeout=10)


def test_serve_detach_refuses_stdio(run_debops, mcp_env, project_dir,
                                    tmp_path):
    result = run_debops("mcp", "serve", "--socket", "--stdio",
                        "--detach",
                        cwd=str(project_dir),
                        env=dict(mcp_env, XDG_RUNTIME_DIR=str(tmp_path)))
    assert result.returncode == 2
    assert "--stdio" in result.stderr


def test_repl_spawns_a_daemon_on_the_well_known_socket(
        run_debops, mcp_env, project_dir, tmp_path):
    """No daemon running: the pipe mode spawns one on the well-known
    runtime path."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "repl", env=env, cwd=str(project_dir),
                        input_text="status\nexit\n")
    sockets = []
    try:
        assert result.returncode == 0, result.stderr
        assert "Project:" in result.stdout
        # The daemon is detached: it outlives the interpreter, and the
        # pid sidecar rides next to the well-known socket
        sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
        assert len(sockets) == 1
        pid_path = Path(socket_pid_path(str(sockets[0])))
        assert pid_path.read_text().split()[1] == "spawned"
    finally:
        # Clean up the spawned daemon via its pid file
        if sockets:
            pid_path = Path(socket_pid_path(str(sockets[0])))
            if pid_path.exists():
                os.kill(int(pid_path.read_text().split()[0]), 15)


def test_mcp_stop_reaps_the_spawned_daemon(run_debops, mcp_env,
                                           project_dir, tmp_path):
    """'mcp stop' reaps the client-spawned daemon."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "repl", env=env, cwd=str(project_dir),
                        input_text="status\nexit\n")
    assert result.returncode == 0
    sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
    assert len(sockets) == 1
    pid_path = Path(socket_pid_path(str(sockets[0])))
    assert pid_path.read_text().split()[1] == "spawned"

    result = run_debops("mcp", "stop", env=env, cwd=str(project_dir))
    assert result.returncode == 0
    assert "stopped" in result.stdout
    # The socket and the sidecars went with the daemon
    assert not sockets[0].exists()
    assert not pid_path.exists()
    # A second stop is a clean refusal
    result = run_debops("mcp", "stop", env=env, cwd=str(project_dir))
    assert result.returncode == 1
    assert "no pid file" in result.stderr


def test_mcp_stop_reaps_a_hand_started_daemon(run_debops, mcp_env,
                                              project_dir, tmp_path):
    """A daemon from 'serve --detach' stops too; the pid is known
    from the sidecar and correlated against /proc first."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "serve", "--socket", "--detach",
                        cwd=str(project_dir), env=env)
    assert result.returncode == 0
    sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
    assert len(sockets) == 1
    pid_path = Path(socket_pid_path(str(sockets[0])))
    lines = pid_path.read_text().split()
    pid = int(lines[0])
    # The hand-started daemon carries no origin note
    assert len(lines) == 1
    result = run_debops("mcp", "stop", env=env, cwd=str(project_dir))
    assert result.returncode == 0
    assert "hand-started" in result.stdout
    assert "stopped" in result.stdout
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_mcp_stop_kill_is_the_sigkill(run_debops, mcp_env, project_dir,
                                      tmp_path):
    """'mcp stop --kill': no drain, and the stale files stay for the
    next daemon to clean up."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "serve", "--socket", "--detach",
                        cwd=str(project_dir), env=env)
    assert result.returncode == 0
    sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
    assert len(sockets) == 1
    pid_path = Path(socket_pid_path(str(sockets[0])))
    pid = int(pid_path.read_text().split()[0])
    result = run_debops("mcp", "stop", "--kill", env=env,
                        cwd=str(project_dir))
    assert result.returncode == 0
    assert "was killed" in result.stdout
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # Nothing cleaned up on a SIGKILL
    assert pid_path.exists()


def test_serve_detach_reports_and_serves(run_debops, mcp_env,
                                         project_dir, tmp_path):
    """'serve --socket --detach': the command returns once the daemon
    is up; the pid sidecar rides the well-known path."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    env = dict(mcp_env, XDG_RUNTIME_DIR=str(runtime))
    result = run_debops("mcp", "serve", "--socket", "--detach",
                        cwd=str(project_dir), env=env)
    assert result.returncode == 0
    assert "The daemon runs detached: pid " in result.stdout
    sockets = list((runtime / "debops" / "mcp").glob("*.sock"))
    assert len(sockets) == 1
    pid_path = Path(socket_pid_path(str(sockets[0])))
    pid = int(pid_path.read_text().strip())
    try:
        # The pid file names the serving process
        assert "debops-mcp" in Path(
                "/proc/{}/comm".format(pid)).read_text()
        # And it answers
        probe = run_debops("mcp", "inspect", str(sockets[0]),
                           cwd=str(project_dir), env=env)
        assert probe.returncode == 0
    finally:
        os.kill(pid, 15)


def test_serve_idle_timeout_stops_the_daemon(run_debops, mcp_env,
                                             project_dir, tmp_path,
                                             debops_source_dir):
    """An untouched daemon with an idle timeout goes away by itself,
    cleaning up the socket."""
    socket_path = str(tmp_path / "idle.sock")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
            [debops_source_dir,
             env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env.update(mcp_env)
    server = subprocess.Popen(
            [sys.executable, "-m", "debops", "mcp", "serve",
             "--socket", socket_path, "--idle-timeout", "2"],
            cwd=str(project_dir), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _serve_daemon_wait(socket_path)
        # No client ever connects: the daemon exits by itself
        server.wait(timeout=20)
        assert server.returncode == 0
        assert not os.path.exists(socket_path)
    finally:
        server.kill()
        server.wait(timeout=10)


def _serve_daemon_wait(socket_path):
    """Wait until the socket accepts a connection."""
    import socket as socket_module
    deadline = time.monotonic() + 15
    while True:
        try:
            probe = socket_module.socket(socket_module.AF_UNIX,
                                         socket_module.SOCK_STREAM)
            probe.connect(socket_path)
            probe.close()
            return
        except (ConnectionRefusedError, FileNotFoundError):
            if time.monotonic() > deadline:
                raise RuntimeError("the daemon did not come up")
            probe.close()
            time.sleep(0.05)


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


def test_skills_cli_filters(run_debops, mcp_env, tmp_path):
    # The generated role briefs are listed, and --filter narrows them
    result = run_debops("mcp", "skills", "--filter", "roles/nginx",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 0
    assert "roles/nginx" in result.stdout
    assert "nginx webserver" in result.stdout
    assert "roles/postfix" not in result.stdout

    result = run_debops("mcp", "skills", "--filter", "zzz-nothing",
                        cwd=str(tmp_path), env=mcp_env)
    assert result.returncode == 0
    assert "roles/" not in result.stdout


def test_ansible_doc_via_stdio(run_debops, mcp_env, tmp_path):
    if shutil.which("ansible-doc") is None:
        pytest.skip("ansible-doc is not installed")

    def call(arguments, request_id=1):
        request = json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"_meta": REQUEST_META, "name": "ansible.doc",
                       "arguments": arguments},
        }) + "\n"
        result = run_debops("mcp", "serve", cwd=str(tmp_path),
                            env=mcp_env, input_text=request)
        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        return response

    # The documentation mode serves it: ansible.builtin content and
    # the bundled collection's own plugins
    response = call({"plugin": "ansible.builtin.service"})
    text = response["result"]["content"][0]["text"]
    assert "manage" in text.lower() and "service" in text.lower()

    response = call({"plugin": "debops.debops.dpkg_divert",
                     "format": "json"}, 2)
    document = json.loads(response["result"]["content"][0]["text"])
    assert "debops.debops.dpkg_divert" in document

    response = call({"plugin": "nosuch.module"}, 3)
    assert response["result"]["isError"]


def test_docs_mode_serves_the_role_briefs(run_debops, mcp_env,
                                          tmp_path):
    # The generated corpus needs no project and no ansible
    def call(method, params, request_id=1):
        request = json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method,
            "params": dict(params, _meta=REQUEST_META),
        }) + "\n"
        result = run_debops("mcp", "serve", cwd=str(tmp_path),
                            env=mcp_env, input_text=request)
        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        return response

    # The extension is declared, and the corpus answers
    response = call("server/discover", {})
    extensions = response["result"]["capabilities"]["extensions"]
    assert extensions["io.modelcontextprotocol/skills"] == {
            "directoryRead": True}

    response = call("skills/get",
                    {"uri": "skill://roles/nginx/SKILL.md"})
    skill = response["result"]["skill"]
    assert skill["frontmatter"]["name"] == "nginx"
    assert "nginx" in skill["frontmatter"]["description"]
    assert skill["resources"] == "dynamic"

    response = call("resources/read",
                    {"uri": "skill://roles/nginx/SKILL.md"}, 2)
    text = response["result"]["contents"][0]["text"]
    assert "# debops.nginx" in text

    # The corpus is browsable as a directory; it outgrows one page, so
    # follow the cursor the way a client would
    children = []
    cursor = None
    for _request in range(10):
        params = {"uri": "skill://roles"}
        if cursor is not None:
            params["cursor"] = cursor
        response = call("resources/directory/read", params, 3)
        children.extend(response["result"]["resources"])
        cursor = response["result"].get("nextCursor")
        if cursor is None:
            break
    assert any(child["uri"] == "skill://roles/nginx" for child in children)


def test_configure_agents_writes_the_guidance(run_debops, mcp_env,
                                              project_dir):
    # The harness-agnostic nudge, in the file every harness reads
    result = run_debops("mcp", "configure", "--agents",
                        cwd=str(project_dir), env=mcp_env)
    assert result.returncode == 0
    assert "Created: AGENTS.md" in result.stdout
    content = (project_dir / "AGENTS.md").read_text()
    assert "## MCP server" in content
    assert "`check` before `run`" in content

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
