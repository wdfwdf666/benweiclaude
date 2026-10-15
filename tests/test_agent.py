from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from codepilot.agent import CodePilot
from codepilot.cli import ConsoleTrace, finish_pending, preview_change


class FakeModel:
    def __init__(self, alias: str, tool_call: dict | None = None):
        self.alias = alias
        self.tool_call = tool_call
        self.seen = []

    def bind_tools(self, tools):
        assert len(tools) >= 1
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        if self.tool_call and not any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content="", tool_calls=[self.tool_call])
        return AIMessage(content=f"reply from {self.alias}")


class FakeRegistry:
    def __init__(self, tool_call=None):
        self.models = {
            alias: FakeModel(alias, tool_call if alias == "deepseek" else None)
            for alias in ("deepseek", "qwen", "openai")
        }

    def get(self, alias):
        return self.models[alias]


def test_session_survives_restart_and_model_switch(tmp_path):
    db = tmp_path / "state.db"
    registry = FakeRegistry()
    first = CodePilot(tmp_path, db, registry)
    session_id = first.new_session("deepseek")
    result = first.send("first question")
    assert result["messages"][-1].content == "reply from deepseek"
    first.close()

    second_registry = FakeRegistry()
    second = CodePilot(tmp_path, db, second_registry)
    second.resume(session_id[:10])
    assert len(second.history()) == 2
    second.switch_model("qwen")
    result = second.send("second question")
    assert result["messages"][-1].content == "reply from qwen"
    seen = second_registry.models["qwen"].seen[-1]
    assert any(getattr(message, "content", None) == "first question" for message in seen)
    assert second.store.get(session_id)["model"] == "qwen"
    second.close()


def test_approval_can_resume_after_restart(tmp_path, monkeypatch):
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "created"}, "id": "call-1"}
    db = tmp_path / "state.db"
    first = CodePilot(tmp_path, db, FakeRegistry(call))
    session_id = first.new_session()
    result = first.send("create demo")
    assert result["__interrupt__"][0].value["requests"][0]["name"] == "write_file"
    assert not (tmp_path / "demo.txt").exists()
    first.close()

    second = CodePilot(tmp_path, db, FakeRegistry(call))
    second.resume(session_id)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    finish_pending(second)
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "created"
    assert second.history()[-1].content == "reply from deepseek"
    second.close()


def test_denial_has_no_side_effect(tmp_path):
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "created"}, "id": "call-1"}
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call))
    app.new_session()
    app.send("create demo")
    app.approve({"call-1": False})
    assert not (tmp_path / "demo.txt").exists()
    assert any(isinstance(message, ToolMessage) and "Denied" in message.content for message in app.history())
    app.close()


def test_workspace_path_is_confined(tmp_path):
    (tmp_path / "workspace").mkdir()
    app = CodePilot(tmp_path / "workspace", tmp_path / "state.db", FakeRegistry())
    app.new_session()
    try:
        app.toolset.path("../outside.txt")
    except ValueError:
        pass
    else:
        raise AssertionError("Path traversal was accepted")
    try:
        app.toolset.path(".env")
    except ValueError:
        pass
    else:
        raise AssertionError("Credential file was exposed")
    app.close()


def test_tool_execution_emits_live_trace(tmp_path, capsys):
    (tmp_path / "demo.txt").write_text("hello trace", encoding="utf-8")
    call = {"name": "read_file", "args": {"path": "demo.txt"}, "id": "call-read"}
    events = []

    def record(event, data):
        events.append(event)
        ConsoleTrace()(event, data)

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call), on_event=record)
    app.new_session()
    app.send("read demo")
    output = capsys.readouterr().out
    assert events.index("model_start") < events.index("tool_start") < events.index("tool_result")
    assert "[工具请求] read_file" in output
    assert "[工具结果] read_file" in output
    assert "hello trace" in output
    app.close()


def test_accept_edits_writes_checkpoint_and_rewind_survives_restart(tmp_path):
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "new"}, "id": "write-1"}
    db = tmp_path / "state.db"
    app = CodePilot(tmp_path, db, FakeRegistry(call))
    session_id = app.new_session(permission_mode="acceptEdits")
    result = app.send("create file")
    assert "__interrupt__" not in result
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "new"
    checkpoints = app.file_checkpoints.list(session_id)
    assert len(checkpoints) == 1
    assert checkpoints[0]["paths"] == ["demo.txt"]
    app.close()

    restarted = CodePilot(tmp_path, db, FakeRegistry())
    restarted.resume(session_id)
    assert restarted.permission_mode == "acceptEdits"
    restored = restarted.rewind_files(checkpoints[0]["id"][:12])
    assert restored == ["demo.txt"]
    assert not (tmp_path / "demo.txt").exists()
    assert "用户回退了文件修改" in restarted.history()[-1].content
    restarted.close()


def test_plan_mode_blocks_write_even_if_model_requests_it(tmp_path):
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "new"}, "id": "write-1"}
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call))
    app.new_session(permission_mode="plan")
    result = app.send("create file")
    assert "__interrupt__" not in result
    assert not (tmp_path / "demo.txt").exists()
    assert any("permission mode" in message.content for message in app.history() if isinstance(message, ToolMessage))
    app.close()


def test_compact_replaces_old_messages_and_keeps_conversation_usable(tmp_path):
    registry = FakeRegistry()
    app = CodePilot(tmp_path, tmp_path / "state.db", registry)
    app.new_session()
    for number in range(3):
        app.send(f"question {number}")
    before = len(app.history())
    result = app.compact()
    assert result["before"] == before
    assert result["after"] < before
    assert "先前会话摘要" in app.history()[0].content
    app.send("next question")
    assert app.history()[-1].content == "reply from deepseek"
    app.close()


def test_project_instructions_are_loaded_into_system_message(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("优先运行快速测试。", encoding="utf-8")
    registry = FakeRegistry()
    app = CodePilot(tmp_path, tmp_path / "state.db", registry)
    app.new_session()
    app.send("hello")
    assert "优先运行快速测试" in registry.models["deepseek"].seen[-1][0].content
    app.close()


def test_write_preview_shows_unified_diff(tmp_path):
    (tmp_path / "demo.txt").write_text("old\n", encoding="utf-8")
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    diff = preview_change(app, {"name": "write_file", "args": {"path": "demo.txt", "content": "new\n"}})
    assert "-old" in diff and "+new" in diff
    app.close()


def test_oversize_edit_is_rolled_back(tmp_path, monkeypatch):
    monkeypatch.setattr("codepilot.checkpoints.MAX_SNAPSHOT_BYTES", 5)
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "more than five"}, "id": "write-1"}
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call))
    app.new_session(permission_mode="acceptEdits")
    app.send("create file")
    assert not (tmp_path / "demo.txt").exists()
    assert any("too large" in message.content for message in app.history() if isinstance(message, ToolMessage))
    app.close()


def test_move_file_checkpoint_restores_both_paths(tmp_path):
    (tmp_path / "source.txt").write_text("original", encoding="utf-8")
    call = {"name": "move_file", "args": {"source": "source.txt", "destination": "target.txt"}, "id": "move-1"}
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call))
    session_id = app.new_session(permission_mode="acceptEdits")
    app.send("move file")
    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "original"
    checkpoints = app.file_checkpoints.list(session_id)
    assert checkpoints, [message.content for message in app.history()]
    checkpoint = checkpoints[0]
    app.rewind_files(checkpoint["id"])
    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "target.txt").exists()
    app.close()
