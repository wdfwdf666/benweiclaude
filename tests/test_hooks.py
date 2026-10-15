from __future__ import annotations

import json
import sys

from langchain_core.messages import AIMessage, ToolMessage

from codepilot.agent import CodePilot
from codepilot.cli import command, finish_turn
from codepilot.subagents import SubagentRunner


class FakeModel:
    def __init__(self, tool_call=None):
        self.tool_call = tool_call
        self.seen = []

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        if self.tool_call and not any(isinstance(item, ToolMessage) for item in messages):
            return AIMessage(content="", tool_calls=[self.tool_call])
        return AIMessage(content="done")


class FakeRegistry:
    def __init__(self, tool_call=None):
        self.models = {name: FakeModel(tool_call if name == "deepseek" else None)
                       for name in ("deepseek", "qwen", "openai")}

    def get(self, alias):
        return self.models[alias]


def configure(tmp_path, events, script):
    root = tmp_path / ".codepilot"
    root.mkdir(exist_ok=True)
    config = {"hooks": {
        event: [{"matcher": matcher, "hooks": [{
            "type": "command", "command": sys.executable, "args": [str(script)], "timeout": 5,
        }]}] for event, matcher in events.items()
    }}
    (root / "hooks.json").write_text(json.dumps(config), encoding="utf-8")


def test_lifecycle_and_model_switch_hooks(tmp_path, capsys):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json,sys,pathlib\n"
        "event=json.load(sys.stdin)['hook_event_name']\n"
        "with pathlib.Path('events.log').open('a') as f: f.write(event+'\\n')\n",
        encoding="utf-8",
    )
    configure(tmp_path, {name: "*" for name in (
        "SessionStart", "UserPromptSubmit", "Stop", "PreModelSwitch", "PostModelSwitch", "SessionEnd",
    )}, script)
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    app.new_session()
    app.send("hello")
    app.switch_model("qwen")
    command(app, "/hooks")
    assert "SessionStart" in capsys.readouterr().out
    app.close()
    assert (tmp_path / "events.log").read_text(encoding="utf-8").splitlines() == [
        "SessionStart", "UserPromptSubmit", "Stop", "PreModelSwitch", "PostModelSwitch", "SessionEnd",
    ]


def test_pretool_hook_blocks_approved_write(tmp_path):
    script = tmp_path / "deny.py"
    script.write_text(
        "import json,sys\n"
        "data=json.load(sys.stdin)\n"
        "assert data['tool_name']=='write_file'\n"
        "print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny', "
        "'permissionDecisionReason': 'protected by hook'}}))\n",
        encoding="utf-8",
    )
    configure(tmp_path, {"PreToolUse": "write_file"}, script)
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "created"}, "id": "write-1"}
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(call), workspace_trusted=True)
    app.new_session()
    assert "__interrupt__" in app.send("create it")
    app.approve({"write-1": True})
    assert not (tmp_path / "demo.txt").exists()
    assert any("protected by hook" in item.content for item in app.history()
               if isinstance(item, ToolMessage))
    app.close()


def test_posttool_feedback_reaches_model(tmp_path):
    script = tmp_path / "feedback.py"
    script.write_text(
        "import json,sys\n"
        "assert json.load(sys.stdin)['hook_event_name']=='PostToolUse'\n"
        "print(json.dumps({'additionalContext': 'lint check passed'}))\n",
        encoding="utf-8",
    )
    configure(tmp_path, {"PostToolUse": "read_file"}, script)
    (tmp_path / "demo.txt").write_text("hello", encoding="utf-8")
    call = {"name": "read_file", "args": {"path": "demo.txt"}, "id": "read-1"}
    registry = FakeRegistry(call)
    app = CodePilot(tmp_path, tmp_path / "state.db", registry, workspace_trusted=True)
    app.new_session()
    app.send("read")
    assert any("lint check passed" in item.content for item in app.history()
               if isinstance(item, ToolMessage))
    assert any("lint check passed" in item.content for item in registry.models["deepseek"].seen[-1]
               if isinstance(item, ToolMessage))
    app.close()


