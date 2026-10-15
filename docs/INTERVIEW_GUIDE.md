# CodePilot 面试项目说明与答辩指南

## 1. 一句话介绍

我从零构建了一个基于 LangChain 和 LangGraph 的 CLI 编程助手。它用 LangGraph 保存对话与工具调用状态，用 SQLite 恢复会话，用同一套 LangChain Tool 接口接入三个模型，并用权限模式、可恢复审批、文件快照和上下文压缩管理实际编程工作流。

## 2. 做了什么

| 模块 | 实现 | 代码位置 |
|---|---|---|
| CLI | 交互输入、会话与模型切换、权限模式、上下文压缩、文件回退、`--once` | `src/codepilot/cli.py` |
| Agent 图 | `model -> tools -> model` 循环，条件路由到结束 | `src/codepilot/agent.py` |
| 持久化 | `SqliteSaver` 保存图 checkpoint；同一个数据库的独立表保存会话标题、模型、权限模式、时间 | `src/codepilot/storage.py` |
| 多模型 | DeepSeek、Qwen、OpenAI 分别用 `langchain-deepseek`、`langchain-qwq`、`langchain-openai`；延迟创建并缓存 | `src/codepilot/models.py` |
| 工具系统 | 30 个内置 `@tool` 工具，按工作区约束路径；MCP 工具动态追加 | `src/codepilot/tools.py` |
| MCP | 项目级 `.mcp.json`，stdio JSON-RPC 连接，`tools/list` 转 LangChain 工具，`tools/call` 代理执行 | `src/codepilot/mcp.py`；接入点在 `tools.py`、`cli.py`、`permissions.py` |
| 审批 | 危险工具集合进入 `interrupt()`，用户逐项决定，`Command(resume=...)` 继续 | `src/codepilot/agent.py` 与 `cli.py` |
| 权限模式 | `default`、`acceptEdits`、`plan` 用统一策略判定允许、审批或拒绝 | `src/codepilot/permissions.py` |
| 文件快照 | 编辑前记录文件内容，按会话列出并回退；检查外部改动冲突 | `src/codepilot/checkpoints.py` |
| 上下文与项目记忆 | `/compact` 总结旧消息、`/context` 显示粗略用量；读取根目录项目指令 | `src/codepilot/agent.py` |
| 子 Agent | LangChain `create_agent` 构造独立上下文；调查子 Agent 只读，编程子 Agent 在隔离副本中修改文件 | `src/codepilot/subagents.py` 与 `coding_agents.py` |
| 调试日志 | 流式模型回复、服务商公开的推理字段、工具与子 Agent 事件；`--debug` 扩展结果长度和 Token 用量 | `src/codepilot/agent.py` 与 `cli.py` |
| 生命周期 Hooks | 项目级 JSON 配置、事件匹配、命令执行、阻止决策与模型反馈 | `src/codepilot/hooks.py`；接入点在 `agent.py`、`cli.py`、`subagents.py` |

## 3. 数据流与工作原理

1. CLI 为新对话生成 UUID，作为 LangGraph `configurable.thread_id`。会话元数据写入 `codepilot_sessions` 表。
2. 用户消息以 `HumanMessage` 输入图。`MessagesState` 的消息 reducer 自动追加，而不是覆盖历史。
3. `model` 节点在**每次调用时**按当前 alias 获得模型，注入系统提示词与保存的消息，使用 `bind_tools()` 暴露工具 schema。
4. 若模型输出 `tool_calls`，图进入 `tools` 节点。这个节点先收集本批次所有需要审批的调用，再以一次 `interrupt()` 暂停，避免执行了一部分写操作后才中断。
5. CLI 展示每个工具和参数。用户批准后，调用 `Command(resume={tool_call_id: bool})`。被拒绝的工具会返回 `ToolMessage` 告诉模型未执行。
6. 工具结果加入消息状态，模型继续推理，直到不再调用工具。每一步 checkpoint 写入 SQLite。
7. 进程重启后，`/resume <id>` 选择同一个 `thread_id`。下一轮输入会从 SQLite checkpoint 的现有消息状态继续；待审批的中断也可以恢复。

