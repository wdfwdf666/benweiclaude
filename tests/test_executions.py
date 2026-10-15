from __future__ import annotations

from langchain_core.messages import AIMessage
from test_agent import FakeRegistry

from codepilot.agent import CodePilot
from codepilot.executions import ToolExecutionJournal


class CountingTool:
    def __init__(self):
        self.calls = 0

    def invoke(self, arguments):
        self.calls += 1
        return f"value {self.calls}"


def test_successful_execution_is_replayed_without_running_again(tmp_path):
    journal = ToolExecutionJournal(tmp_path / "state.db")
    first = journal.begin("session", "call-1", "run_command", {"command": "echo once"})
    assert first["action"] == "execute"
    journal.finish("session", "call-1", succeeded=True, result="exit code 0\nonce")

    replay = journal.begin("session", "call-1", "run_command", {"command": "echo once"})

    assert replay == {
        "action": "replay", "status": "succeeded", "result": "exit code 0\nonce",
    }
    journal.close()


def test_interrupted_execution_becomes_uncertain_and_is_not_restarted(tmp_path):
    db = tmp_path / "state.db"
    first = ToolExecutionJournal(db)
    assert first.begin("session", "call-1", "mcp__remote__create", {"name": "one"})["action"] == "execute"
    first.close()

    restarted = ToolExecutionJournal(db)
    decision = restarted.begin(
        "session", "call-1", "mcp__remote__create", {"name": "one"},
    )

    assert decision["action"] == "uncertain"
    assert "not executed again" in decision["result"]
    assert restarted.list("session")[0]["status"] == "uncertain"
    restarted.close()


def test_reused_call_id_with_different_arguments_is_rejected(tmp_path):
    journal = ToolExecutionJournal(tmp_path / "state.db")
    journal.begin("session", "call-1", "write_file", {"path": "a", "content": "one"})

    try:
        journal.begin("session", "call-1", "write_file", {"path": "a", "content": "two"})
    except ValueError as exc:
        assert "collision" in str(exc)
    else:
        raise AssertionError("Expected call ID collision")
    journal.close()


def test_agent_reuses_journaled_result_without_reinvoking_tool(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    app.new_session()
    tool = CountingTool()
    app.toolset.by_name["read_file"] = tool
    state = {"messages": [AIMessage(content="", tool_calls=[{
        "name": "read_file", "args": {"path": "demo.txt"}, "id": "read-once",
    }])]}

    with app.leases.lease(app.session_id):
        first = app._call_tools(state)
        second = app._call_tools(state)

    assert tool.calls == 1
    assert first["messages"][0].content == "value 1"
    assert second["messages"][0].content == "value 1"
    app.close()


def test_agent_does_not_run_tool_with_uncertain_prior_execution(tmp_path):
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry())
    session_id = app.new_session()
    tool = CountingTool()
    app.toolset.by_name["read_file"] = tool
    arguments = {"path": "demo.txt"}
    app.executions.begin(session_id, "read-uncertain", "read_file", arguments)
    state = {"messages": [AIMessage(content="", tool_calls=[{
        "name": "read_file", "args": arguments, "id": "read-uncertain",
    }])]}

    with app.leases.lease(app.session_id):
        result = app._call_tools(state)

    assert tool.calls == 0
    assert "not executed again" in result["messages"][0].content
    assert app.executions.list(session_id)[0]["status"] == "uncertain"
    app.close()