def test_permission_hook_denies_before_prompt_and_resume(tmp_path, monkeypatch):
    script = tmp_path / "deny_permission.py"
    script.write_text("import sys\nsys.stderr.write('policy denied')\nsys.exit(2)\n", encoding="utf-8")
    configure(tmp_path, {"PermissionRequest": "write_file"}, script)
    call = {"name": "write_file", "args": {"path": "demo.txt", "content": "created"}, "id": "write-1"}
    db = tmp_path / "state.db"
    first = CodePilot(tmp_path, db, FakeRegistry(call), workspace_trusted=True)
    session_id = first.new_session()
    first.send("create")
    first.close()
    second = CodePilot(tmp_path, db, FakeRegistry(call), workspace_trusted=True)
    second.resume(session_id)
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(AssertionError("approval prompt ran")))
    snapshot = second.graph.get_state(second.config())
    finish_turn(second, {"__interrupt__": snapshot.tasks[0].interrupts})
    assert not (tmp_path / "demo.txt").exists()
    assert any("Denied by user" in item.content for item in second.history()
               if isinstance(item, ToolMessage))
    second.close()


def test_stop_hook_requests_one_more_model_pass(tmp_path):
    script = tmp_path / "stop.py"
    script.write_text(
        "import json,sys,pathlib\n"
        "assert json.load(sys.stdin)['hook_event_name']=='Stop'\n"
        "counter=pathlib.Path('stop-count.txt')\n"
        "count=int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count+1))\n"
        "if count==0: print(json.dumps({'decision':'block','reason':'please verify once more'}))\n",
        encoding="utf-8",
    )
    configure(tmp_path, {"Stop": "*"}, script)
    registry = FakeRegistry()
    app = CodePilot(tmp_path, tmp_path / "state.db", registry, workspace_trusted=True)
    app.new_session()
    app.send("hello")
    assert len(registry.models["deepseek"].seen) == 2
    assert any("please verify once more" in item.content for item in app.history())
    assert (tmp_path / "stop-count.txt").read_text() == "2"
    app.close()


def test_subagent_start_hook_blocks_delegation(tmp_path):
    script = tmp_path / "deny_agent.py"
    script.write_text(
        "import json,sys\n"
        "data=json.load(sys.stdin)\n"
        "assert data['agent_type']=='explore'\n"
        "print(json.dumps({'decision':'block','reason':'agent budget reached'}))\n",
        encoding="utf-8",
    )
    configure(tmp_path, {"SubagentStart": "explore"}, script)
    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    app.new_session()
    runner = SubagentRunner(app.models, lambda: "deepseek", [], event_sink=app._emit)
    result = runner.run_one("investigate", "explore")
    assert result["status"] == "failed"
    assert "agent budget reached" in result["summary"]
    app.close()


def test_hook_receives_only_allowlisted_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEPILOT_HOOK_SECRET", "visible")
    script = tmp_path / "environment.py"
    script.write_text(
        "import json,os\n"
        "print(json.dumps({'additionalContext': os.getenv('CODEPILOT_HOOK_SECRET', 'missing')}))\n",
        encoding="utf-8",
    )
    root = tmp_path / ".codepilot"
    root.mkdir()
    handler = {
        "type": "command", "command": sys.executable, "args": [str(script)],
        "envAllowlist": ["CODEPILOT_HOOK_SECRET"],
    }
    (root / "hooks.json").write_text(json.dumps({
        "hooks": {"UserPromptSubmit": [{"matcher": "*", "hooks": [handler]}]},
    }), encoding="utf-8")

    app = CodePilot(tmp_path, tmp_path / "state.db", FakeRegistry(), workspace_trusted=True)
    app.new_session()
    app.send("hello")
    seen = app.models.models["deepseek"].seen[0]
    assert any("visible" in str(message.content) for message in seen)
    app.close()
