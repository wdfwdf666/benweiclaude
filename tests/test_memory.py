from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage

from codepilot.agent import CodePilot
from codepilot.memory import MemoryManager


class RecordingModel:
    def __init__(self, tool_call=None):
        self.tool_call = tool_call
        self.seen = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        if self.tool_call and not any(isinstance(item, ToolMessage) for item in messages):
            return AIMessage(content="", tool_calls=[self.tool_call])
        return AIMessage(content="普通回复")


class Registry:
    def __init__(self, tool_call=None):
        self.model = RecordingModel(tool_call)

    def get(self, alias):
        return self.model


def test_memory_retrieval_and_file_invalidation(tmp_path):
    manager = MemoryManager(tmp_path / "state.db", tmp_path)
    saved = manager.save(
        scope="project", memory_type="architecture",
        content="认证模块使用 JWT，并位于 src/auth.py",
        related_files=["src/auth.py"], importance=0.9, confidence=0.95,
    )
    assert manager.retrieve("JWT认证")[0]["id"] == saved["id"]

    invalidated = manager.invalidate_by_files(["src/auth.py"])
    assert invalidated == [saved["id"]]
    assert manager.retrieve("JWT认证") == []
    assert manager.get(saved["id"])["invalidation_reason"] == "related file changed"
    manager.close()


def test_memory_update_preserves_superseded_record(tmp_path):
    manager = MemoryManager(tmp_path / "state.db", tmp_path)
    original = manager.save(
        scope="project", memory_type="decision", content="使用旧认证方案",
        related_files=["src/auth"],
    )
    replacement = manager.update(original["id"][:8], "改用新的认证方案")
    old = manager.get(original["id"])
    assert replacement["id"] != original["id"]
    assert old["invalidation_reason"] == "superseded"
    assert old["superseded_by"] == replacement["id"]
    assert manager.invalidate_by_files(["src/auth/service.py"]) == [replacement["id"]]
    manager.close()


def test_structured_tasks_are_session_scoped_and_migrate_legacy_json(tmp_path):
    state_dir = tmp_path / ".codepilot"
    state_dir.mkdir()
    (state_dir / "todo-old-session.json").write_text(json.dumps([
        {"content": "读取旧任务", "status": "in_progress"},
    ], ensure_ascii=False), encoding="utf-8")
    manager = MemoryManager(tmp_path / "state.db", tmp_path)

    migrated = manager.list_tasks("old-session")
    assert len(migrated) == 1
    assert migrated[0]["status"] == "in_progress"
    created = manager.create_task("new-session", "运行测试")
    manager.update_task("new-session", created["id"][:8], status="completed", evidence=["36 passed"])
    assert manager.list_tasks("old-session")[0]["content"] == "读取旧任务"
    assert manager.list_tasks("new-session")[0]["evidence"] == ["36 passed"]
    manager.close()


def test_tasks_and_relevant_memory_are_injected_into_every_model_call(tmp_path):
    registry = Registry()
    app = CodePilot(tmp_path, tmp_path / "state.db", registry)
    session_id = app.new_session()
    app.memory.create_task(session_id, "补充登录测试")
    app.memory.save(
        scope="project", memory_type="fact", content="登录入口位于 login.py",
        source_session_id=session_id, related_files=["login.py"],
    )

    app.send("登录功能下一步做什么")
    system = registry.model.seen[-1][0].content
    assert "当前会话任务状态" in system
    assert "补充登录测试" in system
    assert "检索到的项目记忆" in system
    assert "登录入口位于 login.py" in system
    snapshot = app.graph.get_state(app.config()).values
    assert snapshot["active_tasks"][0]["content"] == "补充登录测试"
    assert snapshot["retrieved_memories"][0]["memory_type"] == "fact"
    app.close()


def test_compact_persists_structured_summary_across_restart(tmp_path):
    db = tmp_path / "state.db"
    first = CodePilot(tmp_path, db, Registry())
    session_id = first.new_session()
    for number in range(3):
        first.send(f"问题 {number}")
    first.compact()
    state = first.graph.get_state(first.config()).values
    assert state["session_summary"]["goal"] == "普通回复"
    assert "先前会话摘要" in first.history()[0].content
    first.close()

    second = CodePilot(tmp_path, db, Registry())
    second.resume(session_id)
    restored = second.graph.get_state(second.config()).values
    assert restored["session_summary"]["goal"] == "普通回复"
    assert "先前会话摘要" in second.history()[0].content
    second.close()


def test_successful_file_edit_invalidates_related_memory(tmp_path):
    (tmp_path / "app.py").write_text("old\n", encoding="utf-8")
    call = {
        "name": "write_file", "args": {"path": "app.py", "content": "new\n"}, "id": "write-1",
    }
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(call))
    session_id = app.new_session(permission_mode="acceptEdits")
    memory = app.memory.save(
        scope="project", memory_type="fact", content="app.py 当前内容是 old",
        source_session_id=session_id, related_files=["app.py"],
    )
    app.send("更新 app.py")
    assert app.memory.get(memory["id"])["invalidated_at"] is not None
    app.close()
