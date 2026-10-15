from __future__ import annotations

import json
import socket
import subprocess
import sys

import pytest

from codepilot.memory import MemoryManager
from codepilot.tools import ToolSet


class DummyRegistry:
    pass


def make_tools(tmp_path, *, memory=None):
    return ToolSet(
        tmp_path, DummyRegistry(), lambda: "session-1", lambda: "deepseek",
        enable_delegation=False, enable_mcp=False, memory=memory,
    )


def test_workspace_file_search_and_git_tools(tmp_path):
    tools = make_tools(tmp_path).by_name

    assert "empty" in tools["list_directory"].invoke({}).lower()
    assert "Wrote" in tools["write_file"].invoke({"path": "src/a.txt", "content": "hello\nneedle"})
    assert "1: hello" in tools["read_file"].invoke({"path": "src/a.txt"})
    assert "Edited" in tools["edit_file"].invoke({
        "path": "src/a.txt", "old": "hello", "new": "world",
    })
    tools["append_file"].invoke({"path": "src/a.txt", "content": "\nlast"})
    assert "world" in (tmp_path / "src" / "a.txt").read_text(encoding="utf-8")
    assert "Created" in tools["make_directory"].invoke({"path": "out"})
    assert "Copied" in tools["copy_file"].invoke({"source": "src/a.txt", "destination": "out/b.txt"})
    assert "Moved" in tools["move_file"].invoke({"source": "out/b.txt", "destination": "out/c.txt"})
    glob_output = tools["glob_files"].invoke({"pattern": "**/*.txt"}).replace("\\", "/")
    grep_output = tools["grep_code"].invoke({"pattern": "needle", "glob": "*.txt"}).replace("\\", "/")
    assert "src/a.txt" in glob_output
    assert "src/a.txt:2" in grep_output
    assert "file" in tools["file_info"].invoke({"path": "out/c.txt"})
    directory_output = tools["list_directory"].invoke({"path": "src"}).replace("\\", "/")
    assert "FILE src/a.txt" in directory_output
    assert "Deleted" in tools["delete_file"].invoke({"path": "out/c.txt"})

    # Git helpers return diagnostic text even outside a repository.
    assert tools["git_status"].invoke({})
    assert tools["git_diff"].invoke({})
    assert tools["git_log"].invoke({"count": 2})
    assert tools["git_show"].invoke({"revision": "HEAD"})


def test_file_tool_validation_and_command_execution(tmp_path):
    tools = make_tools(tmp_path).by_name
    (tmp_path / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "duplicate.txt").write_text("x x", encoding="utf-8")

    with pytest.raises(ValueError, match="workspace"):
        tools["read_file"].invoke({"path": "../outside.txt"})
    with pytest.raises(ValueError, match="Credential"):
        tools["read_file"].invoke({"path": ".env"})
    with pytest.raises(ValueError, match="occurs 2 times"):
        tools["edit_file"].invoke({"path": "duplicate.txt", "old": "x", "new": "y"})
    with pytest.raises(ValueError, match="workspace-relative"):
        tools["glob_files"].invoke({"pattern": "../*.txt"})
    with pytest.raises(ValueError, match="Only existing files"):
        tools["delete_file"].invoke({"path": "missing.txt"})

    command = subprocess.list2cmdline([sys.executable, "-c", "print(12345)"])
    output = tools["run_command"].invoke({"command": command, "timeout_seconds": 5})
    assert "exit code 0" in output
    assert "12345" in output


def test_planning_skills_and_legacy_todo_tools(tmp_path):
    skill = tmp_path / ".benwei" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Review carefully", encoding="utf-8")
    tools = make_tools(tmp_path).by_name

    assert tools["list_skills"].invoke({}) == "review"
    assert tools["read_skill"].invoke({"name": "review"}) == "Review carefully"
    with pytest.raises(ValueError, match="Invalid skill"):
        tools["read_skill"].invoke({"name": "../review"})
    assert "Approved plan" in tools["propose_plan"].invoke({"markdown": "# Plan"})
    assert tools["todo_read"].invoke({}) == "[]"
    assert "Saved 1" in tools["todo_write"].invoke({
        "items_json": json.dumps([{"content": "test"}]),
    })
    assert "test" in tools["todo_read"].invoke({})


def test_structured_task_and_memory_tools(tmp_path):
    memory = MemoryManager(tmp_path / "state.db", tmp_path)
    tools = make_tools(tmp_path, memory=memory).by_name

    created = json.loads(tools["todo_create"].invoke({"content": "implement tests"}))
    assert created["status"] == "pending"
    updated = json.loads(tools["todo_update"].invoke({
        "task_id": created["id"], "status": "in_progress", "evidence_json": "[]",
    }))
    assert updated["status"] == "in_progress"
    completed = json.loads(tools["todo_complete"].invoke({
        "task_id": created["id"], "evidence": "pytest passed",
    }))
    assert completed["status"] == "completed"
    assert "implement tests" in tools["todo_list"].invoke({})

    saved = json.loads(tools["memory_propose"].invoke({
        "memory_type": "fact", "content": "uses sqlite", "related_files_json": "[]",
    }))
    assert "uses sqlite" in tools["memory_search"].invoke({"query": "sqlite"})
    assert "uses sqlite" in tools["memory_list"].invoke({})
    changed = json.loads(tools["memory_update"].invoke({
        "memory_id": saved["id"], "content": "uses sqlite checkpoints",
    }))
    assert changed["content"] == "uses sqlite checkpoints"
    assert "Invalidated" in tools["memory_forget"].invoke({"memory_id": changed["id"]})
    assert "Removed task" in tools["todo_remove"].invoke({"task_id": created["id"]})
    memory.close()


def test_web_search_and_fetch_boundaries(tmp_path, monkeypatch):
    tools = make_tools(tmp_path).by_name

    class SearchClient:
        def text(self, query, max_results):
            return [{"title": "Result", "href": "https://example.com", "body": query}]

    monkeypatch.setattr("codepilot.tools.DDGS", SearchClient)
    assert "Result" in tools["web_search"].invoke({"query": "codepilot", "max_results": 2})

    with pytest.raises(ValueError, match="public HTTP"):
        tools["web_fetch"].invoke({"url": "file:///etc/passwd"})
    with pytest.raises(ValueError, match="Private"):
        tools["web_fetch"].invoke({"url": "http://127.0.0.1/secret"})

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def raise_for_status(self):
            return None

        def iter_content(self, size):
            yield b"<html><body><nav>skip</nav><main>Hello web</main></body></html>"

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
    ])
    monkeypatch.setattr("codepilot.tools.requests.get", lambda *args, **kwargs: Response())
    assert tools["web_fetch"].invoke({"url": "https://example.com"}) == "Hello web"
