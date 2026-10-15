```text
### 面试参考回答（结构化、清晰简洁，约1分钟讲完核心）
我理解 Hook 本质上是**符合开闭原则的扩展点机制**：不用修改原有核心流程代码，就能在流程的特定节点插入自定义逻辑，实现拦截、增强或改写原有行为的效果。

它的实现核心可以拆成三个关键环节：
1. **预埋挂载点**：在主流程的关键节点（比如函数执行前、执行后、异常抛出时）提前预留调用入口，同时约定好钩子的入参（上下文信息）和返回值格式规范。
2. **钩子注册机制**：通过事件订阅、全局注册表等方式，允许外部把自定义的钩子逻辑绑定到对应的挂载点上，支持灵活插拔。
3. **调度执行**：主流程运行到挂载点时，会按约定顺序调用所有绑定的钩子函数，传入当前上下文；同时根据钩子的返回结果，决定主流程是正常继续、修改入参，还是直接中断。

举个具象的例子，比如工具调用的前置钩子 `PreToolUse`：
我们在每次执行删除文件工具前，都会触发这个挂载点，把工具名、待删除的文件路径传给钩子；如果钩子返回「拒绝」，就直接终止删除操作；没有异议就正常执行原工具。

不同场景的具体实现会有差异：框架级钩子（比如Webpack、Vue生命周期）大多基于发布订阅模式；系统级钩子会通过替换函数地址、拦截消息实现；React Hooks 则是基于组件渲染顺序，在 fiber 节点上存储状态来调度执行。

---

### 面试小技巧
- 如果面试官是针对你项目里的 Hook 提问，直接把例子换成你项目里的真实场景（比如Agent工具调用钩子、请求拦截钩子），说服力更强。
- 如果对方继续追问“手写一个最简Hook”，可以用十几行伪代码快速实现一个事件订阅式的前置钩子，核心就是`on`注册、`trigger`执行两个方法。
```



```text
可以这样回答，控制在 1 分钟左右：

> 我这个 Hook 参考 Claude Code 的思路，本质上是给 Agent 生命周期加了一层事件拦截器。  
>  
> 项目启动时会读取工作区 `.codepilot/hooks.json`，里面配置了事件名、matcher 和要执行的 command。比如 `PreToolUse` 匹配 `delete_file` 时，就执行一个 Python 脚本。  
>  
> 当 Agent 运行到关键节点时，我会主动调用 `HookManager.run(event, payload)`。比如用户输入前触发 `UserPromptSubmit`，工具执行前触发 `PreToolUse`，工具执行后触发 `PostToolUse`，子 Agent 派遣前触发 `SubagentStart`，模型回答结束前触发 `Stop`。  
>  
> `HookManager` 会把当前事件信息，比如 session_id、workspace、tool_name、tool_input，通过 JSON stdin 传给外部命令。命令执行完后，如果返回 `decision: block`、退出码是 2，或者返回 Claude Code 风格的 `permissionDecision: deny`，我就把它转换成 `HookResult(blocked=True, reason=...)`。  
>  
> 不同事件收到 blocked 后处理方式不同：`PreToolUse` 会阻止真实工具执行，并把拒绝原因写成 `ToolMessage` 反馈给模型；`PermissionRequest` 会在弹出人工审批前直接拒绝；`SubagentStart` 会阻止子 Agent 派遣；`Stop` 会要求模型继续补一轮。  
>  
> 所以 Hook 不依赖模型自己遵守规则，而是在 Agent 执行链路里做程序级拦截。LangGraph 负责主流程和审批恢复，HookManager 负责事件匹配、外部命令执行和结果翻译。

如果面试官追问“和人工审批是什么关系”，你可以补一句：

> 人工审批是 LangGraph `interrupt()` 暂停出来让用户决策；Hook 是预配置的自动策略。流程上是先审批，审批通过后还会跑 `PreToolUse`，所以 Hook 可以作为最后一道自动安全门，但 Hook 不能绕过用户拒绝。

```

