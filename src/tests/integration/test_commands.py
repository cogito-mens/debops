# Copyright (C) 2026 Maciej Delmanowski <drybjed@gmail.com>
# Copyright (C) 2026 DebOps <https://debops.org/>
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end tests for the command sections which run Ansible.

``ansible-playbook`` is replaced by a stub on ``PATH`` which records its
invocation, so the tests can check the command DebOps built without running
any real playbook.
"""

import os

import pytest


pytestmark = pytest.mark.integration


def make_stub(directory, name, log):
    """Write an executable which logs its working directory and arguments."""
    stub = directory / name
    stub.write_text('#!/bin/sh\necho "$PWD" "$@" >> "{}"\n'.format(log))
    stub.chmod(0o755)
    return stub


@pytest.fixture
def legacy_project(run_debops, tmp_path, debops_env):
    project_dir = tmp_path / "example-legacy"
    run_debops("project", "init", "--no-requirements", str(project_dir),
               env=debops_env, check=True)
    return project_dir


def test_exec_eval_prints_the_ansible_command(run_debops, debops_env,
                                              legacy_project):
    result = run_debops("exec", "--eval", "all", "-m", "ping",
                        cwd=str(legacy_project), env=debops_env)

    assert result.returncode == 0
    assert "export ANSIBLE_CONFIG=" in result.stdout
    assert "ansible all -m ping" in result.stdout


def test_commands_fail_fast_while_the_run_lock_is_held(
        run_debops, debops_env, legacy_project):
    """The built-in commands respect the project run lock: the same
    flock the MCP executions hold."""
    import fcntl
    from debops.filelock import run_lock_path
    project = type('Project', (), {
            'path': str(legacy_project), 'project_type': 'legacy',
            'view': None,
            'config': type('Config', (), {'raw': {}})()})()
    lock_path = run_lock_path(project)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        result = run_debops("project", "commit", cwd=str(legacy_project),
                            env=debops_env, input_text="")
        assert result.returncode == 1
        assert "is locked" in result.stderr
        result = run_debops("env", "true", cwd=str(legacy_project),
                            env=debops_env)
        assert result.returncode == 1
        assert "is locked" in result.stderr
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Free again, the command goes through
    result = run_debops("env", "true", cwd=str(legacy_project),
                        env=debops_env)
    assert result.returncode == 0


def test_commands_wait_for_the_run_lock(run_debops, debops_env,
                                        legacy_project):
    """'--wait' blocks until the run lock is free."""
    import fcntl
    import threading
    import time
    from debops.filelock import run_lock_path
    project = type('Project', (), {
            'path': str(legacy_project), 'project_type': 'legacy',
            'view': None,
            'config': type('Config', (), {'raw': {}})()})()
    lock_path = run_lock_path(project)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    results = []

    def run():
        results.append(run_debops("env", "--wait", "true",
                                  cwd=str(legacy_project),
                                  env=debops_env))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # The command waits on the held lock
    time.sleep(1.0)
    assert results == []
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    thread.join(timeout=30)
    assert len(results) == 1
    assert results[0].returncode == 0


def test_run_executes_the_playbook(run_debops, tmp_path, debops_env,
                                   legacy_project):
    log = tmp_path / "ansible-playbook.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_stub(bin_dir, "ansible-playbook", log)
    env = dict(debops_env,
               PATH=os.pathsep.join([str(bin_dir), os.environ["PATH"]]))

    result = run_debops("run", "site.yml", cwd=str(legacy_project), env=env)

    assert result.returncode == 0
    invocation = log.read_text()
    assert str(legacy_project) in invocation
    assert "site.yml" in invocation


def test_check_runs_the_playbook_in_check_mode(run_debops, tmp_path,
                                               debops_env, legacy_project):
    log = tmp_path / "ansible-playbook.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_stub(bin_dir, "ansible-playbook", log)
    env = dict(debops_env,
               PATH=os.pathsep.join([str(bin_dir), os.environ["PATH"]]))

    result = run_debops("check", "site.yml", cwd=str(legacy_project), env=env)

    assert result.returncode == 0
    invocation = log.read_text()
    assert "site.yml" in invocation
    assert "--diff" in invocation
    assert "--check" in invocation


def test_env_runs_a_command_in_the_project_environment(run_debops, debops_env,
                                                       legacy_project):
    result = run_debops("env", "true", cwd=str(legacy_project),
                        env=debops_env)

    assert result.returncode == 0


def test_env_without_a_command_lists_the_environment(run_debops, debops_env,
                                                     legacy_project):
    result = run_debops("env", cwd=str(legacy_project), env=debops_env)

    assert result.returncode == 0
    assert "DEBOPS_ANSIBLE_COLLECTIONS_PATH=" in result.stdout


def test_config_get_reports_the_project_name(run_debops, debops_env,
                                             legacy_project):
    result = run_debops("config", "get", "project.name",
                        cwd=str(legacy_project), env=debops_env)

    assert result.returncode == 0
    assert result.stdout.strip() == "example-legacy"
