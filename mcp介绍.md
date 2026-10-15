# CodePilot MCP 功能介绍与面试答辩

## 1. MCP 是什么

MCP 全称是 Model Context Protocol，可以理解成“给 AI Agent 接外部工具和数据源的标准协议”。

传统做法是每接一个外部系统都在项目里手写一个工具，比如 GitHub 写一个 `create_issue`，数据库写一个 `query_sql`，浏览器写一个 `open_page`。这样工具越多，Agent 项目越臃肿。

MCP 的思路是把外部能力做成独立 server。Agent 不需要知道 GitHub、数据库、浏览器内部怎么实现，只需要按 MCP 协议问 server：

```text
你有哪些工具？每个工具叫什么？有什么描述？参数 schema 是什么？
```

server 返回工具列表后，Agent 再把这些工具暴露给模型。模型根据工具名、description 和 inputSchema 决定何时调用。

一句话概括：

> MCP 是 AI Agent 的工具扩展协议。它让外部工具以统一方式被发现、描述和调用。

## 2. 本项目实现了什么

本项目实现了项目级 MCP stdio 工具扩展，接近 Claude Code 的常用 MCP 使用方式。

支持能力：

- 读取工作区根目录 `.mcp.json`
- 支持 `mcpServers` 配置
- 支持 stdio MCP server
- 支持 `codepilot mcp add/list/get/remove`
- 支持会话内 `/mcp` 重新加载 MCP server
- 通过 JSON-RPC 发送 `initialize`
- 通过 `tools/list` 动态发现 MCP 工具
- 把 MCP 工具转换成 LangChain `StructuredTool`
- 工具名统一包装成 `mcp__<server>__<tool>`
- 模型通过 `bind_tools()` 看到 MCP 工具
- 调用时通过 `tools/call` 转发给 MCP server
- MCP 工具默认需要人工审批
- 计划模式下 MCP 工具会被拒绝
- 支持 `CODEPILOT_PROJECT_DIR` 和 `CLAUDE_PROJECT_DIR` 环境变量

当前未实现：

- HTTP/SSE MCP transport
- OAuth 授权流程
- MCP resources
- MCP prompts
- server 动态变更通知 `list_changed`
- 项目级首次信任审批

所以面试时要说清楚：这个版本覆盖了 stdio tools 主链路，足够演示 Agent 如何接 MCP 工具，但不是完整 MCP 客户端。

## 3. 配置文件长什么样

MCP 配置放在工作区根目录：

```text
.mcp.json
```

示例：

```json
{
  "mcpServers": {
    "github": {
      "command": "cmd",
      "args": ["/c", "mcp-server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}",
        "HTTP_PROXY": "http://127.0.0.1:7897",
        "HTTPS_PROXY": "http://127.0.0.1:7897"
      }
    }
  }
}
```

注意：

- `.mcp.json` 只描述如何启动 MCP server
- 工具名、工具描述、参数 schema 不写在 `.mcp.json`
- 这些信息由 MCP server 启动后通过 `tools/list` 返回
- 密钥建议用 `${GITHUB_PERSONAL_ACCESS_TOKEN}` 引用环境变量，不要直接写真实 token

## 4. 一条完整 MCP 加载链路

完整流程如下：

```text
启动 CodePilot 或输入 /mcp
  -> 读取 .mcp.json
  -> 遍历 mcpServers
  -> subprocess.Popen 启动 stdio MCP server
  -> 发送 initialize
  -> 发送 notifications/initialized
  -> 发送 tools/list
  -> server 返回 tools
  -> 保存 name、description、inputSchema
  -> 包装成 LangChain StructuredTool
  -> 加入 ToolSet.tools
  -> model.bind_tools(...)
  -> 模型看到 MCP 工具
```

真正调用工具时：

```text
模型选择 mcp__github__create_issue
  -> 进入 LangGraph tools 节点
  -> 权限策略判断为 ask
  -> interrupt 暂停等待用户审批
  -> 用户批准
  -> PreToolUse Hook 可再次拦截
  -> McpManager.call()
  -> 启动对应 MCP server
  -> 发送 tools/call
  -> MCP server 执行真实外部操作
  -> 返回 content
  -> CodePilot 转成 ToolMessage
  -> 模型继续总结结果
```

## 5. 关键代码位置

### 5.1 配置读写

代码位置：

```text
src/codepilot/mcp.py
```

配置路径：

```python
def config_path(workspace: Path) -> Path:
    return workspace.resolve() / ".mcp.json"
```