```text
可以把它讲得更“本质”一点：

> Hook 的本质不是 AI 能力，而是**在主流程的关键位置预留回调点**。  
>  
> 我的 Agent 正常流程是：用户输入 → 模型思考 → 模型提出工具调用 → 执行工具 → 把结果交回模型 → 输出答案。  
>  
> Hook 就是在这些步骤之间插入一些固定检查点，比如：
>  
> `工具执行前`、`工具执行后`、`子 Agent 启动前`、`用户输入进入模型前`、`模型准备结束前`。
>  
> 每到一个检查点，我都会把当前上下文打包成 JSON，比如当前 session、工具名、工具参数、工作目录，然后交给用户配置的外部脚本处理。脚本返回结果后，主流程根据结果决定继续、阻止，或者给模型追加上下文。
>  
> 所以 Hook 的核心是三件事：
>  
> 1. **埋点**：在 Agent 生命周期的关键位置调用 `HookManager.run()`。
> 2. **外部化策略**：具体规则不写死在 Agent 里，而是放到 `.codepilot/hooks.json` 和脚本里。
> 3. **把脚本结果重新接回主流程**：比如阻止工具执行、拒绝子 Agent 派遣、要求模型继续回答，或者把额外反馈塞回模型上下文。

你可以用一个例子讲：

> 比如模型想调用 `delete_file` 删除文件。  
>  
> 在真正执行 `delete_file` 前，我会触发 `PreToolUse` Hook，把 `tool_name=delete_file`、`tool_input={path: ...}` 传给脚本。  
>  
> 如果脚本判断这是 `src/` 下的文件，就返回 deny。主 Agent 收到 deny 后，不会执行删除，而是生成一个 `ToolMessage` 告诉模型“这个工具被 Hook 拒绝了”。模型再根据这个结果换方案或向用户解释。  
>  
> 这就是 Hook 的本质：**不是让模型自己记住规则，而是在执行链路里加程序级拦截点。**

再压缩成一句面试金句：

> Hook 本质上是 Agent 执行流里的可配置中间件：在关键生命周期节点暂停一下，把上下文交给外部规则判断，再把判断结果接回 Agent 主流程。
```








可以。你这个项目里的 Hook 可以理解成一句话：

Hook 是 CodePilot 自己实现的“生命周期事件拦截器”：Agent 运行到某些关键点时，主动调用 `HookManager.run(event, payload)`，`HookManager` 根据 `.codepilot/hooks.json` 找到匹配的外部命令，把事件 JSON 通过 stdin 传给命令，再根据命令返回值决定“放行、阻止、追加上下文”。

核心文件是这几个：

- [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:12>)：Hook 引擎本体
- [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:61>)：主 Agent 在各个生命周期点触发 Hook
- [cli.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/cli.py:192>)：人工审批前触发 `PermissionRequest`
- [subagents.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/subagents.py:49>)：子 Agent 派遣前触发 `SubagentStart`
- [test_hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/tests/test_hooks.py:48>)：Hook 行为测试

**一、Hook 配置从哪里来**

项目会读取当前工作区下面的：

```text
.codepilot/hooks.json
```

读取逻辑在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:35>)：

```python
self.path = self.workspace / ".codepilot" / "hooks.json"
self.reload()
```

如果文件不存在，Hook 就是空的：

```python
if not self.path.exists():
    self.hooks = {}
    return
```

示例配置在 [examples/hooks.json](</C:/Users/524/Desktop/简历/benweiclaude/examples/hooks.json:1>)：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "delete_file",
        "hooks": [
          {
            "type": "command",
            "command": "python",
            "args": ["examples/hooks/block_delete.py"],
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

意思是：当 `PreToolUse` 事件发生，并且工具名匹配 `delete_file` 时，执行：

```bash
python examples/hooks/block_delete.py
```

**二、支持哪些 Hook 事件**

事件列表定义在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:12>)：

```python
EVENTS = frozenset({
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
    "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Stop",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch",
})
```

其中不是每个事件都能阻止流程。能阻止的事件在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:18>)：

```python
BLOCKABLE = frozenset({
    "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "Stop", "PreCompact", "PreModelSwitch", "SubagentStart",
})
```

也就是说：

- `PreToolUse` 可以阻止工具执行
- `PermissionRequest` 可以在弹出人工审批前直接拒绝
- `UserPromptSubmit` 可以阻止用户输入进入模型
- `Stop` 可以要求模型继续处理
- `SubagentStart` 可以阻止派遣子 Agent
- `PostToolUse` 不能阻止，因为工具已经执行完了，但它可以追加反馈上下文给模型