工具调用先经过 `permissions.decide()`：`default` 审批文件变更及命令；`acceptEdits` 自动执行大部分文件编辑，但删除和命令仍审批；`plan` 从模型工具 schema 中去掉修改工具，并在执行层再次拒绝越权调用。文件编辑成功后记录独立于 LangGraph 对话 checkpoint 的文件快照。审批预览通过 unified diff 展示拟议的文本变化。

`/compact` 把旧消息交给当前模型生成中文摘要，然后用 `RemoveMessage(REMOVE_ALL_MESSAGES)` 替换状态，并保留最近两条正常用户/助手消息。自动压缩阈值为 60 条消息或约 10 万字符。项目根目录的 `AGENTS.md`、`CLAUDE.md` 和 `CODEPILOT.md` 在模型调用时注入项目约定。

默认终端日志会流式显示模型公开回复，以及工具请求、审批、执行结果、子 Agent 进度和耗时。`--show-reasoning` 显示服务商在 `reasoning_content` 或标准 reasoning 块中实际返回的推理；模型未返回时不会虚构。Qwen 默认开启 `enable_thinking`，可通过 `.env` 中的 `QWEN_ENABLE_THINKING=false` 关闭。DeepSeek 可在 `.env` 中把 `DEEPSEEK_MODEL` 设为支持思考的模型。`--debug` 显示更长结果与 Token 用量；`--quiet` 关闭非必要日志。公开的推理字段并不保证等同于模型完整内部推理。

调查子 Agent 的 `explore`、`review`、`plan` 角色分别用于代码探索、审查和方案研究。主 Agent 用工具调用传入任务；每个子 Agent 使用独立的临时消息上下文和受限工具集。`delegate_agents` 用线程池并行处理 2 至 3 个互不依赖的只读任务，通过事件回调报告开始、工具调用、结果与完成。最终结论按提交顺序返回给主图，主 Agent 再核对关键证据。每个子 Agent 的递归上限为 20。

编程调度遵循“拆分目标 → 分配互不重叠的可写路径 → 隔离执行 → 审查差异 → 审批合并 → 集成验证 → 汇总”。`delegate_coding_tasks` 同时派遣 1 至 3 个 `implement` 子 Agent，每项在 `.codepilot/subagents/<id>/workspace` 的副本中运行，副本含项目文件但跳过密钥、依赖缓存和本项目 SQLite 状态库。文件工具在执行时检查写入范围；结束后再次检查所有实际变化。主 Agent 收到任务状态、修改文件和子 Agent 总结，调用 `review_subagent_changes` 查看差异。`apply_subagent_changes` 经 LangGraph `interrupt()` 审批后，验证原文件自派遣以来未变化、暂存副本未被更改，才把修改合并到主工作区并建立文件检查点。失败任务及同一文件上的并行修改不能合并。任务清单与差异保存在 `.codepilot/subagents/`，重启后可用 `/agents` 查看。

编程子 Agent 不持有主会话历史，也没有 shell 工具；主 Agent负责运行测试、修复跨任务集成问题和写最后报告。这样主 Agent 的上下文保留目标、审批、差异与验证结果，子 Agent 的大量查找和编辑过程留在各自上下文。若任务有依赖，先完成并合并前项，再派后项。主 Agent 的最终总结应分别列出已合并文件、实际测试结果、未完成任务和仍待确认的问题。

### 为什么要把会话元数据和 checkpoint 分开？

`SqliteSaver` 负责图状态与恢复，并不是面向用户的会话索引。单独的 `codepilot_sessions` 表使 `/resume` 可以显示标题、模型和最后访问时间；不需要解析内部 checkpoint 格式。两者在同一个 SQLite 文件中，部署简单。

### 多模型切换为何不丢上下文？

消息历史由 `thread_id` 指向的 checkpoint 保存，模型对象不持有会话历史。`/model qwen` 只更新当前会话的模型 alias；下一次图进入 `model` 节点时选择 Qwen，并读取**同一个**消息列表。已经发送给上一个供应商的内容不会自动迁移到模型服务端；这里的“保留”指本地记录的消息重新作为上下文发送。不同模型的上下文长度和工具调用能力可能不同，需要按服务商实际限制控制历史长度。

### 审批为何使用 LangGraph interrupt？