读取配置：

```python
def load_config(workspace: Path) -> dict[str, Any]:
    path = config_path(workspace)
    if not path.exists():
        return {"mcpServers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise ValueError(f"Invalid MCP config: {path}")
    return data
```

这里做了两件事：

- 没有 `.mcp.json` 时返回空配置
- 有配置时要求顶层必须包含 `mcpServers`

### 5.2 命令行 add/list/get/remove

代码位置：

```text
src/codepilot/cli.py
```

入口：

```python
if len(sys.argv) > 1 and sys.argv[1] == "mcp":
    handle_mcp_cli(sys.argv[2:])
    return
```

所以可以这样使用：

```powershell
uv run codepilot mcp add github -- cmd /c mcp-server-github
uv run codepilot mcp list
uv run codepilot mcp get github
uv run codepilot mcp remove github
```

`add` 最终调用：

```python
add_stdio_server(workspace, args.name, command_parts[0], command_parts[1:], env or None)
```

写入 `.mcp.json` 的代码在：

```python
def add_stdio_server(workspace, name, command, args, env=None):
    _validate_server_name(name)
    data = load_config(workspace)
    servers = data.setdefault("mcpServers", {})
    servers[name] = {"command": command, "args": args}
    if env:
        servers[name]["env"] = env
    save_config(workspace, data)
```

### 5.3 启动 MCP server

核心类：

```python
class StdioMcpClient:
```

启动位置：

```python
self.process = subprocess.Popen(
    [command, *args],
    cwd=self.workspace,
    env=env,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
)
```

这就是根据 `.mcp.json` 的 `command` 和 `args` 启动外部 server。

如果配置是：

```json
{
  "command": "cmd",
  "args": ["/c", "mcp-server-github"]
}
```

实际启动就是：

```python
subprocess.Popen(["cmd", "/c", "mcp-server-github"])
```

同时把 stdin/stdout 接管成管道，后续所有 MCP 请求都通过这里传输。

### 5.4 发送 initialize

启动 server 后立刻发送：

```python
self._request("initialize", {
    "protocolVersion": PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "codepilot", "version": "0.1.0"},
})
```

这会变成 JSON-RPC：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {
      "name": "codepilot",
      "version": "0.1.0"
    }
  }
}
```

发送逻辑：

```python
def _write(self, message: dict[str, Any]) -> None:
    self.process.stdin.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    self.process.stdin.flush()
```

这里是一行一个 JSON-RPC 消息。

初始化成功后发送通知：

```python
self._notify("notifications/initialized", {})
```

通知没有 id，不等待返回。

### 5.5 发送 tools/list

代码：

```python
def list_tools(self) -> list[dict[str, Any]]:
    result = self._request("tools/list", {})
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        raise RuntimeError("MCP tools/list returned a non-list tools field")
    return [tool for tool in tools if isinstance(tool, dict)]
```

发送的 JSON-RPC 类似：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list",
  "params": {}
}
```

server 返回类似：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "tools": [
      {
        "name": "create_issue",
        "description": "Create a GitHub issue",
        "inputSchema": {
          "type": "object",
          "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"}
          },
          "required": ["owner", "repo", "title"]
        }
      }
    ]
  }
}
```

这就是模型知道工具用途的来源。

### 5.6 读取 MCP 响应

响应读取代码：

```python
def _read_response(self, request_id: int) -> dict[str, Any]:
    while True:
        line = self._readline()
        message = json.loads(line)
        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
        result = message.get("result", {})
        return result
```

要点：

- 按 `id` 匹配请求和响应
- server 返回 `error` 时抛异常
- `stdout` 超时会报错，避免 CLI 卡死

超时读取：

```python
def _readline(self) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return result.get(timeout=self.timeout)
```

这里用线程包一层，是因为 Windows 上 `readline()` 本身不好直接加 timeout。

### 5.7 保存 MCP 工具元数据

加载 MCP 工具的入口：

```python
class McpManager:
    def reload(self) -> None:
        self.tools = []
        self.errors = {}
        servers = load_config(self.workspace).get("mcpServers", {})
```

逐个 server 连接并读取工具：

```python
with StdioMcpClient(self.workspace, server, config) as client:
    for item in client.list_tools():