**三、HookManager.run 做了什么**

最关键的方法是 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:94>)：

```python
def run(self, event: str, *, match: str = "", **payload) -> HookResult:
```

它做 5 件事。

1. 检查事件名是否合法：

```python
if event not in EVENTS:
    raise ValueError(f"Unknown hook event: {event}")
```

2. 拼出传给 Hook 命令的 JSON：

```python
input_data = {
    "hook_event_name": event,
    "session_id": payload.get("session_id"),
    "cwd": str(self.workspace),
    **payload,
}
```

比如工具执行前，传进去的大概是：

```json
{
  "hook_event_name": "PreToolUse",
  "session_id": "03a11afc...",
  "cwd": "C:\\Users\\524\\Desktop\\简历\\benweiclaude",
  "tool_name": "delete_file",
  "tool_input": {
    "path": "src/codepilot/agent.py"
  },
  "tool_use_id": "call_xxx"
}
```

3. 根据 matcher 判断是否命中：

```python
if not self._matches(group.get("matcher", "*"), match):
    continue
```

`match` 是调用方传进来的匹配对象。比如：

- `PreToolUse` 的 `match` 是工具名，例如 `write_file`
- `SubagentStart` 的 `match` 是子 Agent 角色，例如 `explore`
- `PreModelSwitch` 的 `match` 是目标模型，例如 `qwen`

匹配逻辑在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:75>)：

```python
if pattern in {"", "*"}:
    return True
if re.fullmatch(r"[\w\- ,|]+", pattern):
    return value in {part.strip() for part in re.split(r"[,|]", pattern)}
return re.search(pattern, value) is not None
```

所以 matcher 支持：

```json
"matcher": "*"
"matcher": "write_file"
"matcher": "write_file,edit_file"
"matcher": "write_file|edit_file"
"matcher": ".*file"
```

4. 启动外部命令：

```python
completed = subprocess.run(
    [handler["command"], *handler.get("args", [])],
    input=json.dumps(input_data, ensure_ascii=False),
    text=True,
    capture_output=True,
    cwd=self.workspace,
    timeout=handler.get("timeout", 10),
    check=False,
)
```

这里有几个细节很好面试时讲：

- `shell=False`，避免 shell 注入风险
- Hook 命令工作目录固定为 workspace
- 事件数据通过 stdin 传入，不拼进命令行参数
- stdout/stderr 会截断到 `MAX_OUTPUT = 64000`
- timeout 默认 10 秒，最大 120 秒

5. 把 Hook 输出翻译成 `HookResult`：

```python
return HookResult(blocked, "; ".join(reasons), "\n".join(contexts))
```

`HookResult` 定义在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:25>)：

```python
@dataclass(frozen=True)
class HookResult:
    blocked: bool = False
    reason: str = ""
    context: str = ""
```

**四、Hook 怎么表示拒绝**

在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:126>)：

```python
denied = (
    completed.returncode == 2
    or output.get("decision") == "block"
    or specific.get("permissionDecision") == "deny"
)
```

所以 Hook 有 3 种拒绝方式。

第一种：退出码 2，用 stderr 作为原因：

```python
import sys

sys.stderr.write("不允许删除 src 文件")
sys.exit(2)
```

第二种：stdout 输出 JSON：

```json
{
  "decision": "block",
  "reason": "不允许这个操作"
}
```

第三种：兼容 Claude Code 风格的 `hookSpecificOutput`：

```json
{
  "hookSpecificOutput": {
    "permissionDecision": "deny",
    "permissionDecisionReason": "src/ 下的文件不能删除"
  }
}
```

示例脚本在 [block_delete.py](</C:/Users/524/Desktop/简历/benweiclaude/examples/hooks/block_delete.py:6>)：

```python
event = json.load(sys.stdin)
path = str(event.get("tool_input", {}).get("path", "")).replace("\\", "/")
if event.get("tool_name") == "delete_file" and path.startswith("src/"):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "src/ 下的文件不能通过 delete_file 删除",
        }
    }, ensure_ascii=False))
```

