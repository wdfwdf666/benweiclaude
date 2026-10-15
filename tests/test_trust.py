from __future__ import annotations

import json
import sys

from test_hooks import FakeRegistry, configure

from codepilot.agent import CodePilot
from codepilot.cli import handle_mcp_cli
from codepilot.trust import WorkspaceTrustStore


def test_untrusted_workspace_does_not_execute_session_hook(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("from pathlib import Path\nPath('executed').write_text('yes')\n", encoding="utf-8")
    configure(tmp_path, {"SessionStart": "*"}, script)

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    app.new_session()
    app.close()

    assert not (tmp_path / "executed").exists()


def test_untrusted_malformed_hook_config_does_not_block_safe_startup(tmp_path):
    config_dir = tmp_path / ".codepilot"
    config_dir.mkdir()
    (config_dir / "hooks.json").write_text("not json", encoding="utf-8")

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    app.new_session()
    app.close()


def test_untrusted_workspace_does_not_start_mcp_process(tmp_path):
    marker = tmp_path / "mcp-started"
    command = f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')"
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"unsafe": {"command": sys.executable, "args": ["-c", command]}},
    }), encoding="utf-8")

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    app.new_session()
    app.close()

    assert not marker.exists()


def test_mcp_list_respects_workspace_trust(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "mcp-list-started"
    command = f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')"
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"unsafe": {"command": sys.executable, "args": ["-c", command]}},
    }), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "n")

    handle_mcp_cli(["--workspace", str(tmp_path), "list"])

    assert not marker.exists()
    assert "未启动 MCP Server" in capsys.readouterr().out


def test_trusted_workspace_executes_hook(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text("from pathlib import Path\nPath('executed').write_text('yes')\n", encoding="utf-8")
    configure(tmp_path, {"SessionStart": "*"}, script)

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    app.new_session()
    app.close()

    assert (tmp_path / "executed").read_text(encoding="utf-8") == "yes"


def test_config_change_invalidates_persisted_trust(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = workspace / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path / "trust.json")

    assert not store.is_trusted(workspace)
    store.trust(workspace)
    assert store.is_trusted(workspace)

    config.write_text(json.dumps({"mcpServers": {"new": {"command": sys.executable}}}), encoding="utf-8")
    assert not store.is_trusted(workspace)


def test_revoke_removes_persisted_trust(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    store.trust(workspace)

    store.revoke(workspace)

    assert not store.is_trusted(workspace)


def test_runtime_config_change_stops_further_hook_execution(tmp_path):
    first = tmp_path / "first.py"
    first.write_text("from pathlib import Path\nPath('first').write_text('yes')\n", encoding="utf-8")
    configure(tmp_path, {"SessionStart": "*"}, first)
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    app.new_session()
    assert (tmp_path / "first").exists()

    second = tmp_path / "second.py"
    second.write_text("from pathlib import Path\nPath('second').write_text('yes')\n", encoding="utf-8")
    configure(tmp_path, {"SessionStart": "*"}, second)
    app.hooks.reload()
    app.new_session()
    app.close()

    assert not (tmp_path / "second").exists()