```

保存工具元数据：

```python
self.tools.append(McpToolRef(
    server=server,
    name=name,
    public_name=_tool_public_name(server, name),
    description=str(item.get("description") or f"MCP tool {name} from {server}"),
    input_schema=schema,
))
```

其中：

```python
def _tool_public_name(server: str, tool_name: str) -> str:
    safe_tool = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
    return f"mcp__{server}__{safe_tool}"
```

所以 GitHub server 的 `create_issue` 会变成：

```text
mcp__github__create_issue
```

这么做有两个目的：

- 避免不同 server 的工具重名
- 让权限策略和日志能一眼识别这是 MCP 工具

### 5.8 转成 LangChain 工具

MCP 工具还不能直接给模型，需要变成 LangChain `StructuredTool`：

```python
def as_langchain_tools(self) -> list[StructuredTool]:
    return [self._to_langchain_tool(ref) for ref in self.tools]
```

转换代码：

```python
def _to_langchain_tool(self, ref: McpToolRef) -> StructuredTool:
    def call(**kwargs) -> str:
        return self.call(ref.public_name, kwargs)

    call.__name__ = ref.public_name
    return StructuredTool.from_function(
        func=call,
        name=ref.public_name,
        description=f"[MCP:{ref.server}] {ref.description}",
        args_schema=ref.input_schema,
        infer_schema=False,
    )
```

最关键的是：

```python
description=f"[MCP:{ref.server}] {ref.description}"
args_schema=ref.input_schema
```

模型就是靠这两个信息判断：

- 这个工具能干什么
- 调用时需要哪些参数

### 5.9 加入 ToolSet

代码位置：

```text
src/codepilot/tools.py
```

初始化时创建 MCP manager：

```python
self.mcp = McpManager(self.workspace, event_sink=event_sink) if enable_mcp else None
```

构建工具列表时追加 MCP tools：

```python
if owner.mcp is not None:
    base_tools.extend(owner.mcp.as_langchain_tools())
```

所以最终工具列表里既有内置工具，也有 MCP 工具。

### 5.10 绑定给模型

代码位置：

```text
src/codepilot/agent.py
```

模型调用前，会取出当前允许暴露的工具：

```python
available_tools = [
    tool for tool in self.toolset.tools
    if decide(self.permission_mode, tool.name).kind != "deny"
]
```

然后绑定给模型：

```python
bound_model = model.bind_tools(available_tools)
```

所以模型能看到 MCP 工具，是因为：

```text
MCP tools/list
  -> McpToolRef
  -> StructuredTool
  -> ToolSet.tools
  -> model.bind_tools(...)
```

### 5.11 调用 MCP 工具

当模型调用：

```text
mcp__github__create_issue
```

实际进入这个闭包：

```python
def call(**kwargs) -> str:
    return self.call(ref.public_name, kwargs)
```

然后：

```python
def call(self, public_name: str, arguments: dict[str, Any]) -> str:
    ref = next((item for item in self.tools if item.public_name == public_name), None)
    config = get_server_config(self.workspace, ref.server)
    with StdioMcpClient(self.workspace, ref.server, config) as client:
        return client.call_tool(ref.name, arguments)
```

真正发送 `tools/call`：

```python
def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
    result = self._request("tools/call", {"name": name, "arguments": arguments})
```

发送给 MCP server 的消息类似：

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "create_issue",
    "arguments": {
      "owner": "xxx",
      "repo": "xxx",
      "title": "xxx",
      "body": "xxx"
    }
  }
}
```

server 返回后，CodePilot 把 `content` 转成字符串：

```python
if item.get("type") == "text":
    parts.append(str(item.get("text", "")))
```

最后作为普通工具结果返回给 LangGraph。

## 6. MCP 和普通内置工具的区别

内置工具：

```text
工具函数写在 CodePilot 源码里
比如 read_file、write_file、grep_code
```

MCP 工具：

```text
工具函数在外部 server 里
CodePilot 只负责发现、包装、转发调用
```

对模型来说，两者都是工具；对系统实现来说，一个是本地函数，一个是外部 JSON-RPC server。

可以这样回答：

> 内置工具是编译在 Agent 里的能力，MCP 工具是运行时从外部 server 动态发现的能力。我通过 `tools/list` 拿到工具的 description 和 inputSchema，再包装成 LangChain `StructuredTool`，所以模型看到 MCP 工具时和看到普通工具一样。

## 7. MCP 和 Hook 的区别

这两个容易混。

MCP 是：

```text
给模型增加可调用工具
由模型主动选择调用
通过 tools/list 发现工具
通过 tools/call 执行工具
```

Hook 是：

