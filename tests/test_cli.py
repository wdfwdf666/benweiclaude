import os
import sys
from types import SimpleNamespace

import pytest
from test_agent import FakeRegistry

from codepilot.agent import CodePilot
from codepilot.cli import command, handle_mcp_cli, load_environment


def test_env_from_launch_directory_when_workspace_has_none(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    workspace = tmp_path / "workspace"
    launch.mkdir()
    workspace.mkdir()
    (launch / ".env").write_text("QWEN_API_KEY=launch-test-key\n", encoding="utf-8")
    monkeypatch.chdir(launch)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    load_environment(workspace)
    assert os.environ["QWEN_API_KEY"] == "launch-test-key"


def test_workspace_env_takes_priority(tmp_path, monkeypatch):
    launch = tmp_path / "launch"
    workspace = tmp_path / "workspace"
    launch.mkdir()
    workspace.mkdir()
    (launch / ".env").write_text("QWEN_API_KEY=launch-test-key\n", encoding="utf-8")
    (workspace / ".env").write_text("QWEN_API_KEY=workspace-test-key\n", encoding="utf-8")
    monkeypatch.chdir(launch)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    load_environment(workspace)
    assert os.environ["QWEN_API_KEY"] == "workspace-test-key"


def test_mode_status_and_rename_commands(tmp_path, capsys):
    app = CodePilot(tmp_path, tmp_path / "state.db")
    app.new_session("qwen")
    command(app, "/mode plan")
    command(app, "/rename 调查登录模块")
    command(app, "/status")
    command(app, "/context")
    output = capsys.readouterr().out
    assert "权限：plan" in output
    assert "调查登录模块" in output
    assert app.store.get(app.session_id)["permission_mode"] == "plan"
    app.close()


def test_todo_and_memory_status_commands(tmp_path, capsys):
    app = CodePilot(tmp_path, tmp_path / "state.db")
    session_id = app.new_session("qwen")
    app.memory.create_task(session_id, "验证记忆命令")
    app.memory.save(
        scope="project", memory_type="note", content="记忆命令已经实现",
        source_session_id=session_id,
    )

    command(app, "/todo")
    command(app, "/memory status")
    command(app, "/memory search 记忆命令")
    output = capsys.readouterr().out
    assert "验证记忆命令" in output
    assert "长期记忆：1 条有效" in output
    assert "记忆命令已经实现" in output
    app.close()


def test_general_slash_commands(tmp_path, capsys):
    app = CodePilot(
        tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True,
    )
    app.new_session("deepseek")

    for line in (
        "/help", "/resume", "/model", "/mode", "/status", "/context",
        "/history", "/agents", "/skills", "/tools", "/hooks", "/mcp", "/sandbox",
        "/trust status", "/rewind", "/executions", "/compact", "/unknown",
    ):
        assert command(app, line) is True
    command(app, "/model qwen")
    command(app, "/plan")
    command(app, "/new openai")

    output = capsys.readouterr().out
    assert "/new [model]" in output
    assert "会话写租约：空闲" in output
    assert "当前会话没有编程子 Agent 任务" in output
    assert "当前会话没有工具执行记录" in output
    assert "进程树清理：有" in output
    assert "Unknown command" in output
    assert app.model_alias == "openai"
    app.close()


def test_slash_command_detail_branches(tmp_path, capsys, monkeypatch):
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    session_id = app.new_session("deepseek")
    memory_id = app.memory.save(
        scope="project", memory_type="note", content="命令分支记忆",
        source_session_id=session_id,
    )
    app.memory.create_task(session_id, "命令分支任务")
    app.executions.begin(session_id, "pending-call", "read_file", {"path": "a.txt"})

    for line in (
        "/rename", "/rename 新标题", "/resume", f"/resume {session_id[:8]}",
        "/model", "/mode", "/mode acceptEdits", "/rewind", "/todo",
        "/memory list", "/memory search", "/memory search 命令分支",
        f"/memory show {memory_id['id'][:8]}", "/memory invalid", "/history",
        "/executions", "/hooks", "/trust invalid",
    ):
        assert command(app, line) is True

    monkeypatch.setattr(app.hooks, "configured", lambda: [{
        "event": "PreToolUse", "matcher": "write_file", "command": "echo check",
    }])
    command(app, "/hooks")
    app.reload_mcp = lambda: [
        {"name": "ok", "status": "connected", "tools": ["echo"], "error": ""},
        {"name": "bad", "status": "failed", "tools": [], "error": "boom"},
    ]
    app.toolset.mcp = SimpleNamespace(path=tmp_path / ".mcp.json", close=lambda: None)
    command(app, "/mcp")

    output = capsys.readouterr().out
    assert "用法：/rename" in output
    assert "新标题" in output
    assert "命令分支任务" in output
    assert "命令分支记忆" in output
    assert "pending-call" in output
    assert "echo check" in output
    assert "✔ ok connected" in output
    assert "✘ bad failed" in output
    app.close()


def test_trust_command_enable_revoke_and_cancel(tmp_path, capsys, monkeypatch):
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    app.new_session()
    actions = []

    class FakeTrustStore:
        def trust(self, workspace):
            actions.append(("trust", workspace))

        def revoke(self, workspace):
            actions.append(("revoke", workspace))

    monkeypatch.setattr("codepilot.cli.WorkspaceTrustStore", FakeTrustStore)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    command(app, "/trust enable")
    command(app, "/trust enable")
    command(app, "/trust revoke")

    assert [item[0] for item in actions] == ["trust", "revoke"]
    assert "已取消" in capsys.readouterr().out
    app.close()


def test_mcp_management_cli_branches(tmp_path, capsys, monkeypatch):
    server = tmp_path / "server.py"
    server.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if 'id' in m:\n"
        "  tools=[{'name':'ping','inputSchema':{'type':'object'}}] if m.get('method')=='tools/list' else []\n"
        "  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'tools':tools}}),flush=True)\n",
        encoding="utf-8",
    )
    workspace_args = ["--workspace", str(tmp_path)]
    handle_mcp_cli([
        *workspace_args, "add", "--env", "MODE=test", "local", "--",
        sys.executable, str(server),
    ])
    handle_mcp_cli([*workspace_args, "get", "local"])
    monkeypatch.setattr("codepilot.cli.request_workspace_trust", lambda *_args: (True, False))
    handle_mcp_cli([*workspace_args, "list"])
    handle_mcp_cli([*workspace_args, "remove", "local"])
    handle_mcp_cli([*workspace_args, "add-json", "json", '{"command":"demo"}'])

    output = capsys.readouterr().out
    assert "Added MCP server local" in output
    assert '"MODE": "test"' in output
    assert "mcp__local__ping" in output
    assert "Removed MCP server local" in output

    with pytest.raises(SystemExit):
        handle_mcp_cli(["--workspace", str(tmp_path / "missing"), "list"])
    with pytest.raises(SystemExit):
        handle_mcp_cli([*workspace_args, "add", "empty"])
    with pytest.raises(SystemExit):
        handle_mcp_cli([*workspace_args, "add", "--env", "BROKEN", "bad-env", "--", "demo"])


def test_memory_forget_and_file_rewind_commands(tmp_path, capsys, monkeypatch):
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    session_id = app.new_session()
    memory = app.memory.save(
        scope="project", memory_type="fact", content="即将失效的记忆",
        source_session_id=session_id,
    )
    target = tmp_path / "changed.txt"
    before = app.file_checkpoints.capture([target])
    target.write_text("changed", encoding="utf-8")
    after = app.file_checkpoints.capture([target])
    checkpoint_id = app.file_checkpoints.record(session_id, "write_file", before, after)
    answers = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    command(app, f"/memory forget {memory['id'][:8]}")
    command(app, "/memory search 不存在内容")
    command(app, f"/rewind {checkpoint_id[:8]}")
    command(app, "/memory compact")
    with pytest.raises(ValueError, match="记忆不存在"):
        command(app, "/memory show missing")

    output = capsys.readouterr().out
    assert "已失效" in output
    assert "没有匹配" in output
    assert "已回退文件" in output
    assert not target.exists()
    app.close()