`interrupt()` 是图运行时的持久化暂停点，恢复时以同一 `thread_id` 调用 `Command(resume=...)`。审批发生在任何副作用之前；重启后仍能看到 pending checkpoint。批准或拒绝会形成工具响应，使后续模型回合拥有明确结果。工具节点把同一批工具调用的批准一次性收齐，降低重放时重复副作用的风险。

### Hook 是怎样工作的？

Hook 是确定性的生命周期扩展点。项目级 `.codepilot/hooks.json` 按事件名配置 matcher 和命令；`HookManager` 向命令标准输入写入事件 JSON，读取退出码与标准输出，汇总拒绝决定和附加上下文。它独立于 `_emit()`：`_emit()` 把进度交给终端显示；Hook 可以改变后续执行，例如拒绝工具或要求模型继续回答。

本项目接入 `SessionStart/End`、`UserPromptSubmit`、`PermissionRequest`、`Pre/PostToolUse`、`PostToolUseFailure`、`SubagentStart/Stop`、`Pre/PostCompact`、`Pre/PostModelSwitch` 与 `Stop`。`PreToolUse` 在审批恢复后、真正调用 `BaseTool.invoke()` 之前执行，因此 Hook 不会在 `interrupt()` 节点重放时提前重复执行。Hook 拒绝工具时写入 `ToolMessage`，模型看到拒绝原因；`PostToolUse` 的 `additionalContext` 追加到工具响应，下一轮模型能据此修复问题。`Stop` 返回阻止决定时，主 Agent 追加一条继续处理的消息，再调用图；最多尝试两次。

Hook 不能把 `permissions.decide()` 的拒绝或用户拒绝改成批准。`PermissionRequest` 在 CLI 展示审批时可以提前拒绝，但直接调用 `CodePilot.approve()` 的程序仍以 `PreToolUse` 作为统一执行前阻断点。命令超时、找不到或输出无效时记录错误并继续；这是该项目当前的失败策略。当前只支持命令型处理程序，未实现 Claude Code 的 HTTP、prompt、agent Hook 类型或完整事件协议。

### MCP 是怎样工作的？

MCP 的本质是把外部进程提供的工具动态接入 LangChain 工具生态。项目根目录 `.mcp.json` 使用 `mcpServers` 配置 server 名称、启动命令、参数和环境变量；命令行提供 `codepilot mcp add/list/get/remove` 管理配置，会话内 `/mcp` 重新探测并刷新工具。

实现上，`McpManager` 读取 `.mcp.json`，对每个 stdio server 启动子进程，用 MCP 规范的换行分隔 JSON-RPC 通信。初始化时发送 `initialize` 和 `notifications/initialized`，再调用 `tools/list` 获取工具名、描述和 `inputSchema`。每个 MCP 工具被转换成一个 LangChain `StructuredTool`，命名为 `mcp__<server>__<tool>`，模型能像调用内置工具一样看到它的 schema。

真正调用时，`StructuredTool` 会回到 `McpManager.call()`，复用已经初始化的 server 连接，发送 `tools/call`，把 arguments 传给原 MCP 工具，并把返回的 text content 拼成工具结果交给 LangGraph。MCP 进程通过统一沙箱启动，只继承最小环境与显式白名单变量。MCP 工具名以 `mcp__` 开头，在权限层默认需要人工审批；计划模式下直接拦截。

当前实现覆盖 stdio tools，这是 Claude Code 最常见的本地 server 路径；未实现 HTTP/SSE、OAuth、resources、prompts、list_changed 动态通知和项目级首次信任审批。Windows 下 npm server 需要配置为 `cmd /c npx ...`，这和 Claude Code 的 Windows 使用建议一致。

## 4. 30+ 个工具

| 类别 | 工具 |
|---|---|
| 文件 | `list_directory`、`read_file`、`write_file`、`edit_file`、`append_file`、`make_directory`、`delete_file`、`move_file`、`copy_file`、`file_info` |
| 搜索 | `glob_files`、`grep_code` |
| Git | `git_status`、`git_diff`、`git_log`、`git_show` |
| 运行 | `run_command` |
| Web | `web_search`、`web_fetch` |
| 任务与计划 | `todo_read`、`todo_write`、`propose_plan` |
| 技能 | `list_skills`、`read_skill` |
| Agent | `delegate_agent`、`delegate_agents`、`delegate_coding_tasks`、`list_coding_tasks`、`review_subagent_changes`、`apply_subagent_changes` |
| MCP | 动态工具，命名为 `mcp__<server>__<tool>` |