```text
给 Agent 生命周期加拦截点
由程序在固定时机自动触发
比如 PreToolUse、PostToolUse、Stop
可以阻止、追加上下文、要求继续
```

一句话：

> MCP 是工具扩展协议，Hook 是生命周期拦截机制。MCP 让模型能调用更多工具；Hook 控制 Agent 在关键步骤是否继续执行。

## 8. 权限和安全如何处理

权限代码在：

```text
src/codepilot/permissions.py
```

核心逻辑：

```python
if mode == "plan" and tool_name.startswith("mcp__"):
    return PermissionDecision("deny", "计划模式只允许调查和提交计划")

if tool_name.startswith("mcp__"):
    return PermissionDecision("ask")
```

含义：

- MCP 工具默认不自动执行
- 每次调用都需要人工审批
- 计划模式下直接拒绝
- MCP 工具仍然经过 `PreToolUse` Hook
- 执行结果仍然作为 `ToolMessage` 回到模型

面试可以说：

> MCP server 往往连接外部系统，比如 GitHub、数据库、浏览器，所以我没有把它设成默认自动执行，而是统一进入原有审批链路。这样 MCP 扩展不会绕过项目已有的权限模式、人工审批和 Hook 拦截。

## 9. 为什么 .mcp.json 里没有工具描述，模型仍然知道怎么调用

因为 `.mcp.json` 只负责启动 server，不负责描述工具。

工具描述来自 MCP server 的 `tools/list` 响应。

流程是：

```text
.mcp.json 提供 command + args
  -> CodePilot 启动 server
  -> CodePilot 调 tools/list
  -> server 返回 tools
  -> 每个 tool 包含 name、description、inputSchema
  -> CodePilot 包装成 LangChain 工具
  -> 模型通过 bind_tools 看到工具说明和参数
```

所以：

```json
{
  "command": "cmd",
  "args": ["/c", "mcp-server-github"]
}
```

只是告诉 CodePilot：

```text
怎么启动 GitHub MCP server
```

真正告诉模型“有哪些 GitHub 操作”的，是 GitHub MCP server 返回的：

```json
{
  "tools": [
    {
      "name": "create_issue",
      "description": "Create an issue in a GitHub repository",
      "inputSchema": {}
    }
  ]
}
```

## 10. 为什么调用时重新启动 server

当前实现里，发现工具时会启动一次 server：

```text
initialize -> tools/list -> 关闭
```

真正调用工具时又启动一次 server：

```text
initialize -> tools/call -> 关闭
```

这样设计比较简单：

- 不需要长期维护多个子进程
- 不需要处理 server 崩溃后的重连
- 不需要管理并发请求队列
- 对面试演示更稳定

缺点是：

- 每次调用都有启动开销
- 不能利用 server 内部长连接状态
- 不支持 server 动态通知

可以这样回答：

> 我当前选择短连接式 stdio client，优点是实现简单、故障隔离清楚、不会因为某个 MCP server 卡住长期影响主进程。生产版本可以把 server 进程做成连接池，启动时保持长连接，支持动态 tool list 更新和重连。

## 11. 面试 1 分钟介绍

可以这样说：

> 我在项目里实现了一个项目级 MCP stdio client，用来动态扩展 Agent 的工具生态。用户在工作区 `.mcp.json` 里配置 MCP server 的 command、args 和 env。CodePilot 启动或执行 `/mcp` 时，会按配置启动 stdio server，通过 JSON-RPC 发送 `initialize`，再调用 `tools/list` 获取 server 暴露的工具列表。每个工具包含 name、description 和 inputSchema，我把它包装成 LangChain `StructuredTool`，命名为 `mcp__server__tool`，再加入 `ToolSet.tools`，最终通过 `model.bind_tools()` 暴露给模型。模型调用 MCP 工具时，执行层会走原来的权限审批和 Hook 流程，审批通过后发送 `tools/call` 给对应 MCP server，并把返回内容作为 ToolMessage 交回 LangGraph。这样 MCP 不需要写死在项目里，外部系统能力可以运行时接入，同时仍然复用原有的安全边界。

## 12. 更短的简历版描述

> 实现 MCP stdio 工具扩展能力，支持项目级 `.mcp.json`、`codepilot mcp add/list/get/remove` 和会话内 `/mcp` 动态加载；通过 JSON-RPC `initialize`、`tools/list`、`tools/call` 将外部 MCP server 工具转换为 LangChain `StructuredTool`，并统一接入权限审批、Hook 拦截和 LangGraph 工具执行链路。

