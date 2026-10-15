from __future__ import annotations

import sys

from codepilot.mcp import McpManager, add_stdio_server
from codepilot.permissions import decide
from codepilot.tools import ToolSet


class DummyRegistry:
    pass


def write_fake_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    method=msg.get('method')\n"
        "    if 'id' not in msg:\n"
        "        continue\n"
        "    if method=='initialize':\n"
        "        result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fake','version':'1'}}\n"
        "    elif method=='tools/list':\n"
        "        result={'tools':[{'name':'echo','description':'Echo text','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]}\n"
        "    elif method=='tools/call':\n"
        "        text=msg.get('params',{}).get('arguments',{}).get('text','')\n"
        "        result={'content':[{'type':'text','text':'echo:'+text}]}\n"
        "    else:\n"
        "        result={}\n"
        "    print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    return script


def test_mcp_manager_loads_stdio_tools(tmp_path):
    script = write_fake_server(tmp_path)
    add_stdio_server(tmp_path, "local", sys.executable, [str(script)])

    manager = McpManager(tmp_path)

    assert manager.errors == {}
    assert [tool.public_name for tool in manager.tools] == ["mcp__local__echo"]
    process = manager.clients["local"].process
    assert manager.call("mcp__local__echo", {"text": "hi"}) == "echo:hi"
    assert manager.clients["local"].process is process
    manager.close()


def test_mcp_tools_are_exposed_as_langchain_tools(tmp_path):
    script = write_fake_server(tmp_path)
    add_stdio_server(tmp_path, "local", sys.executable, [str(script)])

    toolset = ToolSet(
        tmp_path, DummyRegistry(), lambda: "session", lambda: "deepseek",
        enable_delegation=False,
    )

    assert "mcp__local__echo" in toolset.by_name
    assert toolset.by_name["mcp__local__echo"].invoke({"text": "hello"}) == "echo:hello"
    toolset.close()


def test_mcp_manager_reconnects_a_dead_server_before_next_call(tmp_path):
    script = write_fake_server(tmp_path)
    add_stdio_server(tmp_path, "local", sys.executable, [str(script)])
    manager = McpManager(tmp_path)
    old_process = manager.clients["local"].process
    assert old_process is not None
    old_process.kill()
    old_process.wait(timeout=2)

    assert manager.call("mcp__local__echo", {"text": "again"}) == "echo:again"
    assert manager.clients["local"].process is not old_process
    manager.close()


def test_mcp_tools_require_approval_and_are_denied_in_plan_mode():
    assert decide("default", "mcp__local__echo").kind == "ask"
    assert decide("plan", "mcp__local__echo").kind == "deny"
