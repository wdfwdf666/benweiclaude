from __future__ import annotations

import os
import subprocess
import sys

import pytest

from codepilot.sandbox import SandboxPolicy, SandboxRunner, parse_command
from codepilot.sandbox.backends import BubblewrapBackend


def python_command(source: str, *args: str) -> list[str]:
    return [sys.executable, "-c", source, *args]


def test_policy_validation_and_command_parsing(tmp_path):
    policy = SandboxPolicy(tmp_path)
    assert policy.workspace == tmp_path.resolve()
    assert "PATH" in policy.allowed_environment
    assert parse_command(subprocess.list2cmdline(python_command("print(1)"))) == python_command("print(1)")
    with pytest.raises(ValueError, match="empty"):
        parse_command("   ")
    with pytest.raises(ValueError, match="profile"):
        SandboxPolicy(tmp_path, profile="root")
    with pytest.raises(ValueError, match="network"):
        SandboxPolicy(tmp_path, network_mode="private")
    with pytest.raises(ValueError, match="filesystem"):
        SandboxPolicy(tmp_path, filesystem_mode="host")
    with pytest.raises(ValueError, match="allowed_hosts"):
        SandboxPolicy(tmp_path, network_mode="allowlist")
    with pytest.raises(ValueError, match="timeout"):
        SandboxPolicy(tmp_path, timeout_seconds=0)


def test_runner_reports_platform_capabilities():
    capabilities = SandboxRunner().capabilities()
    assert capabilities.process_tree is True
    assert capabilities.memory_limit is True
    assert capabilities.process_limit is True
    assert capabilities.backend


def test_strict_mode_can_be_enabled_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_SANDBOX_STRICT", "true")
    assert SandboxPolicy(tmp_path).strict_isolation is True


def test_bubblewrap_command_contains_filesystem_and_network_boundaries(tmp_path, monkeypatch):
    backend = BubblewrapBackend()
    monkeypatch.setattr(backend, "executable", "/usr/bin/bwrap")
    policy = SandboxPolicy(tmp_path, filesystem_mode="workspace-read", strict_isolation=True)
    command = backend.wrap(["python", "task.py"], policy)

    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-net" in command
    workspace_bind = command.index("--ro-bind", command.index("--unshare-net"))
    assert command[workspace_bind:workspace_bind + 3] == [
        "--ro-bind", str(tmp_path.resolve()), str(tmp_path.resolve()),
    ]
    assert command[-2:] == ["python", "task.py"]


def test_strict_isolation_fails_closed_without_platform_backend(tmp_path):
    policy = SandboxPolicy(tmp_path, strict_isolation=True)
    runner = SandboxRunner()
    if os.name == "nt":
        with pytest.raises(RuntimeError, match="AppContainer"):
            runner.run(python_command("print(1)"), policy)
    else:
        backend = BubblewrapBackend()
        if not backend.available:
            with pytest.raises(RuntimeError, match="bubblewrap"):
                runner.run(python_command("print(1)"), policy)


def test_environment_is_minimal_and_explicitly_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_TEST_SECRET", "hidden-value")
    runner = SandboxRunner()
    source = "import os; print(os.getenv('CODEPILOT_TEST_SECRET', 'missing'))"

    hidden = runner.run(python_command(source), SandboxPolicy(tmp_path))
    visible = runner.run(
        python_command(source),
        SandboxPolicy(tmp_path, environment_allowlist=("CODEPILOT_TEST_SECRET",)),
    )

    assert hidden.stdout.strip() == "missing"
    assert visible.stdout.strip() == "hidden-value"


def test_output_is_bounded_and_timeout_kills_process(tmp_path):
    runner = SandboxRunner()
    output = runner.run(
        python_command("print('x' * 10000)"),
        SandboxPolicy(tmp_path, max_output_bytes=1024),
    )
    timeout = runner.run(
        python_command("import time; time.sleep(10)"),
        SandboxPolicy(tmp_path, timeout_seconds=0.2),
    )

    assert len(output.stdout.encode("utf-8")) == 1024
    assert output.output_truncated is True
    assert "sandbox output truncated" in output.render()
    assert timeout.timed_out is True
    assert timeout.returncode is None


def test_shell_metacharacters_are_passed_as_arguments(tmp_path):
    marker = tmp_path / "unexpected.txt"
    argv = python_command("import sys; print('|'.join(sys.argv[1:]))")
    command = subprocess.list2cmdline(argv) + f" & echo bad > {marker}"
    result = SandboxRunner().run(parse_command(command), SandboxPolicy(tmp_path))

    assert result.returncode == 0
    assert "&" in result.stdout
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-specific process-group assertion")
def test_timeout_kills_descendant_process_group(tmp_path):
    marker = tmp_path / "child-finished.txt"
    child = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        f"\"import time,pathlib;time.sleep(1);pathlib.Path(r'{marker}').write_text('bad')\"]);"
        "time.sleep(10)"
    )
    result = SandboxRunner().run(
        python_command(child), SandboxPolicy(tmp_path, timeout_seconds=0.2),
    )
    assert result.timed_out is True
    import time

    time.sleep(1.2)
    assert not marker.exists()