文件操作以 `Path.resolve()` 规范化路径，并验证最终路径位于工作区内；`.git`、`.codepilot`、虚拟环境与常见密钥文件不暴露给内置文件工具。Git 命令使用参数数组。`run_command` 解析为 argv 后直接启动程序，不经过 Shell，并通过统一沙箱限制最小环境、超时、输出、内存、进程数和进程树生命周期。`web_fetch` 检查 URL 及解析出的地址、禁止自动重定向、限制读取字节数。调查子 Agent 只有读工具；编程子 Agent 的写工具只作用于隔离副本的明确路径。

`todo_write` 接受 JSON 对象数组，并按 session ID 保存在 `.codepilot`；`propose_plan` 只有审批后才会保存 Markdown 计划。技能格式是工作区的 `.benwei/skills/<name>/SKILL.md`，可按项目增添。

## 5. 为什么这样选技术

- **LangChain**：统一 `BaseChatModel` 与 `@tool` 接口。模型切换时主逻辑无需为每个模型重新编写工具协议。三个服务商分别使用 `langchain-deepseek`、`langchain-qwq` 与 `langchain-openai`；Qwen 专用适配器保留服务商的推理字段。
- **LangGraph**：显式定义模型、工具节点与路由；`MessagesState` 管理消息追加；`SqliteSaver` 提供本地持久化；`interrupt`/`Command` 提供审批恢复语义。
- **SQLite**：无需另起数据库，适合单机 CLI。WAL/多用户并发与远程部署不是当前目标。
- **自建工具节点**：审批需要在**执行任何工具前**汇总本轮有副作用的调用。自建节点使这个边界清楚；仍使用 LangChain `BaseTool.invoke()` 执行每个工具。

## 6. 可复现的演示脚本

1. 在项目根目录准备 `.env`，填入至少一个模型密钥。运行 `uv sync --python 3.12 --extra dev`。
2. `uv run codepilot --workspace <真实存在的示例代码目录> --model qwen --show-reasoning`。
3. 输入“找出包含 `TODO` 的 Python 文件，概述风险”。展示 Glob/Grep 和文件读取。
4. 输入“并行派两个子 Agent：一个调查 CLI 入口，一个审查会话恢复；汇总结论”。展示独立上下文、并发调度和进度日志。
5. 输入“派两个编程子 Agent：一个只改 `demo_a.py`，另一个只改 `demo_b.py`；先审查各自差异再申请合并，最后运行相关测试并总结”。展示隔离编辑、`/agents`、差异审查与合并审批。
6. 输入“给一个两步计划并在我同意后写入 `demo.txt`”。展示计划及写文件审批。拒绝一次以展示无副作用，再重新请求并批准。
7. 输入 `/rewind` 查看文件检查点，选择一次写入并 `/rewind <ID>`，展示修改可回退。输入 `/mode plan` 请求调查，展示写入被禁止；再用 `/mode default` 返回审批模式。
8. 输入 `/context`、`/compact` 展示长会话管理；输入 `/rename 登录实验` 给会话命名。
9. 输入 `/model deepseek`（需要 DeepSeek Key）继续问“刚才我批准了什么？”展示上下文保留；也可以不切模型直接继续。
10. 退出并重新运行 `uv run codepilot --workspace <同一目录> --resume <显示的会话 ID>`，输入 `/history` 或继续问答。
11. 把 `examples/hooks.json` 复制到当前工作区的 `.codepilot/hooks.json`，运行 `/hooks` 查看配置，再尝试让模型删除 `src/` 下的文件；展示人工审批之后 `PreToolUse` 如何拒绝执行。
12. 准备一个简单 stdio MCP server，运行 `uv run codepilot mcp add demo -- python path/to/server.py`，再运行 `/mcp` 查看 `mcp__demo__...` 工具已加入；让模型调用该工具，展示它仍走审批与工具结果回传。
13. 运行 `uv run pytest -q` 展示不依赖真实 Key 的核心行为测试。

