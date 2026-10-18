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

    Unit tests construct ProjectDir in-process, which leaks ANSIBLE_CONFIG
    into os.environ; ProjectDir would then redirect the project path to
    its parent directory. Neutralize the variable so the subprocess uses
    the project directory it is given.
    """
    return dict(debops_env, ANSIBLE_CONFIG="")


# The per-request "_meta" fields the specification requires
REQUEST_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def make_discover_request(request_id=1):
    return json.dumps({
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": request_id,
        "method": "server/discover", "params": {},
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
    # prompts/* are not implemented (skills are served as resources)
    request = (json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META,
                           "id": 5, "method": "prompts/list"}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["id"] == 5
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


def test_serve_outside_a_project_fails(run_debops, mcp_env, tmp_path):
    result = run_debops("mcp", "serve", cwd=str(tmp_path), env=mcp_env,
                        input_text="")

    assert result.returncode == 1
    assert "project directory not found" in result.stderr
    # Nothing may be written to the protocol channel
    assert result.stdout == ""


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
    request = (json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META,
                           "id": 1, "method": "tools/list"}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert sorted(names) == ["ansible_facts", "ansible_ping", "check",
                             "collection_playbooks", "collection_roles",
                             "config_get", "inventory_query",
                             "inventory_spec", "playbook_info",
                             "playbooks", "project_files",
                             "project_pull", "project_push", "project_read",
                             "project_status", "role_argspec",
                             "role_defaults", "role_files",
                             "role_handlers", "role_info", "role_tasks",
                             "role_templates", "role_variables",
                             "secret_paths", "view_delete",
                             "view_write"]


def test_role_defaults_via_stdio(run_debops, mcp_env, project_dir):
    brief = json.dumps({
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "role_defaults",
                   "arguments": {"name": "elasticsearch",
                                 "filter": "base_packages"}},
    })
    full = json.dumps({
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 2,
        "method": "tools/call",
        "params": {"name": "role_defaults",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "project_read",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "inventory_query", "arguments": {}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["result"]["isError"] is False
    assert "@testgroup" in response["result"]["content"][0]["text"]


def test_role_inspection_via_stdio(run_debops, mcp_env, project_dir):
    requests = (
        json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
                    "method": "tools/call",
                    "params": {"name": "role_tasks",
                               "arguments": {"name": "nginx",
                                             "file": "main"}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 2,
                      "method": "tools/call",
                      "params": {"name": "role_templates",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "collection_roles", "arguments": {}},
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "playbooks",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "playbook_info",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "check", "arguments": {"playbook": "custom"}},
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "check",
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
                "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}) + "\n"
        result = run_debops("mcp", "serve", cwd=str(project_dir),
                            env=mcp_env, input_text=request)
        assert result.returncode == 0
        (response,) = read_json_lines(result.stdout)
        return response["result"]

    written = call("view_write", {"path": "playbooks/written.yml",
                                  "content": playbook})
    assert "created: playbooks/written.yml" in written["content"][0]["text"]
    listed = call("playbooks", {"filter": "written"})
    assert "written" in listed["content"][0]["text"]
    checked = call("check", {"playbook": "written"})
    assert checked["isError"] is False
    assert "written by mcp" in checked["content"][0]["text"]

    # The write was committed with MCP provenance
    log = subprocess.run(["git", "log", "--format=%s", "-1"],
                         cwd=str(project_dir), env=env,
                         capture_output=True, text=True)
    assert "DebOps MCP: view_write playbooks/written.yml" in log.stdout


def test_inventory_spec_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

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
                "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}) + "\n"
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
    preview = call("inventory_spec", {"spec": spec, "dry_run": True})
    assert "dry run" in preview["content"][0]["text"]
    assert "Planned writes" in preview["content"][0]["text"]
    # Nothing was written
    assert not (project_dir / "ansible" / "inventory" / "hosts").exists()

    applied = call("inventory_spec", {"spec": spec})
    assert "## Written (1)" in applied["content"][0]["text"]
    assert (project_dir / "ansible" / "inventory"
            / "hosts").read_text() == "[spec_group]\nspec1.example.com\n"

    graph = call("inventory_query", {"action": "graph"})
    assert "spec_group" in graph["content"][0]["text"]

    # Committed with MCP provenance
    log = subprocess.run(["git", "log", "--format=%s", "-1"],
                         cwd=str(project_dir), env=env,
                         capture_output=True, text=True)
    assert "DebOps MCP: inventory_spec" in log.stdout


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
        json.dumps({"jsonrpc": "2.0", "_meta": elicit_meta, "id": 1,
                    "method": "tools/call",
                    "params": {"name": "run",
                               "arguments": {"playbook": "custom"}}}) + "\n"
        # The retry carries the user's answer, with a fresh request id
        + json.dumps({"jsonrpc": "2.0", "_meta": elicit_meta, "id": 2,
                      "method": "tools/call",
                      "params": {
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "secret_paths",
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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "ansible_ping", "arguments": {"hosts": "local"}},
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
        json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
                    "method": "tools/call",
                    "params": {"name": "project_pull",
                               "arguments": {}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 2,
                      "method": "tools/call",
                      "params": {"name": "project_pull",
                                 "arguments": {}}}) + "\n"
    )
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=requests)

    assert result.returncode == 0
    responses = {response["id"]: response
                 for response in read_json_lines(result.stdout)}
    first = responses[1]["result"]
    assert first["isError"] is False
    assert "Already up to date" in first["content"][0]["text"]
    # The second, immediate call is throttled
    second = responses[2]["result"]
    assert second["isError"] is True
    assert "retry in" in second["content"][0]["text"]


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
        "jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
        "method": "tools/call",
        "params": {"name": "project_pull", "arguments": {}}}) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is False
    assert "project refreshed" in text
    # ansible.cfg was regenerated and carries the pulled configuration
    ansible_cfg = (project_dir / "ansible.cfg").read_text()
    assert "force_color" in ansible_cfg
    # The skeleton directory is back
    assert (project_dir / "ansible" / "playbooks").is_dir()


def test_check_with_limit_resolves_hosts(run_debops, mcp_env, project_dir):
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
        msg: hello from mcp
""")

    requests = (
        json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 1,
                    "method": "tools/call",
                    "params": {"name": "check",
                               "arguments": {"playbook": "custom",
                                             "limit": "local"}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META, "id": 2,
                      "method": "tools/call",
                      "params": {"name": "check",
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
            "---\nproject:\n  mcp:\n    deny: ['role_info']\n")

    request = (json.dumps({"jsonrpc": "2.0", "_meta": REQUEST_META,
                           "id": 1, "method": "tools/list"}) + "\n")
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "role_info" not in names
    assert "role_variables" in names


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