意思是：如果模型想通过 `delete_file` 删除 `src/` 下的文件，就拒绝。

**五、PreToolUse 的完整执行链路**

这是最关键的面试点。

主 Agent 的 LangGraph 节点是：

```python
model -> tools -> model
```

定义在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:70>)：

```python
builder.add_node("model", self._call_model)
builder.add_node("tools", self._call_tools)
builder.add_edge(START, "model")
builder.add_conditional_edges("model", self._route, {"tools": "tools", "end": END})
builder.add_edge("tools", "model")
```

当模型返回工具调用时，进入 `_call_tools()`。关键代码在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:147>)。

先找出需要审批的高风险工具：

```python
risky = [
    {"id": call["id"], "name": call["name"], "args": call["args"]}
    for call in calls if decide(self.permission_mode, call["name"]).kind == "ask"
]
```

如果需要人工审批，就调用 LangGraph 的 `interrupt()`：

```python
decisions = interrupt({"type": "tool_approval", "requests": risky}) if risky else {}
```

等用户批准恢复以后，才会继续往下走。

然后执行权限判断：

```python
if policy.kind == "deny":
    result = f"Denied by permission mode: {policy.reason}."
elif policy.kind == "ask" and decisions.get(call["id"]) is not True:
    result = "Denied by user; no action was taken."
else:
    pre = self.hooks.run("PreToolUse", ...)
```

重点：`PreToolUse` 在人工审批之后、真正工具执行之前。

这很合理，因为：

- 用户没批准，Hook 不需要跑
- 用户批准了，Hook 还能做最后一道自动策略检查
- Hook 拒绝不能被人工审批绕过

如果 `PreToolUse` 阻止了：

```python
if pre.blocked:
    result = f"Denied by PreToolUse hook: {pre.reason}"
    self._emit("tool_denied", name=call["name"], call_id=call["id"])
    messages.append(ToolMessage(...))
    continue
```

也就是说，工具不会执行，但模型会收到一个 `ToolMessage`，内容类似：

```text
Denied by PreToolUse hook: src/ 下的文件不能通过 delete_file 删除
```

然后 LangGraph 会回到 `model` 节点，让模型基于这个工具结果继续回答或换方案。

如果 Hook 放行，才执行真正工具：

```python
result = self.toolset.by_name[call["name"]].invoke(call["args"])
```

执行完再跑：

```python
post = self.hooks.run(
    "PostToolUse" if succeeded else "PostToolUseFailure",
    ...
)
```

如果 Post Hook 返回 `additionalContext`，会追加到工具结果里：

```python
if post.context:
    result = f"{result}\n[Hook feedback]\n{post.context}"
```

这就让 Hook 可以做“执行后反馈”，比如：

- 跑格式检查
- 提醒测试失败
- 给模型补充 CI 结果
- 要求模型修复某个问题

**六、PermissionRequest 和 PreToolUse 的区别**

这两个很像，但位置不同。

`PermissionRequest` 在 CLI 里，位置是 [cli.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/cli.py:192>)：

```python
while "__interrupt__" in result:
    payload = result["__interrupt__"][0].value
    requests = payload.get("requests", [])
```

这里说明 LangGraph 已经暂停了，CLI 正准备问用户：

```text
批准此工具调用？[y/N]
```

在真正问用户之前，先跑：

```python
permission_hook = app.hooks.run(
    "PermissionRequest",
    match=request["name"],
    session_id=app.session_id,
    tool_name=request["name"],
    tool_input=request["args"],
    tool_use_id=request["id"],
)
```

如果 Hook 拒绝：

```python
if permission_hook.blocked:
    print(f"[Hook 拒绝] {permission_hook.reason}")
    decisions[request["id"]] = False
    continue
```

也就是连人工审批提示都不弹，直接把这个工具决定设置为 `False`。

区别可以这样记：

```text
PermissionRequest：审批界面出现前，提前拒绝某个请求
PreToolUse：审批完成后、工具执行前，最后拦截
```

所以 `PermissionRequest` 更像“审批策略过滤器”，`PreToolUse` 更像“执行前守门员”。

**七、UserPromptSubmit 怎么工作**