## 13. 常见面试问题与回答

### Q1：MCP 的本质是什么？

A：MCP 是 Agent 和外部工具之间的标准协议。它解决的是工具如何被发现、描述和调用的问题。Agent 不需要手写每个外部系统的工具，只需要启动 MCP server，通过 `tools/list` 获取工具 schema，再通过 `tools/call` 调用。

### Q2：你的项目里 MCP 是怎么实现的？

A：我实现了一个 stdio MCP client。它读取 `.mcp.json`，用 `subprocess.Popen` 启动 server，通过 stdin/stdout 发送换行分隔 JSON-RPC。启动后先发 `initialize`，再发 `tools/list` 获取工具列表。然后把每个 MCP tool 包装成 LangChain `StructuredTool`，加入 ToolSet，最终通过 `model.bind_tools()` 暴露给模型。调用时再发送 `tools/call` 给 server。

### Q3：模型怎么知道 MCP 工具该怎么用？

A：不是靠 `.mcp.json`，而是靠 MCP server 返回的 `tools/list`。每个 tool 都有 `name`、`description` 和 `inputSchema`。我把这些字段原样转换成 LangChain 工具的 name、description 和 args_schema，模型就能根据工具描述和参数 schema 做 tool calling。

### Q4：为什么工具名要变成 `mcp__server__tool`？

A：为了避免不同 MCP server 的工具重名，同时也方便权限策略、日志和 Hook 识别这是 MCP 工具。比如 GitHub 和 GitLab 都可能有 `create_issue`，包装后就是 `mcp__github__create_issue` 和 `mcp__gitlab__create_issue`。

### Q5：MCP 工具和内置工具执行链路一样吗？

A：对模型和 LangGraph 来说基本一样，都是 LangChain Tool。区别在于内置工具直接执行 Python 函数，MCP 工具的函数体会把参数转发成 `tools/call` JSON-RPC 请求。它仍然会经过权限判断、人工审批、PreToolUse Hook、PostToolUse Hook 和 ToolMessage 回传。

### Q6：MCP 工具为什么默认需要审批？

A：因为 MCP server 通常连接外部系统，比如 GitHub、数据库、浏览器或云服务，副作用不可控。为了不绕过原来的安全机制，我把所有 `mcp__` 开头的工具默认设为 ask，计划模式下直接 deny。

### Q7：MCP 和 Hook 有什么区别？

A：MCP 是让模型多一些可调用工具；Hook 是程序在生命周期节点自动触发的拦截器。MCP 由模型选择调用，Hook 由 Agent 运行时触发。MCP 扩展能力，Hook 管控流程。

### Q8：如果 MCP server 挂了怎么办？

A：加载阶段如果 server 启动失败、initialize 失败或 tools/list 超时，错误会记录到 `McpManager.errors`，`/mcp` 或 `mcp list` 会显示 failed，不会让整个 Agent 崩溃。读取 stdout 也做了 timeout，避免 CLI 卡死。

### Q9：为什么当前只支持 stdio？

A：stdio 是本地 MCP server 最常见的接入方式，也是 Claude Code 本地 MCP 的主路径之一。它实现简单，适合 CLI 项目演示。HTTP/SSE 和 OAuth 涉及连接管理、鉴权和回调流程，可以作为后续扩展。

### Q10：为什么调用 MCP 工具时重新启动 server，而不是一直保持连接？

A：这是一个工程取舍。短连接实现简单，server 出错不会污染长期状态，也不用维护多 server 进程池。缺点是每次调用有启动开销。生产版本可以改成长连接管理器，支持 keep-alive、重连、并发请求和动态工具更新。

### Q11：MCP 工具的参数校验是谁做的？

A：MCP server 返回 `inputSchema`，我把它挂到 LangChain `StructuredTool.args_schema`。模型调用前会看到 schema，LangChain 也会按工具 schema 组织参数。真正的业务校验仍应由 MCP server 自己完成，因为 server 才知道外部系统的真实约束。

### Q12：MCP 会不会绕过项目的 workspace 限制？

A：MCP server 是外部进程，它自己的能力不一定受 CodePilot 内置文件工具的 workspace 限制。所以我把 MCP 工具放进审批链路，并且建议只配置可信 server。内置文件工具的路径沙箱不能自动约束外部 MCP server。

### Q13：MCP server 的密钥如何管理？