## 7. 常见面试问题与回答

**Q：LangChain 和 LangGraph 各做什么？**  
A：LangChain 定义模型、消息与工具的统一接口。LangGraph 编排这些接口组成循环状态机，并负责 checkpoint 与人工审批中断。

**Q：切换模型后上下文为什么还在？**  
A：上下文在 LangGraph 的 SQLite checkpoint 中，而不在模型对象里。切换只修改 alias，下一次模型节点会用同一个 `thread_id` 读取原消息。

**Q：如何保证重启不丢历史？**  
A：每次图执行以稳定的会话 ID 作为 `thread_id`，`SqliteSaver` 把消息和执行位置写入 SQLite。恢复时重新编译相同图并复用 `thread_id`。测试会关闭实例、重新创建实例，再检查历史可读且下一轮能看到旧消息。

**Q：用户拒绝工具会怎样？**  
A：工具不会执行。图将“Denied by user”作为该调用的 `ToolMessage` 写入对话，模型可以调整计划或解释无法继续。

**Q：LangGraph checkpoint 和文件检查点有什么区别？**  
A：前者保存对话消息及图执行位置，后者保存内置文件工具修改前后的文件内容。`/rewind` 只回退文件，并在会话中记录回退事件；不会把整个对话状态倒带。

**Q：Hook 和人工审批有什么区别？**  
A：人工审批是用户针对具体高风险调用给出的决定，并用 LangGraph `interrupt()` 持久化暂停。Hook 是用户预先配置的事件处理程序，每次匹配事件时自动运行。两者叠加：审批批准后，`PreToolUse` 仍可以拒绝；Hook 的放行不能越过用户拒绝。

**Q：MCP 和 Hook 有什么区别？**  
A：MCP 是工具扩展协议，目的是把外部 server 的 tools 加到模型可调用工具列表里；Hook 是生命周期拦截器，目的是在用户输入、审批、工具执行、模型停止等节点执行本地策略。MCP 工具会被包装成 LangChain `StructuredTool`，仍经过权限审批、Hook 和 LangGraph 工具节点；Hook 自己不是模型工具，不由模型主动选择调用。

**Q：MCP 是如何实现的？**  
A：项目读取 `.mcp.json` 的 `mcpServers`，启动 stdio server 后用换行分隔 JSON-RPC 发送 `initialize`、`tools/list`。拿到 MCP 工具的 `name`、`description`、`inputSchema` 后，包装成 `mcp__server__tool` 形式的 LangChain `StructuredTool`。模型调用这个工具时，执行层再发 `tools/call` 给对应 MCP server，并把返回的 text content 写成普通工具结果。权限层把 `mcp__` 工具默认设为 ask，计划模式 deny，所以外部能力不会绕过审批。

**Q：为什么权限模式要在工具执行层再次判断？**  
A：计划模式会从模型可见的工具列表移除写工具，但模型响应或历史状态仍可能包含写调用。执行层的策略是最后一道边界，不能只依赖提示词或模型工具 schema。

**Q：上下文压缩会丢信息吗？**  
A：会。摘要是有损的，所以提示模型保留目标、决定、关键路径、真实测试和未完成事项，并保留最近两条消息。重要长期约定应放进项目指令文件。`/context` 的 Token 估计也只是粗略值。

**Q：子 Agent 是不是另一个线程？**  
A：子 Agent 是独立的 LangChain `create_agent` 调用，拥有临时对话。批量派遣时用 Python 线程池并发运行，但它不是可长期恢复的主会话线程。调查子 Agent 只有读工具；编程子 Agent 可在隔离副本中修改指定路径。它们都不共享主会话 checkpoint，只把结论与变更摘要返回给主 Agent。

**Q：主 Agent 如何把编程任务分给子 Agent，并保证最后能合并？**  
A：先依据文件所有权和依赖关系拆任务。并行任务必须有互不重叠的可写路径；每个子 Agent 在项目副本中编辑，暂存结果和文件摘要。主 Agent 逐项审查 diff，再请求一次合并审批。合并时对比派遣时与当前主工作区的文件哈希，发现用户或其他任务改动就拒绝覆盖。合并后由主 Agent 跑测试、处理集成问题，最后只报告已合并且验证过的结果。依赖前项输出的任务按顺序派发。