用户输入进入 LangGraph 前，会先触发 `UserPromptSubmit`。位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:351>)：

```python
prompt_hook = self.hooks.run(
    "UserPromptSubmit",
    session_id=self.session_id,
    prompt=message
)
```

如果阻止：

```python
if prompt_hook.blocked:
    raise ValueError(f"Prompt blocked by hook: {prompt_hook.reason}")
```

如果 Hook 返回上下文：

```python
if prompt_hook.context:
    message = f"{message}\n\n[Hook context]\n{prompt_hook.context}"
```

这可以实现类似：

- 用户输入前做敏感信息检查
- 自动补充项目规则
- 根据输入内容注入额外上下文
- 阻止某些危险请求进入模型

**八、Stop Hook 怎么工作**

`Stop` 是模型准备结束一轮回答时触发。位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:384>)：

```python
while "__interrupt__" not in result:
    stop = self.hooks.run("Stop", session_id=self.session_id,
                          response=str(result["messages"][-1].content))
```

如果 Stop Hook 不阻止，就返回最终结果：

```python
if not stop.blocked:
    return result
```

如果 Stop Hook 阻止，说明 Hook 认为“还不能结束，需要继续处理”。于是它会追加一条新的用户消息：

```python
result = self.graph.invoke({"messages": [HumanMessage(
    content=f"[Stop Hook 要求继续] {stop.reason}。请继续处理并重新回答。",
)]}, config=self.config())
```

最多继续两次：

```python
if retries > 2:
    raise ValueError(f"Stop hook blocked the answer repeatedly: {stop.reason}")
```

这个设计可以实现：

- 回答结束前检查是否运行测试
- 检查是否遗漏总结
- 检查是否还有未处理 todo
- 要求模型再补一轮验证

测试在 [test_hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/tests/test_hooks.py:136>)，它验证 Stop Hook 第一次 block 后，模型确实又跑了一轮。

**九、子 Agent Hook 怎么接进去**

子 Agent 自己不直接知道 HookManager。它只负责发事件：

```python
gate = self._emit("subagent_start", id=agent_id, role=role, task=task)
```

位置在 [subagents.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/subagents.py:49>)。

`event_sink` 实际上传的是主 Agent 的 `_emit()`。主 Agent 的 `_emit()` 在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:78>)：

```python
if event == "subagent_start":
    hook_result = self.hooks.run(
        "SubagentStart",
        match=data.get("role", ""),
        session_id=self.session_id,
        agent_id=data.get("id"),
        agent_type=data.get("role"),
        task=data.get("task"),
    )
```

如果 Hook 拒绝，子 Agent 就不会真正创建和运行：

```python
if gate is not None and getattr(gate, "blocked", False):
    reason = f"SubagentStart hook blocked delegation: {gate.reason}"
    return {"status": "failed", "summary": reason, ...}
```

所以链路是：

```text
SubagentRunner.run_one
  -> _emit("subagent_start")
  -> CodePilot._emit
  -> HookManager.run("SubagentStart")
  -> 如果 blocked，直接返回 failed
```

测试在 [test_hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/tests/test_hooks.py:158>)。

**十、模型切换 Hook 怎么工作**

模型切换前后分别触发 `PreModelSwitch` 和 `PostModelSwitch`。位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:245>)：

```python
pre = self.hooks.run(
    "PreModelSwitch",
    match=alias,
    session_id=self.session_id,
    previous_model=previous,
    model=alias
)
```

如果阻止：

```python
if pre.blocked:
    raise ValueError(f"Model switch blocked by hook: {pre.reason}")
```

放行后才真正切换：

```python
self.model_alias = alias
self.store.touch(self.session_id, model=alias)
```

切完之后触发：

```python
self.hooks.run("PostModelSwitch", ...)
```

这可以实现：

- 禁止切到某个模型
- 记录模型切换审计日志
- 切模型前检查 API key 是否存在
- 切模型后写入统计文件

**十一、上下文压缩 Hook**

上下文压缩在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:296>)：

```python
pre = self.hooks.run(
    "PreCompact",
    match=trigger,
    session_id=self.session_id,
    trigger=trigger,
    message_count=len(messages)
)
```

如果 Hook 阻止：