A：`.mcp.json` 支持 env 配置和 `${ENV_NAME}` 展开。推荐把真实密钥放到 `.env` 或系统环境变量里，`.mcp.json` 只写 `${GITHUB_PERSONAL_ACCESS_TOKEN}`。不要把真实 token 写进配置文件。

### Q14：怎么在 Windows 上接 npm MCP server？

A：通常用 `cmd /c` 包一层，例如：

```json
{
  "command": "cmd",
  "args": ["/c", "npx", "-y", "@xxx/mcp-server"]
}
```

或者：

```powershell
uv run codepilot mcp add github -- cmd /c mcp-server-github
```

### Q15：如何排查 MCP 没加载成功？

A：先运行：

```powershell
uv run codepilot mcp list
```

或者会话里输入：

```text
/mcp
```

看 server 是 connected 还是 failed。如果 failed，检查：

- command 是否能在当前终端直接运行
- args 是否正确
- env 变量是否缺失
- 代理地址是否是纯 URL，不要写 Markdown 链接
- server 是否支持 stdio MCP
- server 是否在 10 秒内响应 initialize

### Q16：MCP 如何和 LangGraph 结合？

A：MCP 本身不直接进入 LangGraph。它先被包装成 LangChain `StructuredTool`，加入 `ToolSet.tools`。LangGraph 的 model 节点通过 `bind_tools()` 暴露这些工具，tools 节点在模型产生 tool call 后执行它们。所以 MCP 是通过 LangChain Tool 这层接入 LangGraph 工具节点的。

### Q17：MCP 工具执行失败会怎样？

A：如果 `tools/call` 返回 `isError`，我会把结果前面加上 `MCP tool returned an error:`，作为工具结果返回给模型。如果进程异常、JSON-RPC error 或超时，会抛异常，外层工具节点捕获后生成 `Tool error: ...`，再以 ToolMessage 返回给模型。

### Q18：你这个 MCP 实现和 Claude Code 一样吗？

A：设计思路接近 Claude Code 的项目级 MCP：都使用 `.mcp.json` 配置 server，会话内可查看状态，工具由 server 自描述。但我当前只实现 stdio tools 主链路，没有完整实现 Claude Code 的所有 MCP transport、OAuth、资源、prompts 和信任管理。

## 14. 面试追问时可以画的流程图

```text
                   .mcp.json
                       |
                       v
               McpManager.reload()
                       |
                       v
             StdioMcpClient.__enter__()
                       |
              subprocess.Popen()
                       |
                       v
        initialize -> notifications/initialized
                       |
                       v
                  tools/list
                       |
                       v
       name + description + inputSchema
                       |
                       v
              McpToolRef 列表
                       |
                       v
          StructuredTool.from_function()
                       |
                       v
                 ToolSet.tools
                       |
                       v
              model.bind_tools()
                       |
                       v
                  模型选择工具
                       |
                       v
        interrupt 审批 -> PreToolUse Hook
                       |
                       v
                  tools/call
                       |
                       v
              ToolMessage 回到模型
```

## 15. 可以展示的命令

添加 GitHub MCP server：

```powershell
uv run codepilot mcp add github --env GITHUB_PERSONAL_ACCESS_TOKEN=${GITHUB_PERSONAL_ACCESS_TOKEN} -- cmd /c mcp-server-github
```

查看状态：

```powershell
uv run codepilot mcp list
```

查看配置：

```powershell
uv run codepilot mcp get github
```

进入会话：

```powershell
uv run codepilot --model qwen
```

会话内重新加载：

```text
codepilot> /mcp
codepilot> /tools
```

模型能看到的工具名会类似：

```text
mcp__github__create_issue
mcp__github__search_repositories
mcp__github__get_file_contents
```

## 16. 项目里的测试

测试文件：

```text
tests/test_mcp.py
```

测试覆盖：

- fake stdio MCP server 能被启动
- `tools/list` 能返回工具
- MCP 工具能包装成 LangChain tool
- `tools/call` 能返回结果
- MCP 工具默认需要审批
- plan 模式拒绝 MCP 工具

