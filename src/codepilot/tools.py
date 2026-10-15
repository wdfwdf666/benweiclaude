"""Workspace, web, Git, planning, skill and delegation tools."""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from langchain_core.tools import BaseTool, tool

from .coding_agents import CodingCoordinator
from .mcp import McpManager
from .sandbox import SandboxPolicy, SandboxRunner, parse_command
from .subagents import SubagentRunner

if TYPE_CHECKING:
    from .memory import MemoryManager
    from .models import ModelRegistry

MAX_OUTPUT = 16_000
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".codepilot"}


def is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return (name == ".env" or name.startswith(".env.") and name != ".env.example"
            or name in {"id_rsa", "id_ed25519"} or path.suffix.lower() in {".pem", ".key"})


def clipped(value: str, limit: int = MAX_OUTPUT) -> str:
    return value if len(value) <= limit else value[:limit] + "\n... [truncated]"


class ToolSet:
    """Tools are bound to one CLI workspace and current session."""

    def __init__(self, workspace: Path, models: ModelRegistry, session_id_getter, model_getter,
                  event_sink=None, write_scope=None, enable_delegation: bool = True,
                  internal_paths=None, enable_mcp: bool = True,
                  memory: MemoryManager | None = None, trust_validator=None) -> None:
        self.workspace = workspace.resolve()
        self.models = models
        self.session_id_getter = session_id_getter
        self.model_getter = model_getter
        self.event_sink = event_sink
        self.write_scope = write_scope
        self.enable_delegation = enable_delegation
        self.memory = memory
        self.trust_validator = trust_validator
        self.sandbox = SandboxRunner()
        self.mcp = McpManager(
            self.workspace, event_sink=event_sink, trust_validator=trust_validator,
        ) if enable_mcp else None
        self.coding = CodingCoordinator(self.workspace, models, session_id_getter, model_getter,
                                        event_sink, internal_paths=internal_paths) if enable_delegation else None
        self.tools = self._build()
        self.by_name = {item.name: item for item in self.tools}

    def reload_mcp(self) -> None:
        if self.mcp is not None:
            self.mcp.reload()
        self.tools = self._build()
        self.by_name = {item.name: item for item in self.tools}

    def close(self) -> None:
        if self.mcp is not None:
            self.mcp.close()

    def set_mcp_enabled(self, enabled: bool) -> None:
        """Enable MCP only after workspace trust has been established."""
        if enabled and self.mcp is None:
            self.mcp = McpManager(
                self.workspace, event_sink=self.event_sink,
                trust_validator=self.trust_validator,
            )
        elif enabled:
            self.mcp.reload()
        elif not enabled:
            if self.mcp is not None:
                self.mcp.close()
            self.mcp = None
        self.tools = self._build()
        self.by_name = {item.name: item for item in self.tools}

    def path(self, value: str) -> Path:
        resolved = (self.workspace / value).resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("Path must remain inside the workspace")
        if {".git", ".codepilot", ".venv"}.intersection(resolved.relative_to(self.workspace).parts):
            raise ValueError("Internal workspace directories are not exposed to file tools")
        if is_secret_file(resolved):
            raise ValueError("Credential files are not exposed to the assistant")
        return resolved

    def display(self, path: Path) -> str:
        return str(path.relative_to(self.workspace)) or "."

    def write_path(self, value: str) -> Path:
        path = self.path(value)
        if self.write_scope is not None:
            relative = path.relative_to(self.workspace).as_posix()
            if not any(relative == item["path"] or item["kind"] == "directory"
                       and relative.startswith(item["path"] + "/") for item in self.write_scope):
                raise ValueError(f"Path is outside the subagent's write scope: {relative}")
        return path

    def state_path(self, name: str) -> Path:
        state_dir = self.workspace / ".codepilot"
        state_dir.mkdir(exist_ok=True)
        return state_dir / name

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15, shell=False,
        )
        return clipped(result.stdout + result.stderr) or f"exit code {result.returncode}"

    def _build(self) -> list[BaseTool]:
        owner = self

        @tool
        def list_directory(path: str = ".") -> str:
            """List files and directories at a workspace path."""
            folder = owner.path(path)
            if not folder.is_dir():
                raise ValueError("Not a directory")
            return "\n".join(
                f"{'DIR ' if p.is_dir() else 'FILE'} {owner.display(p)}"
                for p in sorted(folder.iterdir())[:200]
            ) or "(empty)"

        @tool
        def read_file(path: str, start_line: int = 1, max_lines: int = 200) -> str:
            """Read a UTF-8 workspace file with line numbers and an optional line range."""
            p = owner.path(path)
            lines = p.read_text(encoding="utf-8").splitlines()
            start = max(start_line, 1) - 1
            return clipped("\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines[start:start + min(max_lines, 500)], start)))

        @tool
        def write_file(path: str, content: str) -> str:
            """Create or replace a UTF-8 workspace file. Requires approval."""
            p = owner.write_path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Wrote {owner.display(p)}"

        @tool
        def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
            """Replace exact text in a workspace file. Requires approval."""
            p = owner.write_path(path)
            content = p.read_text(encoding="utf-8")
            count = content.count(old)
            if not old or count == 0:
                raise ValueError("Old text not found")
            if count > 1 and not replace_all:
                raise ValueError(f"Old text occurs {count} times; set replace_all=true or use a longer match")
            p.write_text(content.replace(old, new, -1 if replace_all else 1), encoding="utf-8")
            return f"Edited {owner.display(p)} ({count if replace_all else 1} replacement(s))"

        @tool
        def append_file(path: str, content: str) -> str:
            """Append UTF-8 text to a workspace file. Requires approval."""
            p = owner.write_path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as handle:
                handle.write(content)
            return f"Appended to {owner.display(p)}"

        @tool
        def make_directory(path: str) -> str:
            """Create a workspace directory. Requires approval."""
            p = owner.write_path(path)
            p.mkdir(parents=True, exist_ok=True)
            return f"Created {owner.display(p)}"

        @tool
        def delete_file(path: str) -> str:
            """Delete one workspace file, never a directory. Requires approval."""
            p = owner.write_path(path)
            if not p.is_file():
                raise ValueError("Only existing files can be deleted")
            p.unlink()
            return f"Deleted {owner.display(p)}"

        @tool
        def move_file(source: str, destination: str) -> str:
            """Move a workspace file to a new workspace path. Requires approval."""
            src, dst = owner.write_path(source), owner.write_path(destination)
            if not src.is_file() or dst.exists():
                raise ValueError("Source must be a file and destination must not exist")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return f"Moved {owner.display(src)} to {owner.display(dst)}"

        @tool
        def copy_file(source: str, destination: str) -> str:
            """Copy a workspace file to a new workspace path. Requires approval."""
            src, dst = owner.path(source), owner.write_path(destination)
            if not src.is_file() or dst.exists():
                raise ValueError("Source must be a file and destination must not exist")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return f"Copied {owner.display(src)} to {owner.display(dst)}"

        @tool
        def glob_files(pattern: str) -> str:
            """Find workspace files using a glob pattern, for example **/*.py."""
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Use a workspace-relative glob")
            matches = [p for p in owner.workspace.glob(pattern) if p.is_file() and not is_secret_file(p) and not SKIP_DIRS.intersection(p.relative_to(owner.workspace).parts)]
            return "\n".join(owner.display(p) for p in matches[:200]) or "No matches"

        @tool
        def grep_code(pattern: str, glob: str = "*", max_results: int = 100) -> str:
            """Search workspace text files with a regular expression and filename glob."""
            regex = re.compile(pattern)
            matches: list[str] = []
            cap = max(1, min(max_results, 200))
            for p in owner.workspace.rglob("*"):
                if not p.is_file() or is_secret_file(p) or SKIP_DIRS.intersection(p.relative_to(owner.workspace).parts):
                    continue
                if not fnmatch.fnmatch(p.name, glob) or p.stat().st_size > 1_000_000:
                    continue
                try:
                    with p.open(encoding="utf-8") as handle:
                        for number, line in enumerate(handle, 1):
                            if regex.search(line):
                                matches.append(f"{owner.display(p)}:{number}: {line.rstrip()[:300]}")
                                if len(matches) >= cap:
                                    return clipped("\n".join(matches))
                except (UnicodeError, OSError):
                    continue
            return clipped("\n".join(matches)) or "No matches"

        @tool
        def file_info(path: str) -> str:
            """Get size and modification time for a workspace path."""
            p = owner.path(path)
            stat = p.stat()
            return f"{owner.display(p)}: {'directory' if p.is_dir() else 'file'}, {stat.st_size} bytes, modified {stat.st_mtime} (Unix seconds)"

        @tool
        def git_status() -> str:
            """Show concise Git working tree status."""
            return owner._git("status", "--short")

        @tool
        def git_diff(path: str = "") -> str:
            """Show unstaged Git diff, optionally for a workspace path."""
            return owner._git("diff", "--", path) if path else owner._git("diff")

        @tool
        def git_log(count: int = 5) -> str:
            """Show recent Git commits."""
            return owner._git("log", f"-{max(1, min(count, 20))}", "--oneline")

        @tool
        def git_show(revision: str = "HEAD") -> str:
            """Show a Git commit summary or revision."""
            return owner._git("show", "--stat", "--oneline", revision)

        @tool
        def run_command(command: str, timeout_seconds: int = 30) -> str:
            """Run a shell command in the workspace. Requires approval."""
            policy = SandboxPolicy(
                owner.workspace,
                timeout_seconds=max(1, min(timeout_seconds, 120)),
                max_output_bytes=MAX_OUTPUT,
            )
            return owner.sandbox.run(parse_command(command), policy).render()

        @tool
        def web_search(query: str, max_results: int = 5) -> str:
            """Search the public web and return titles, URLs and snippets."""
            results = DDGS().text(query, max_results=max(1, min(max_results, 10)))
            return clipped(json.dumps(list(results), ensure_ascii=False, indent=2))

        @tool
        def web_fetch(url: str) -> str:
            """Fetch readable text from a public HTTP or HTTPS webpage."""
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("A public HTTP(S) URL is required")
            import ipaddress
            import socket

            addresses = socket.getaddrinfo(parsed.hostname, None)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError("Private and local addresses are blocked")
            with requests.get(url, timeout=15, headers={"User-Agent": "benwei/0.1"}, stream=True, allow_redirects=False) as response:
                response.raise_for_status()
                if 300 <= response.status_code < 400:
                    raise ValueError("Redirects are not followed")
                chunks = []
                size = 0
                for chunk in response.iter_content(8192):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= 200_000:
                        break
                raw = b"".join(chunks)[:200_000]
            soup = BeautifulSoup(raw, "html.parser")
            for node in soup(["script", "style", "nav", "footer"]):
                node.decompose()
            return clipped(soup.get_text(" ", strip=True))

        @tool
        def todo_read() -> str:
            """读取当前会话的结构化任务列表。"""
            if owner.memory is not None:
                return json.dumps(
                    owner.memory.list_tasks(owner.session_id_getter()), ensure_ascii=False, indent=2
                )
            p = owner.state_path(f"todo-{owner.session_id_getter()}.json")
            return p.read_text(encoding="utf-8") if p.exists() else "[]"

        @tool
        def todo_write(items_json: str) -> str:
            """兼容旧接口：用 JSON 数组整体替换当前会话任务列表。"""
            items = json.loads(items_json)
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("Expected a JSON array of task objects")
            if owner.memory is not None:
                saved = owner.memory.replace_tasks(owner.session_id_getter(), items)
                return f"Saved {len(saved)} tasks"
            p = owner.state_path(f"todo-{owner.session_id_getter()}.json")
            p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            return f"Saved {len(items)} tasks"

        @tool
        def todo_create(content: str, dependencies_json: str = "[]") -> str:
            """创建一项当前会话任务，初始状态为 pending。"""
            if owner.memory is None:
                raise ValueError("Structured task memory is unavailable")
            task = owner.memory.create_task(
                owner.session_id_getter(), content, json.loads(dependencies_json)
            )
            return json.dumps(task, ensure_ascii=False)

        @tool
        def todo_update(
            task_id: str, status: str = "", content: str = "", evidence_json: str = "",
        ) -> str:
            """按任务 ID 更新内容、pending/in_progress/completed/blocked 状态或证据。"""
            if owner.memory is None:
                raise ValueError("Structured task memory is unavailable")
            task = owner.memory.update_task(
                owner.session_id_getter(), task_id,
                status=status or None, content=content or None,
                evidence=json.loads(evidence_json) if evidence_json else None,
            )
            return json.dumps(task, ensure_ascii=False)

        @tool
        def todo_complete(task_id: str, evidence: str = "") -> str:
            """把当前会话中的一项任务标记为 completed，可附带完成证据。"""
            if owner.memory is None:
                raise ValueError("Structured task memory is unavailable")
            current = owner.memory.get_task(owner.session_id_getter(), task_id)
            if current is None:
                raise ValueError("Task not found or prefix is ambiguous")
            evidence_items = list(current["evidence"])
            if evidence:
                evidence_items.append(evidence)
            task = owner.memory.update_task(
                owner.session_id_getter(), current["id"], status="completed", evidence=evidence_items,
            )
            return json.dumps(task, ensure_ascii=False)

        @tool
        def todo_list(status: str = "") -> str:
            """列出当前会话任务，可按状态筛选。"""
            if owner.memory is None:
                return todo_read.invoke({})
            return json.dumps(
                owner.memory.list_tasks(owner.session_id_getter(), status or None),
                ensure_ascii=False, indent=2,
            )

        @tool
        def todo_remove(task_id: str) -> str:
            """从当前会话任务列表删除一项任务。"""
            if owner.memory is None:
                raise ValueError("Structured task memory is unavailable")
            task = owner.memory.remove_task(owner.session_id_getter(), task_id)
            return f"Removed task {task['id'][:8]}: {task['content']}"

        @tool
        def memory_search(query: str, limit: int = 5) -> str:
            """检索可追溯的会话、项目和用户长期记忆；代码事实仍需读取文件核实。"""
            if owner.memory is None:
                return "[]"
            return json.dumps(
                owner.memory.retrieve(query, owner.session_id_getter(), limit),
                ensure_ascii=False, indent=2,
            )

        @tool
        def memory_list(memory_type: str = "", include_invalidated: bool = False) -> str:
            """列出长期记忆，可按 architecture/decision/preference/failure/command/fact/note 筛选。"""
            if owner.memory is None:
                return "[]"
            return json.dumps(
                owner.memory.list_memories(
                    memory_type=memory_type or None, include_invalidated=include_invalidated,
                ), ensure_ascii=False, indent=2,
            )

        @tool
        def memory_propose(
            memory_type: str, content: str, related_files_json: str = "[]",
            scope: str = "project", importance: float = 0.5, confidence: float = 0.8,
        ) -> str:
            """提议保存一条长期记忆；需要审批，并应提供来源文件和保守可信度。"""
            if owner.memory is None:
                raise ValueError("Long-term memory is unavailable")
            item = owner.memory.save(
                scope=scope, memory_type=memory_type, content=content,
                source_session_id=owner.session_id_getter(),
                related_files=json.loads(related_files_json), importance=importance,
                confidence=confidence,
            )
            return json.dumps(item, ensure_ascii=False)

        @tool
        def memory_update(memory_id: str, content: str, related_files_json: str = "") -> str:
            """更新一条长期记忆；需要审批。"""
            if owner.memory is None:
                raise ValueError("Long-term memory is unavailable")
            item = owner.memory.update(
                memory_id, content,
                json.loads(related_files_json) if related_files_json else None,
            )
            return json.dumps(item, ensure_ascii=False)

        @tool
        def memory_forget(memory_id: str, reason: str = "用户要求遗忘") -> str:
            """使一条长期记忆失效但保留审计记录；需要审批。"""
            if owner.memory is None:
                raise ValueError("Long-term memory is unavailable")
            item = owner.memory.forget(memory_id, reason)
            return f"Invalidated memory {item['id'][:8]}"

        @tool
        def propose_plan(markdown: str) -> str:
            """Present a Markdown plan for human approval, then save the approved plan."""
            p = owner.state_path(f"plan-{owner.session_id_getter()}.md")
            p.write_text(markdown, encoding="utf-8")
            return f"Approved plan saved to {owner.display(p)}"

        @tool
        def list_skills() -> str:
            """List local project skills in .benwei/skills/*/SKILL.md."""
            folder = owner.workspace / ".benwei" / "skills"
            return "\n".join(p.parent.name for p in sorted(folder.glob("*/SKILL.md"))) or "No local skills"

        @tool
        def read_skill(name: str) -> str:
            """Load the instructions for a named local project skill."""
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Invalid skill name")
            p = owner.workspace / ".benwei" / "skills" / name / "SKILL.md"
            return clipped(p.read_text(encoding="utf-8"))

        base_tools = [
            list_directory, read_file, write_file, edit_file, append_file,
            make_directory, delete_file, move_file, copy_file, glob_files,
            grep_code, file_info, git_status, git_diff, git_log, git_show,
            run_command, web_search, web_fetch, todo_read, todo_write,
            propose_plan, list_skills, read_skill,
        ]
        if owner.memory is not None:
            base_tools.extend([
                todo_create, todo_update, todo_complete, todo_list, todo_remove,
                memory_search, memory_list, memory_propose, memory_update, memory_forget,
            ])
        if owner.mcp is not None:
            base_tools.extend(owner.mcp.as_langchain_tools())
        if not owner.enable_delegation:
            return base_tools

        readonly_tools = [
            list_directory, read_file, glob_files, grep_code, file_info,
            git_status, git_diff, git_log, git_show, web_search, web_fetch,
            todo_read, list_skills, read_skill,
        ]
        if owner.memory is not None:
            readonly_tools.extend([todo_list, memory_search, memory_list])

        runner = SubagentRunner(owner.models, owner.model_getter, readonly_tools, owner.event_sink)

        @tool
        def delegate_agent(task: str, role: str = "explore") -> str:
            """Delegate one read-only investigation to an explore, review, or plan subagent."""
            result = runner.run_one(task, role)
            return json.dumps(result, ensure_ascii=False)

        @tool
        def delegate_agents(tasks: list[dict[str, str]]) -> str:
            """Run 2-3 independent read-only subagent tasks in parallel; each item has task and role (explore/review/plan)."""
            return json.dumps(runner.run_many(tasks), ensure_ascii=False)

        @tool
        def delegate_coding_tasks(tasks: list[dict]) -> str:
            """Run 1-3 coding subagents in parallel isolated copies. Each task has task and paths (explicit writable files or directories ending /). Returns staged change IDs; no project files are changed yet."""
            return json.dumps(owner.coding.run(tasks), ensure_ascii=False)

        @tool
        def list_coding_tasks() -> str:
            """List staged coding subagent runs for this session, including changed files and status."""
            return json.dumps(owner.coding.list(), ensure_ascii=False)

        @tool
        def review_subagent_changes(task_id: str) -> str:
            """Show the diff and summary from one staged coding subagent before integration."""
            return owner.coding.preview([task_id])

        @tool
        def apply_subagent_changes(task_ids: list[str]) -> str:
            """Apply reviewed coding subagent changes to the project after approval, with conflict checks."""
            return owner.coding.apply(task_ids)

        return base_tools + [delegate_agent, delegate_agents, delegate_coding_tasks,
                             list_coding_tasks, review_subagent_changes, apply_subagent_changes]