**Q：为什么不给编程子 Agent 直接运行命令？**  
A：编程子 Agent 当前采用工作区文件副本并只提供限定路径的文件工具。主 Agent 的命令、Hook 和 MCP 已接入进程沙箱；后续为子 Agent 增加自测时，可以直接复用同一执行器并为副本设置更窄的策略。

**Q：你展示的是模型完整思维链吗？**  
A：不是。CLI 只显示模型服务商明确返回的 `reasoning_content` 或标准 reasoning 块，再加上真实工具调用与执行轨迹。不同模型和配置暴露的信息不同。Qwen 默认打开思考模式，并用 `ChatQwen` 保留其推理字段；这是可观察性功能，不把工具日志冒充模型内部推理。

**Q：如何避免工具调用失控？**  
A：限定工作区路径、要求批准副作用操作、使用无 Shell 进程沙箱、最小环境、命令超时、输出截断、进程树清理、内存与进程数限制；调查子 Agent 只读，编程子 Agent 在隔离副本中限定写入路径，主图还设置递归限制。

**Q：最值得继续改进什么？**  
A：自动压缩阈值目前按消息数和字符数判断，应按模型真实 token 预算调整；SQLite 适合单机，多用户服务应换支持并发的 checkpointer；审批通过后的工具执行若崩溃仍可能重放，应对外部副作用添加幂等键；网页搜索依赖第三方服务，生产环境需要稳定 API 和缓存。

## 8. 已知边界

- 真实模型的工具调用能力、上下文窗口和价格取决于模型服务；项目只统一本地接口。
- `run_command`、Hook 和 MCP 已使用统一的进程生命周期与资源沙箱；宿主文件系统和网络的强制隔离仍取决于后续 AppContainer、namespace/seccomp 后端。网页地址检查负责 Web 工具边界，不代表通用网络隔离。
- 文件工具按 UTF-8 文本处理；不处理二进制文件。`grep_code` 是 Python 实现，适合中小仓库，不追求 `ripgrep` 的大仓库性能。
- Web 搜索使用 `ddgs`，可能受网络环境和搜索服务策略影响。
- CLI 单进程单会话交互；SQLite 不是并发任务队列。对审批后执行副作用的严格 exactly-once 语义未做保证。
- 文件快照覆盖内置文件工具和已合并的子 Agent 文件修改，不覆盖 shell 命令、目录创建或外部改动。单个文件大于 2 MB 时，内置编辑会报错并尝试回滚；文件快照保存在本地 SQLite，会增加数据库大小。
- 编程子 Agent 的隔离是文件副本，不是进程或网络沙箱。副本最多复制 2000 个文件或 40 MB；单批最多 3 项任务、每项最多修改 40 个文件。大仓库与有依赖的改动应拆批或由主 Agent 执行。副本保存在 `.codepilot/subagents/`，会占用磁盘；当前不自动清理。
- MCP 当前只支持 stdio tools；HTTP/SSE、OAuth、resources、prompts 和 list_changed 尚未实现。MCP Server 使用最小环境、资源限制和进程树清理；操作系统级文件与网络隔离后端仍待接入。
- `/compact` 依赖当前模型生成摘要，摘要可能遗漏细节；自动阈值是启发式估计，不等于精确的上下文窗口判断。

## 9. 参考资料

- [LangGraph Python quickstart](https://docs.langchain.com/oss/python/langgraph/quickstart)
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangChain tools](https://docs.langchain.com/oss/python/langchain/tools)
- [LangChain model providers](https://docs.langchain.com/oss/python/concepts/providers-and-models)
- [LangChain DeepSeek integration](https://docs.langchain.com/oss/python/integrations/providers/deepseek)
- [LangChain Qwen integration](https://docs.langchain.com/oss/python/integrations/chat/qwq)
- [LangChain event streaming](https://docs.langchain.com/oss/python/langchain/event-streaming)
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents)
- [Claude Code 工作机制](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude Code 文件回退机制](https://code.claude.com/docs/en/agent-sdk/file-checkpointing)