运行：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_mcp.py -q
```

全量验证：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## 17. 最后总结

这个项目里的 MCP 实现可以概括成三层：

第一层是协议层：

```text
StdioMcpClient 负责启动 server，并用 JSON-RPC 发送 initialize、tools/list、tools/call。
```

第二层是适配层：

```text
McpManager 把 MCP tool 的 name、description、inputSchema 转成 LangChain StructuredTool。
```

第三层是 Agent 集成层：

```text
ToolSet 把 MCP 工具加入工具列表，Agent 用 bind_tools 暴露给模型，权限层和 Hook 层继续保护执行过程。
```

最适合面试的一句话：

> 我把 MCP 当成动态工具发现协议来实现：`.mcp.json` 只负责启动 server，`tools/list` 提供工具 schema，CodePilot 把这些 schema 包装成 LangChain `StructuredTool`，再接入 LangGraph 原有的工具执行、人工审批和 Hook 拦截链路。


因为 Claude Code 很可能把 MCP 启动做成了**异步懒加载**，主 CLI 不会被 MCP server 的初始化阻塞。

你现在项目里的实现是同步加载：

```text
启动 CodePilot
  -> ToolSet 初始化
  -> McpManager 初始化
  -> McpManager.reload()
  -> 遍历所有 MCP server
  -> 每个 server 都 initialize + tools/list
  -> 全部完成后
  -> 进入主 CLI
```

也就是：

```text
MCP 没探测完，主程序不进入对话
```

Claude Code 的体验更像：

```text
启动 Claude Code
  -> 先进入主 CLI
  -> 后台启动 MCP server
  -> MCP server 连接成功后，把工具加入可用工具列表
  -> 用户输入 /mcp 可以看到连接状态
  -> 如果模型需要某个 MCP 工具，但它还没 ready，再等待或提示不可用
```

所以它不需要等 MCP 全部启动完。

更本质地说，MCP server 是**外部增强能力**，不是主 Agent 的核心启动依赖。主 Agent 至少可以先提供：

```text
文件读取
代码搜索
普通工具
对话
模型调用
```

MCP 工具晚一点 ready 也不影响主流程。

可以类比 VS Code 插件：

```text
VS Code 先打开窗口
插件在后台慢慢激活
某个插件没加载好，只影响对应功能
```

Claude Code 的 MCP 大概率也是类似插件模型。

你当前实现为什么会等？

因为这里在 `ToolSet.__init__()` 里直接创建了 `McpManager`：

```python
self.mcp = McpManager(self.workspace, event_sink=event_sink) if enable_mcp else None
```

而 `McpManager.__init__()` 里立刻调用：

```python
self.reload()
```

`reload()` 又会同步启动每个 server：

```python
with StdioMcpClient(self.workspace, server, config) as client:
    for item in client.list_tools():
```

所以只要某个 MCP server 启动慢，CodePilot 启动就慢。

如果要改成 Claude Code 那种体验，可以这样设计：

```text
McpManager 初始化时只读取配置，不连接 server
后台线程异步 reload
ToolSet 先只注册内置工具
模型每次 bind_tools 前读取当前 ready 的 MCP tools
/mcp 显示 pending / connected / failed
如果用户手动 /mcp reload，再触发后台刷新
```

架构可以是：

```text
CodePilot 启动
  -> ToolSet 只加载内置工具
  -> McpManager.start_background_reload()
  -> CLI 立刻进入 codepilot>
  -> 后台逐个 server initialize + tools/list
  -> 成功后更新 mcp tools cache
  -> 下一次模型调用时 bind_tools 使用最新工具列表
```

如果面试官问“为什么 Claude Code 不阻塞启动”，你可以这样回答：

> MCP 是外部扩展能力，不应该成为主 Agent 的启动阻塞依赖。Claude Code 更像插件系统，主进程先启动，MCP server 在后台异步初始化。初始化成功后再把 tools 动态加入工具注册表；失败的 server 只影响对应工具，用户可以通过 `/mcp` 查看状态。这样主交互不被慢 server 或坏配置拖住。

如果问“你现在项目怎么优化”，你可以回答：

> 我当前实现是同步 stdio client，便于把 MCP 主链路跑通。下一步会改成异步懒加载：`McpManager` 初始化时只读配置，后台线程连接 server 并缓存工具 schema；`model.bind_tools()` 每轮读取当前 ready 的 MCP tools；`/mcp` 显示 pending、connected、failed，并支持手动刷新。这样就能做到主 CLI 秒进，MCP 工具逐步可用。



后加载的 MCP 工具要想绑定进工具列表，关键是：**不要把工具列表做成启动时的一次性固定列表，而是每次模型调用前动态取一次当前可用工具。**

你现在项目里是半动态的。

`ToolSet.__init__()` 里会构建一次：

```python
self.tools = self._build()
self.by_name = {item.name: item for item in self.tools}
```

模型调用前用的是：

```python
available_tools = [
    tool for tool in self.toolset.tools
    if decide(self.permission_mode, tool.name).kind != "deny"
]

