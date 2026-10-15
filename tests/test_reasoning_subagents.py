from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from codepilot.agent import CodePilot
from codepilot.cli import ConsoleTrace, finish_turn
from codepilot.models import ModelRegistry
from codepilot.reasoning import exposed_reasoning
from codepilot.subagents import SubagentRunner


class StreamingModel:
    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        if any(isinstance(item, ToolMessage) for item in messages):
            yield AIMessageChunk(content="已读取")
        else:
            yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "先读取目标文件。"})
            yield AIMessageChunk(content="", tool_call_chunks=[
                {"name": "read_file", "args": '{"path":"demo.txt"}', "id": "read-1", "index": 0},
            ])


class Registry:
    def __init__(self, model):
        self.model = model

    def get(self, alias):
        return self.model


def test_provider_reasoning_and_streamed_tool_call(tmp_path, capsys):
    (tmp_path / "demo.txt").write_text("hello", encoding="utf-8")
    trace = ConsoleTrace(show_reasoning=True)
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(StreamingModel()), on_event=trace)
    app.new_session()
    result = app.send("read demo")
    finish_turn(app, result)
    output = capsys.readouterr().out
    assert "[模型推理] 先读取目标文件" in output
    assert "[工具请求] read_file" in output
    assert "[模型输出] 已读取" in output
    assert output.count("已读取") == 1
    assert result["messages"][-1].content == "已读取"
    app.close()


class LocalSubagentModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "local-test-subagent"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(item, ToolMessage) for item in messages):
            answer = AIMessage(content="文件包含 hello")
        else:
            answer = AIMessage(content="", tool_calls=[
                {"name": "read_file", "args": {"path": "demo.txt"}, "id": "read-1"}
            ])
        return ChatResult(generations=[ChatGeneration(message=answer)])


def test_subagents_run_in_separate_contexts_and_report_progress(tmp_path):
    (tmp_path / "demo.txt").write_text("hello", encoding="utf-8")
    model = LocalSubagentModel()
    events = []
    app = CodePilot(tmp_path, tmp_path / "state.db", Registry(model))
    app.new_session()
    runner = SubagentRunner(
        app.models, lambda: "deepseek", [app.toolset.by_name["read_file"]],
        event_sink=lambda event, **data: events.append((event, data)),
    )
    results = runner.run_many([
        {"role": "explore", "task": "读取 demo.txt"},
        {"role": "review", "task": "检查 demo.txt"},
    ])
    assert len(results) == 2
    assert all(item["status"] == "completed" for item in results)
    assert all("hello" in item["summary"] for item in results)
    assert sum(event == "subagent_start" for event, _ in events) == 2
    assert sum(event == "subagent_tool" for event, _ in events) == 2
    assert app.history() == []
    app.close()


def test_exposed_reasoning_returns_empty_when_provider_has_none():
    assert exposed_reasoning(AIMessage(content="answer")) == ""


def test_qwen_adapter_enables_provider_reasoning(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("QWEN_ENABLE_THINKING", "true")
    model = ModelRegistry().get("qwen")
    assert model._llm_type == "chat-qwen"
    assert model.enable_thinking is True
    assert model._default_params["extra_body"]["enable_thinking"] is True
