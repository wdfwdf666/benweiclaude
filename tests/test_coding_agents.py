from __future__ import annotations

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from codepilot.agent import CodePilot
from codepilot.permissions import decide
from codepilot.tools import ToolSet


class Registry:
    def __init__(self, model):
        self.model = model

    def get(self, alias):
        return self.model


class CodingModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "local-coding-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(message, ToolMessage) for message in messages):
            response = AIMessage(content="已完成分配的文件修改；未运行测试。")
        else:
            task = next(message.content for message in messages if isinstance(message, HumanMessage))
            path = "a.py" if "任务A" in task else "b.py"
            response = AIMessage(content="", tool_calls=[{
                "name": "write_file", "args": {"path": path, "content": "answer = 42\n"},
                "id": "write-1",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_parallel_coding_agents_stage_then_merge(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(CodingModel()))
    session_id = app.new_session()
    tasks = app.toolset.coding.run([
        {"task": "任务A：实现 a.py", "paths": ["a.py"]},
        {"task": "任务B：实现 b.py", "paths": ["b.py"]},
    ])
    assert [task["status"] for task in tasks] == ["ready", "ready"]
    assert [task["paths"] for task in tasks] == [["a.py"], ["b.py"]]
    assert not (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()
    app.close()
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(CodingModel()))
    app.resume(session_id)
    assert len(app.toolset.coding.list()) == 2
    assert "+answer = 42" in app.toolset.coding.preview([tasks[0]["id"]])
    assert decide("default", "apply_subagent_changes").kind == "ask"
    assert decide("acceptEdits", "apply_subagent_changes").kind == "ask"
    assert decide("plan", "delegate_coding_tasks").kind == "deny"
    assert decide("plan", "apply_subagent_changes").kind == "deny"
    app.toolset.coding.apply([task["id"] for task in tasks])
    assert (tmp_path / "a.py").read_text() == "answer = 42\n"
    assert (tmp_path / "b.py").read_text() == "answer = 42\n"
    assert all(task["merged"] for task in app.toolset.coding.list())
    app.close()


def test_coding_merge_rejects_changed_source_and_overlapping_scopes(tmp_path):
    (tmp_path / "a.py").write_text("original\n")
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(CodingModel()))
    app.new_session()
    with pytest.raises(ValueError, match="overlapping"):
        app.toolset.coding.run([
            {"task": "任务A", "paths": ["a.py"]},
            {"task": "任务B", "paths": ["a.py"]},
        ])
    staged = app.toolset.coding.run([{"task": "任务A", "paths": ["a.py"]}])[0]
    (tmp_path / "a.py").write_text("user edit\n")
    with pytest.raises(ValueError, match="Merge conflict"):
        app.toolset.coding.apply([staged["id"]])
    assert (tmp_path / "a.py").read_text() == "user edit\n"
    app.close()


def test_coding_subagent_cannot_write_outside_assigned_paths(tmp_path):
    toolset = ToolSet(tmp_path, Registry(CodingModel()), lambda: "session", lambda: "deepseek",
                      write_scope=[{"path": "a.py", "kind": "file"}], enable_delegation=False)
    with pytest.raises(ValueError, match="outside"):
        toolset.by_name["write_file"].invoke({"path": "b.py", "content": "no"})
    assert not (tmp_path / "b.py").exists()
    directory_toolset = ToolSet(tmp_path, Registry(CodingModel()), lambda: "session", lambda: "deepseek",
                                write_scope=[{"path": "pkg", "kind": "directory"}], enable_delegation=False)
    directory_toolset.by_name["write_file"].invoke({"path": "pkg/nested/module.py", "content": "ok\n"})
    assert (tmp_path / "pkg/nested/module.py").read_text() == "ok\n"


class SupervisorModel(CodingModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(item, SystemMessage) and str(item.content).startswith("你是编程子 Agent") for item in messages):
            return super()._generate(messages, stop, run_manager, **kwargs)
        results = [item for item in messages if isinstance(item, ToolMessage)]
        names = [item.name for item in results]
        if "apply_subagent_changes" in names:
            response = AIMessage(content="已合并；测试尚未执行。")
        elif "review_subagent_changes" in names:
            stage_id = json.loads(next(item.content for item in results if item.name == "delegate_coding_tasks"))[0]["id"]
            response = AIMessage(content="", tool_calls=[{
                "name": "apply_subagent_changes", "args": {"task_ids": [stage_id]}, "id": "merge-1",
            }])
        elif "delegate_coding_tasks" in names:
            stage_id = json.loads(results[-1].content)[0]["id"]
            response = AIMessage(content="", tool_calls=[{
                "name": "review_subagent_changes", "args": {"task_id": stage_id}, "id": "review-1",
            }])
        else:
            response = AIMessage(content="", tool_calls=[{
                "name": "delegate_coding_tasks",
                "args": {"tasks": [{"task": "任务A：实现 a.py", "paths": ["a.py"]}]},
                "id": "delegate-1",
            }])
        return ChatResult(generations=[ChatGeneration(message=response)])


def test_supervisor_reviews_and_approval_gates_coding_merge(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(SupervisorModel()))
    app.new_session()
    interrupted = app.send("实现 a.py")
    assert "__interrupt__" in interrupted
    request = interrupted["__interrupt__"][0].value["requests"][0]
    assert request["name"] == "apply_subagent_changes"
    assert not (tmp_path / "a.py").exists()
    resumed = app.approve({request["id"]: True})
    assert resumed["messages"][-1].content == "已合并；测试尚未执行。"
    assert "Tool error" not in str(next(item.content for item in resumed["messages"] if isinstance(item, ToolMessage) and item.name == "apply_subagent_changes"))
    assert (tmp_path / "a.py").read_text() == "answer = 42\n"
    assert app.file_checkpoints.list(app.session_id)[0]["tool_name"] == "apply_subagent_changes"
    app.close()