bound_model = model.bind_tools(available_tools)
```

所以如果 MCP 工具是后台后加载的，要做的是：

```text
后台 MCP 加载完成
  -> 更新 McpManager.tools
  -> 重建 ToolSet.tools 和 ToolSet.by_name
  -> 下一次 _call_model() 时 bind_tools() 会拿到新工具
```

也就是说，不需要重启模型，也不需要重启 LangGraph。因为 `bind_tools()` 是每一轮模型调用时执行的，不是会话启动时只执行一次。

可以改成这样。

**方案一：后台加载完后主动 rebuild**

```python
class ToolSet:
    def rebuild(self):
        self.tools = self._build()
        self.by_name = {item.name: item for item in self.tools}
```

后台线程加载 MCP：

```python
def background_reload_mcp(self):
    self.mcp.reload()
    self.rebuild()
```

链路：

```text
CodePilot 启动
  -> ToolSet 先构建内置工具
  -> 后台线程加载 MCP
  -> 加载成功后 ToolSet.rebuild()
  -> 下一次模型调用 bind_tools() 拿到新 MCP 工具
```

这和 Claude Code 的体验接近。

**方案二：每次访问 tools 时动态合并**

更干净一点，不保存固定 `self.tools`，而是写成属性：

```python
class ToolSet:
    def __init__(...):
        self.base_tools = self._build_base_tools()
        self.by_name = {}

    @property
    def tools(self):
        tools = list(self.base_tools)
        if self.mcp:
            tools.extend(self.mcp.as_langchain_tools())
        return tools

    @property
    def by_name(self):
        return {tool.name: tool for tool in self.tools}
```

这样 MCP 后台加载完成后，只要 `self.mcp.tools` 更新了，下一次访问 `toolset.tools` 就自动包含新工具。

不过这里要注意：`by_name` 如果每次都临时生成，工具执行时也要拿同一个最新映射。

你的执行层在 `_call_tools()` 里用的是：

```python
self.toolset.by_name[call["name"]].invoke(call["args"])
```

所以只要 `by_name` 是动态的，模型后加载的 MCP 工具也能执行。

**我更推荐方案一**

因为你项目当前结构已经是：

```python
self.tools = self._build()
self.by_name = {item.name: item for item in self.tools}
```

所以最小改动是保留这个结构，加一个线程安全的 rebuild：

```python
class ToolSet:
    def refresh_tools(self):
        with self._lock:
            self.tools = self._build()
            self.by_name = {item.name: item for item in self.tools}
```

然后 MCP 后台加载完成后调用：

```python
self.toolset.refresh_tools()
```

`_call_model()` 每次都会重新：

```python
bound_model = model.bind_tools(available_tools)
```

所以新工具会在下一轮模型调用时自然出现。

**还需要解决一个细节：模型本轮已经开始了怎么办**

如果用户输入时 MCP 工具还没加载好，那本轮 `bind_tools()` 时模型看不到它。加载完成后，本轮不会自动更新，因为模型请求已经发出去了。

所以行为应该是：

```text
本轮开始前 ready 的 MCP 工具 -> 本轮可见
本轮开始后才 ready 的 MCP 工具 -> 下一轮可见
```

这是合理的。

可以在 CLI 里提示：

```text
[MCP] github connected: mcp__github__create_issue, ...
```

用户下一句话模型就能用了。

**面试时可以这样回答**

> 后加载 MCP 工具不需要重启 Agent，因为工具绑定不是在会话启动时固定完成的。我在每次模型调用前都会执行 `model.bind_tools(available_tools)`。所以只要后台 MCP loader 更新了工具注册表，下一轮模型调用就会把新的 MCP 工具 schema 一起绑定进去。  
>  
> 具体实现上，MCP 后台线程完成 `tools/list` 后，把工具元数据缓存到 `McpManager.tools`，然后重建 `ToolSet.tools/by_name`。模型下一轮进入 `_call_model()` 时重新读取当前工具列表；如果模型调用了新 MCP 工具，tools 节点通过更新后的 `by_name` 找到对应 `StructuredTool`，再发送 `tools/call` 给 MCP server。  
>  
> 本轮模型请求已经发出去后才加载完成的工具，不会插进正在进行的请求，而是下一轮生效。这个边界更清楚，也避免运行中修改已发送给模型的 tool schema。