```python
if pre.blocked:
    raise ValueError(f"Compaction blocked by hook: {pre.reason}")
```

压缩完成后：

```python
self.hooks.run(
    "PostCompact",
    match=trigger,
    session_id=self.session_id,
    trigger=trigger,
    before=len(messages),
    after=after
)
```

这里的 `trigger` 可能是：

```text
manual
auto
```

所以你可以做：

```json
{
  "hooks": {
    "PreCompact": [
      {
        "matcher": "manual",
        "hooks": [...]
      }
    ]
  }
}
```

**十二、SessionStart / SessionEnd**

新建会话时触发，位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:224>)：

```python
self.hooks.run(
    "SessionStart",
    match="startup",
    session_id=self.session_id,
    source="startup",
    model=self.model_alias
)
```

恢复会话时也触发，位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:235>)：

```python
self.hooks.run(
    "SessionStart",
    match="resume",
    session_id=self.session_id,
    source="resume",
    model=self.model_alias
)
```

关闭时触发，位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:403>)：

```python
self.hooks.run("SessionEnd", session_id=self.session_id, reason="exit")
```

这些事件主要用来做日志、审计、初始化环境，不负责阻止。

**十三、调试输出怎么来的**

HookManager 本身不直接 print，它通过 `_emit()` 发事件。比如 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:110>)：

```python
self._emit("hook_start", hook_event=event, command=handler["command"])
```

CLI 的 `ConsoleTrace` 负责把这些事件打印出来。位置在 [cli.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/cli.py:117>)：

```python
elif event == "hook_start":
    print(f"[Hook] {data['hook_event']} → {data['command']}", flush=True)
elif event == "hook_result" and data.get("blocked"):
    print(f"[Hook] {data['hook_event']} 已阻止操作", flush=True)
elif event == "hook_error":
    print(f"[Hook 错误] {data['hook_event']}: {data['error']}", flush=True)
```

所以你运行 CLI 时能看到：

```text
[Hook] PreToolUse → python
[Hook] PreToolUse 已阻止操作
```

**十四、异常策略**

Hook 命令失败时，当前实现是 fail-open，也就是记录错误，但不阻断主流程。代码在 [hooks.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/hooks.py:141>)：

```python
except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
    self._emit("hook_error", hook_event=event,
               error=f"{type(exc).__name__}: {exc}")
```

这意味着：

- Hook 脚本不存在：打印 Hook 错误，继续
- Hook 超时：打印 Hook 错误，继续
- Hook 输出不是 JSON：打印 Hook 错误，继续
- Hook 退出码不是 0 或 2：打印 Hook 错误，继续

面试时可以这样解释：  
“我这里选择 fail-open，是为了避免 Hook 脚本故障导致整个编程助手不可用。真正安全策略仍然由 permission mode 和人工审批兜底；Hook 是增强控制层。”

**十五、一条完整链路总结**

用户让模型改文件时，完整流程是：

```text
用户输入
  -> UserPromptSubmit Hook
  -> LangGraph model 节点
  -> 模型产生 tool_calls
  -> LangGraph tools 节点
  -> 如工具高风险，interrupt 暂停
  -> CLI 展示审批
  -> PermissionRequest Hook
  -> 用户批准或拒绝
  -> Command(resume=decisions) 恢复图
  -> PreToolUse Hook
  -> 真正执行工具
  -> PostToolUse / PostToolUseFailure Hook
  -> 工具结果写成 ToolMessage
  -> 回到 model 节点
  -> 模型生成最终回答
  -> Stop Hook
  -> 如 Stop 要求继续，则再跑一轮
  -> 返回最终回答
```

这里最核心的设计点是：

```text
LangGraph interrupt 负责“持久化暂停和恢复”
HookManager 负责“事件匹配、执行外部命令、翻译阻止结果”
ToolMessage 负责“把 Hook/工具结果反馈给模型”
ConsoleTrace 负责“把 Hook 调试过程打印给用户”
```

这套实现虽然是 Claude Code 风格，但它已经和你项目里的 LangGraph 流程真正打通了：审批、工具执行、子 Agent、模型切换、上下文压缩、会话开始结束、最终回答前检查，都可以通过 `.codepilot/hooks.json` 做扩展。