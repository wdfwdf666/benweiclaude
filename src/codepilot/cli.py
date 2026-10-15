"""Interactive entry point and slash commands."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, ToolMessage

from .agent import CodePilot
from .cli_router import SlashCommandRouter
from .mcp import (
    McpManager,
    add_json_server,
    add_stdio_server,
    config_path,
    get_server_config,
    remove_server,
)
from .models import MODEL_ALIASES
from .permissions import PERMISSION_MODES
from .sandbox import SandboxRunner
from .trust import WorkspaceTrustStore, describe_executable_configs, executable_configs


class ConsoleTrace:
    """Render observable graph activity while the turn is still running."""

    def __init__(self, debug: bool = False, show_reasoning: bool = False) -> None:
        self.debug = debug
        self.show_reasoning = show_reasoning
        self._lock = threading.Lock()
        self._text_open = False
        self._reason_open = False
        self._reason_notified = False

    def _args(self, args: dict) -> str:
        visible = {}
        for key, value in args.items():
            if any(word in key.lower() for word in ("api_key", "token", "password", "secret", "credential", "authorization")):
                visible[key] = "[已隐藏]"
            elif key in {"content", "markdown", "items_json"} and not self.debug:
                visible[key] = f"<{len(str(value))} 字符>"
            else:
                limit = 1200 if self.debug else 240
                value_text = str(value)
                visible[key] = value_text if len(value_text) <= limit else value_text[:limit] + "…"
        return json.dumps(visible, ensure_ascii=False)

    def __call__(self, event: str, data: dict) -> None:
        with self._lock:
            self._render(event, data)

    def _render(self, event: str, data: dict) -> None:
        if event == "model_start":
            self._text_open = False
            self._reason_open = False
            self._reason_notified = False
            print(f"[模型] 正在调用 {data['model']}（上下文 {data['message_count']} 条消息）", flush=True)
        elif event == "model_reasoning_delta":
            if not self.show_reasoning:
                if not self._reason_notified:
                    print("[推理] 模型返回了可见推理；使用 --show-reasoning 可展开", flush=True)
                    self._reason_notified = True
                return
            if self._text_open:
                print(flush=True)
                self._text_open = False
            if not self._reason_open:
                print("[模型推理] ", end="", flush=True)
                self._reason_open = True
            print(data["text"], end="", flush=True)
        elif event == "model_text_delta":
            if self._reason_open:
                print(flush=True)
                self._reason_open = False
            if not self._text_open:
                print("[模型输出] ", end="", flush=True)
                self._text_open = True
            print(data["text"], end="", flush=True)
        elif event == "model_result":
            if self._text_open or self._reason_open:
                print(flush=True)
                self._text_open = False
                self._reason_open = False
            calls = data["tool_calls"]
            if calls:
                note = str(data["content"]).strip()
                if note and not data.get("streamed"):
                    print(f"[行动说明] {note[:1000]}", flush=True)
                for call in calls:
                    print(f"[工具请求] {call['name']} {self._args(call['args'])}", flush=True)
            print(f"[模型完成] {data['seconds']:.2f} 秒", flush=True)
            if self.debug and data.get("usage"):
                print(f"[Token] {data['usage']}", flush=True)
        elif event == "tool_start":
            print(f"[执行工具] {data['name']} {self._args(data['args'])}", flush=True)
        elif event == "tool_result":
            limit = 2000 if self.debug else 300
            result = data["result"][:limit]
            if len(data["result"]) > limit:
                result += "…"
            if not self.debug:
                result = result.replace("\n", "\\n")
            print(f"[工具结果] {data['name']}（{data['seconds']:.2f} 秒）：{result}", flush=True)
        elif event == "tool_replayed":
            print(
                f"[工具幂等] {data['name']} 复用已有 {data['status']} 结果",
                flush=True,
            )
        elif event == "tool_uncertain":
            print(f"[工具幂等] {data['name']} 上次结果不确定，本次未重复执行", flush=True)
        elif event == "tool_denied":
            print(f"[工具跳过] {data['name']}：用户未批准", flush=True)
        elif event == "approval_resume":
            print("[审批] 已收到决定，继续执行", flush=True)
        elif event == "compact_start":
            print(f"[上下文] 正在压缩 {data['before']} 条消息", flush=True)
        elif event == "compact_end":
            print(f"[上下文] 压缩完成：{data['before']} → {data['after']} 条消息", flush=True)
        elif event == "subagent_start":
            print(f"[子Agent {data['id']}] {data['role']} 开始：{data['task'][:150]}", flush=True)
        elif event == "subagent_tool":
            print(f"[子Agent {data['id']}] 调用 {data['name']}", flush=True)
        elif event == "subagent_tool_result":
            print(f"[子Agent {data['id']}] {data['name']} 完成：{data['result'][:180]}", flush=True)
        elif event == "subagent_end":
            print(f"[子Agent {data['id']}] 完成（{data['seconds']:.2f} 秒）", flush=True)
        elif event == "subagent_error":
            print(f"[子Agent {data['id']}] 失败：{data['error']}", flush=True)
        elif event == "hook_start":
            print(f"[Hook] {data['hook_event']} → {data['command']}", flush=True)
        elif event == "hook_result" and data.get("blocked"):
            print(f"[Hook] {data['hook_event']} 已阻止操作", flush=True)
        elif event == "hook_error":
            print(f"[Hook 错误] {data['hook_event']}: {data['error']}", flush=True)
        elif event == "stop_blocked":
            print(f"[Stop Hook] 要求继续（第 {data['attempt']} 次）：{data['reason']}", flush=True)


def load_environment(workspace: Path) -> None:
    """Use workspace credentials first, then the launch directory as a fallback."""
    load_dotenv(workspace / ".env")
    launch_env = Path.cwd().resolve() / ".env"
    if launch_env != workspace / ".env":
        load_dotenv(launch_env)


def show_trust_preview(workspace: Path) -> None:
    print("此工作区包含可启动本地进程的项目配置：")
    for config in describe_executable_configs(workspace):
        print(f"  {config['path']}")
        if config.get("error"):
            print(f"    配置读取失败：{config['error']}")
        for command_item in config.get("commands", []):
            print(f"    {command_item['name']}: {command_item['command']}")


def request_workspace_trust(workspace: Path, store: WorkspaceTrustStore) -> tuple[bool, bool]:
    """Return (trusted for this run, persisted). Default to disabled."""
    if not executable_configs(workspace):
        return True, False
    if store.is_trusted(workspace):
        return True, True
    show_trust_preview(workspace)
    try:
        answer = input("启用这些 Hooks/MCP 命令？[y=仅本次/a=信任并记住/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer in {"a", "always"}:
        store.trust(workspace)
        return True, True
    return answer in {"y", "yes"}, False


def show_sessions(app: CodePilot) -> None:
    rows = app.store.list()
    if not rows:
        print("No sessions yet.")
    for row in rows:
        marker = "*" if row["id"] == app.session_id else " "
        print(f"{marker} {row['id'][:12]}  {row['model']:8}  {row['permission_mode']:11}  {row['updated_at']}  {row['title']}")


def show_history(app: CodePilot) -> None:
    for message in app.history():
        if isinstance(message, ToolMessage):
            print(f"[tool {message.name}] {str(message.content)[:240]}")
        elif isinstance(message, AIMessage):
            if message.tool_calls:
                print("[assistant tools] " + ", ".join(call["name"] for call in message.tool_calls))
            elif message.content:
                print(f"[assistant] {message.content}")
        else:
            print(f"[user] {message.content}")


def preview_change(app: CodePilot, request: dict) -> str:
    """Build a bounded unified diff for a proposed text-file change."""
    name, args = request["name"], request["args"]
    if name == "propose_plan":
        return str(args.get("markdown", ""))[:6000]
    if name == "apply_subagent_changes":
        try:
            return app.toolset.coding.preview(args["task_ids"])
        except (KeyError, OSError, ValueError) as exc:
            return f"无法生成预览：{exc}"
    if name not in {"write_file", "edit_file", "append_file", "delete_file"}:
        return ""
    try:
        path = app.toolset.path(args["path"])
        if path.exists() and path.stat().st_size > 100_000:
            return "文件超过 100 KB，省略预览。"
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        if name == "write_file":
            after = args["content"]
        elif name == "edit_file":
            after = before.replace(args["old"], args["new"], -1 if args.get("replace_all") else 1)
        elif name == "append_file":
            after = before + args["content"]
        else:
            after = ""
        lines = list(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{args['path']}", tofile=f"b/{args['path']}",
        ))
        preview = "".join(lines[:80])[:6000]
        return preview + ("\n... [预览已截断]" if len(lines) > 80 else "")
    except (KeyError, OSError, UnicodeError, ValueError) as exc:
        return f"无法生成预览：{exc}"


def finish_turn(app: CodePilot, result: dict) -> None:
    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        requests = payload.get("requests", [])
        print("\n[等待审批] 以下工具需要确认：")
        decisions = {}
        for request in requests:
            print(f"  {request['name']} {ConsoleTrace()._args(request['args'])}")
            permission_hook = app.hooks.run(
                "PermissionRequest", match=request["name"], session_id=app.session_id,
                tool_name=request["name"], tool_input=request["args"], tool_use_id=request["id"],
            )
            if permission_hook.blocked:
                print(f"[Hook 拒绝] {permission_hook.reason}")
                decisions[request["id"]] = False
                continue
            preview = preview_change(app, request)
            if preview:
                print(f"[变更预览]\n{preview}")
            try:
                answer = input("批准此工具调用？[y/N] ").strip().lower()
            except EOFError:
                answer = "n"
            decisions[request["id"]] = answer in {"y", "yes"}
        result = app.approve(decisions)
    if result.get("messages") and not app.last_answer_streamed:
        last = result["messages"][-1]
        print(f"\n{last.content}\n")


def finish_pending(app: CodePilot) -> None:
    """Offer to resolve a durable approval after /resume or process restart."""
    snapshot = app.graph.get_state(app.config())
    for task in snapshot.tasks:
        if task.interrupts:
            print("This session has a pending tool approval.")
            finish_turn(app, {"__interrupt__": task.interrupts})
            return


def _run_command(app: CodePilot, name: str, value: str) -> bool:
    if name in {"/quit", "/exit"}:
        return False
    if name == "/help":
        print(
            "/new [model]  /resume [id]  /rename <标题>  /model [deepseek|qwen|openai]\n"
            "/mode [default|acceptEdits|plan]  /plan  /status  /context  /compact\n"
            "/rewind [检查点ID]  /agents  /todo  /memory [status|list|search|show|forget|compact]\n"
            "/history  /executions  /skills  /tools  /hooks  /mcp  /sandbox  /trust [status|revoke]  /quit"
        )
    elif name == "/new":
        print(f"New session: {app.new_session(value or app.model_alias, app.permission_mode)}")
    elif name == "/resume":
        if value:
            row = app.resume(value)
            print(f"Resumed {row['id']} ({row['model']}): {row['title']}")
            finish_pending(app)
        else:
            show_sessions(app)
    elif name == "/model":
        if not value:
            print(f"Current: {app.model_alias}; available: {', '.join(MODEL_ALIASES)}")
        else:
            app.switch_model(value)
            print(f"Switched to {value}; conversation history retained.")
    elif name == "/mode":
        if not value:
            print(f"当前权限模式：{app.permission_mode}；可选：{', '.join(PERMISSION_MODES)}")
        else:
            app.switch_mode(value)
            print(f"权限模式已切换为 {value}")
    elif name == "/plan":
        app.switch_mode("plan")
        print("已进入计划模式：可调查并提交计划，不能修改文件或执行命令。")
    elif name == "/rename":
        if not value:
            print("用法：/rename <会话标题>")
        else:
            app.rename_session(value)
            print(f"会话已命名为：{value[:100]}")
    elif name == "/status":
        lease = app.leases.status(app.session_id) if app.session_id else None
        if lease is None:
            lease_text = "空闲"
        elif lease["owned_by_this_process"]:
            lease_text = "由当前进程持有"
        else:
            lease_text = f"由进程 {lease['owner_id'][:8]} 持有"
        print(
            f"会话：{app.session_id}\n模型：{app.model_alias}\n权限：{app.permission_mode}"
            f"\n工作区：{app.workspace}\n项目进程：{'已启用' if app._workspace_trust_valid() else '已禁用'}"
            f"\n会话写租约：{lease_text}"
        )
    elif name == "/context":
        stats = app.context_stats()
        print(
            f"消息：{stats['messages']}；字符：{stats['characters']}；"
            f"粗略 Token：{stats['estimated_tokens']} / {stats['context_limit']}；"
            f"未完成任务：{stats['active_tasks']}；有效长期记忆：{stats['active_memories']}"
        )
    elif name == "/sandbox":
        capabilities = SandboxRunner().capabilities()
        print(
            f"后端：{capabilities.backend}\n"
            f"进程树清理：{'有' if capabilities.process_tree else '无'}\n"
            f"内存限制：{'有' if capabilities.memory_limit else '无'}\n"
            f"进程数限制：{'有' if capabilities.process_limit else '无'}\n"
            f"文件系统强隔离：{'有' if capabilities.filesystem_isolation else '无'}\n"
            f"网络强隔离：{'有' if capabilities.network_isolation else '无'}"
        )
    elif name == "/compact":
        stats = app.compact()
        print(f"上下文：{stats['before']} → {stats['after']} 条消息")
    elif name == "/rewind":
        if not value:
            rows = app.file_checkpoints.list(app.session_id)
            if not rows:
                print("当前会话没有文件检查点。")
            for row in rows[:30]:
                print(f"{row['id'][:12]}  {row['created_at']}  {row['tool_name']}  {', '.join(row['paths'])}")
        else:
            matching = [row for row in app.file_checkpoints.list(app.session_id) if row["id"].startswith(value)]
            if len(matching) != 1:
                raise ValueError("检查点不存在或 ID 前缀不唯一")
            answer = input(f"将文件回退至检查点 {matching[0]['id'][:12]} 之前。确认？[y/N] ").strip().lower()
            if answer in {"y", "yes"}:
                paths = app.rewind_files(value)
                print("已回退文件：" + ", ".join(paths))
            else:
                print("已取消回退。")
    elif name == "/todo":
        tasks = app.memory.list_tasks(app.session_id)
        if not tasks:
            print("当前会话没有任务。")
        symbols = {"pending": "○", "in_progress": "→", "completed": "✓", "blocked": "!"}
        for task in tasks:
            print(f"{symbols[task['status']]} {task['id'][:8]}  {task['content']}")
    elif name == "/memory":
        action, _, argument = value.partition(" ")
        action, argument = action or "status", argument.strip()
        if action == "status":
            status = app.memory.status(app.session_id)
            names = [item for item in ("AGENTS.md", "CLAUDE.md", "CODEPILOT.md")
                     if (app.workspace / item).is_file()]
            snapshot = app.graph.get_state(app.config())
            print("项目指令：" + (", ".join(names) if names else "未找到"))
            print(
                f"长期记忆：{status['active_memories']} 条有效，"
                f"{status['invalidated_memories']} 条已失效；"
                f"任务：{status['tasks']} 条，{status['active_tasks']} 条未完成；"
                f"FTS5：{'可用' if status['fts_enabled'] else '不可用，使用 LIKE 回退'}；"
                f"结构化摘要：{'有' if snapshot.values.get('session_summary') else '无'}"
            )
        elif action == "list":
            rows = app.memory.list_memories(memory_type=argument or None, include_invalidated=True)
            if not rows:
                print("没有长期记忆。")
            for row in rows:
                state = "失效" if row["invalidated_at"] else "有效"
                print(f"{row['id'][:8]}  {state}  {row['memory_type']}  {row['content'][:120]}")
        elif action == "search":
            if not argument:
                print("用法：/memory search <查询>")
            else:
                rows = app.memory.retrieve(argument, app.session_id, limit=10)
                if not rows:
                    print("没有匹配的长期记忆。")
                for row in rows:
                    print(f"{row['id'][:8]}  {row['memory_type']}  {row['content'][:160]}")
        elif action == "show":
            row = app.memory.get(argument)
            if row is None:
                raise ValueError("记忆不存在或 ID 前缀不唯一")
            print(json.dumps(row, ensure_ascii=False, indent=2))
        elif action == "forget":
            row = app.memory.get(argument)
            if row is None:
                raise ValueError("记忆不存在或 ID 前缀不唯一")
            answer = input(f"使记忆 {row['id'][:8]} 失效？[y/N] ").strip().lower()
            print("已失效。" if answer in {"y", "yes"} and app.memory.forget(row["id"]) else "已取消。")
        elif action == "compact":
            stats = app.compact()
            print(f"上下文：{stats['before']} → {stats['after']} 条消息")
        else:
            print("用法：/memory [status|list [类型]|search <查询>|show <ID>|forget <ID>|compact]")
    elif name == "/history":
        show_history(app)
    elif name == "/executions":
        rows = app.executions.list(app.session_id)
        if not rows:
            print("当前会话没有工具执行记录。")
        for row in rows:
            print(
                f"{row['tool_call_id'][:16]}  {row['status']:9}  {row['tool_name']}  "
                f"{row['started_at']}"
            )
    elif name == "/agents":
        rows = app.toolset.coding.list()
        if not rows:
            print("当前会话没有编程子 Agent 任务。")
        for row in rows:
            print(f"{row['id']}  {row['status']}  {'已合并' if row['merged'] else '未合并'}  {', '.join(row['paths']) or '(无变更)'}  {row['task'][:80]}")
    elif name == "/skills":
        print(app.toolset.by_name["list_skills"].invoke({}))
    elif name == "/tools":
        print(", ".join(app.toolset.by_name))
    elif name == "/mcp":
        rows = app.reload_mcp()
        print(f"MCP 配置：{app.toolset.mcp.path if app.toolset.mcp else app.workspace / '.mcp.json'}")
        if not rows:
            print("当前没有配置 MCP server。")
        for row in rows:
            if row["status"] == "connected":
                print(f"✔ {row['name']} connected：{', '.join(row['tools']) or '无工具'}")
            else:
                print(f"✘ {row['name']} failed：{row['error']}")
    elif name == "/trust":
        store = WorkspaceTrustStore()
        action = value or "status"
        if action == "status":
            configs = executable_configs(app.workspace)
            print(f"工作区进程执行：{'已启用' if app.workspace_trusted else '已禁用'}")
            print("可执行配置：" + (", ".join(str(path.relative_to(app.workspace)) for path in configs) if configs else "无"))
        elif action == "revoke":
            store.revoke(app.workspace)
            app.set_workspace_trusted(False)
            print("已撤销工作区信任；Hooks 和 MCP 已禁用。")
        elif action == "enable":
            show_trust_preview(app.workspace)
            answer = input("信任并启用此工作区？[y/N] ").strip().lower()
            if answer in {"y", "yes"}:
                app.set_workspace_trusted(True)
                store.trust(app.workspace)
                print("工作区已信任；Hooks 和 MCP 已启用。")
            else:
                print("已取消。")
        else:
            print("用法：/trust [status|enable|revoke]")
    elif name == "/hooks":
        app.hooks.reload()
        rows = app.hooks.configured()
        print(f"Hook 配置：{app.hooks.path}")
        if not rows:
            print("当前没有配置 Hook。")
        for row in rows:
            print(f"{row['event']} [{row['matcher']}] → {row['command']}")
    return True


COMMAND_ROUTER = SlashCommandRouter(
    (
        "/quit", "/exit", "/help", "/new", "/resume", "/model", "/mode",
        "/plan", "/rename", "/status", "/context", "/compact", "/rewind",
        "/todo", "/memory", "/history", "/executions", "/agents", "/skills",
        "/tools", "/mcp", "/sandbox", "/trust", "/hooks",
    ),
    _run_command,
)


def command(app: CodePilot, line: str) -> bool:
    result = COMMAND_ROUTER.dispatch(app, line)
    if result is None:
        print("Unknown command. Type /help.")
        return True
    return result


def handle_mcp_cli(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="codepilot mcp", description="Manage project .mcp.json servers")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List project MCP servers and connection status")
    get_parser = subparsers.add_parser("get", help="Show one MCP server config")
    get_parser.add_argument("name")
    remove_parser = subparsers.add_parser("remove", help="Remove one MCP server")
    remove_parser.add_argument("name")
    add_parser = subparsers.add_parser("add", help="Add a stdio MCP server: codepilot mcp add name -- command args")
    add_parser.add_argument("name")
    add_parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    add_parser.add_argument("server_command", nargs=argparse.REMAINDER)
    add_json_parser = subparsers.add_parser("add-json", help="Add one MCP server from a JSON object")
    add_json_parser.add_argument("name")
    add_json_parser.add_argument("json")

    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace does not exist: {workspace}")

    if args.command == "list":
        trusted, _ = request_workspace_trust(workspace, WorkspaceTrustStore())
        if not trusted:
            print("工作区未受信任：未启动 MCP Server。")
            return
        manager = McpManager(workspace)
        try:
            print(f"MCP 配置：{config_path(workspace)}")
            rows = manager.status()
            if not rows:
                print("当前没有配置 MCP server。")
            for row in rows:
                if row["status"] == "connected":
                    print(f"✔ {row['name']} connected：{', '.join(row['tools']) or '无工具'}")
                else:
                    print(f"✘ {row['name']} failed：{row['error']}")
        finally:
            manager.close()
    elif args.command == "get":
        print(json.dumps(get_server_config(workspace, args.name), ensure_ascii=False, indent=2))
    elif args.command == "remove":
        remove_server(workspace, args.name)
        print(f"Removed MCP server {args.name} from {config_path(workspace)}")
    elif args.command == "add":
        command_parts = list(args.server_command)
        if command_parts and command_parts[0] == "--":
            command_parts = command_parts[1:]
        if not command_parts:
            parser.error("add requires a server command after --")
        env = {}
        for item in args.env:
            key, sep, value = item.partition("=")
            if not sep or not key:
                parser.error("--env must use KEY=VALUE")
            env[key] = value
        add_stdio_server(workspace, args.name, command_parts[0], command_parts[1:], env or None)
        print(f"Added MCP server {args.name} to {config_path(workspace)}")
    elif args.command == "add-json":
        add_json_server(workspace, args.name, args.json)
        print(f"Added MCP server {args.name} to {config_path(workspace)}")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        handle_mcp_cli(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="LangGraph coding assistant")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--db", type=Path, help="SQLite database path")
    parser.add_argument("--model", choices=MODEL_ALIASES, default="qwen")
    parser.add_argument("--mode", choices=PERMISSION_MODES, default="default")
    parser.add_argument("--resume", metavar="SESSION_ID")
    parser.add_argument("--once", metavar="PROMPT", help="Send one prompt, then exit")
    parser.add_argument("--debug", action="store_true", help="Show longer tool results and token usage")
    parser.add_argument("--show-reasoning", action="store_true", help="Display reasoning text when the provider exposes it")
    parser.add_argument("--quiet", action="store_true", help="Hide progress and tool traces")
    trust_group = parser.add_mutually_exclusive_group()
    trust_group.add_argument("--trust-workspace", action="store_true", help="Trust current executable project config and remember its fingerprint")
    trust_group.add_argument("--no-trust-workspace", action="store_true", help="Keep project Hooks and MCP processes disabled")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace does not exist: {workspace}. Use an existing project directory.")
    load_environment(workspace)
    trust_store = WorkspaceTrustStore()
    if args.no_trust_workspace:
        workspace_trusted = not executable_configs(workspace)
    elif args.trust_workspace:
        show_trust_preview(workspace)
        trust_store.trust(workspace)
        workspace_trusted = True
    else:
        workspace_trusted, _ = request_workspace_trust(workspace, trust_store)
    if executable_configs(workspace) and not workspace_trusted:
        print("工作区未受信任：Hooks 和 MCP 进程已禁用。可使用 /trust enable 启用。")
    app = CodePilot(
        workspace, args.db,
        on_event=None if args.quiet else ConsoleTrace(args.debug, args.show_reasoning),
        workspace_trusted=workspace_trusted,
    )
    try:
        if args.resume:
            row = app.resume(args.resume)
            try:
                app.models.get(app.model_alias)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Resumed {row['id']} ({row['model']})")
            finish_pending(app)
        else:
            try:
                app.models.get(args.model)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"Session: {app.new_session(args.model, args.mode)}")
        print(f"Workspace: {workspace}\nModel: {app.model_alias}\nMode: {app.permission_mode}\nType /help for commands.")
        if args.once:
            finish_turn(app, app.send(args.once))
            return
        while True:
            try:
                line = input("codepilot> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            try:
                if line.startswith("/"):
                    if not command(app, line):
                        break
                else:
                    finish_turn(app, app.send(line))
            except Exception as exc:
                print(f"Error: {type(exc).__name__}: {exc}")
    finally:
        app.close()


if __name__ == "__main__":
    main()
