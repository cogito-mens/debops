# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tests of the 'debops mcp' command line.

These tests drive the real ``python -m debops mcp`` entry point as a
subprocess, so they exercise the argument handling and the protocol
framing a user (or an MCP client) sees.
"""

from debops.mcp.server import McpServer
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


def make_discover_request(request_id=1):
    return json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "server/discover",
        "params": {},
        "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
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
    assert response["result"]["protocolVersion"] == "2026-07-28"
    assert response["result"]["serverInfo"]["name"] == "debops"
    assert "tools" in response["result"]["capabilities"]


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
    request = ('{"jsonrpc": "2.0", "id": 5, "method": "prompts/list"}\n')
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
    request = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert sorted(names) == ["check", "collection_playbook_info",
                             "collection_playbooks", "collection_role_info",
                             "collection_role_variables", "collection_roles",
                             "config_get", "inventory_query",
                             "playbook_info", "playbooks", "project_files",
                             "project_read", "project_status", "role_info",
                             "role_variables"]


def test_role_variables_via_stdio(run_debops, mcp_env, project_dir):
    brief = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "role_variables",
                   "arguments": {"name": "elasticsearch",
                                 "filter": "base_packages"}},
    })
    full = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "role_variables",
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
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "inventory_query", "arguments": {}},
    }) + "\n"
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    assert response["result"]["isError"] is False
    assert "@testgroup" in response["result"]["content"][0]["text"]


def test_collection_roles_via_stdio(run_debops, mcp_env, project_dir):
    if shutil.which("ansible-galaxy") is None:
        pytest.skip("ansible-galaxy is not installed")

    request = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
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
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
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


def test_policy_deny_hides_a_tool(run_debops, mcp_env, project_dir):
    conf_d = project_dir / ".debops" / "conf.d"
    conf_d.mkdir(parents=True)
    (conf_d / "mcp.yml").write_text(
            "---\nproject:\n  mcp:\n    deny: ['role_info']\n")

    request = ('{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}\n')
    result = run_debops("mcp", "serve", cwd=str(project_dir),
                        env=mcp_env, input_text=request)

    assert result.returncode == 0
    (response,) = read_json_lines(result.stdout)
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert "role_info" not in names
    assert "role_variables" in names


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
        assert response["result"]["protocolVersion"] == "2026-07-28"
    finally:
        transport.shutdown()
        thread.join(timeout=5)
