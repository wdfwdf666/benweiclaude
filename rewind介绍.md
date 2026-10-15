`rewind` 的本质可以这样讲：

> 项目的 `/rewind` 不是回滚 LangGraph 对话，而是给文件修改单独做了一套轻量级 checkpoint。每次内置文件工具真正执行前，我先保存受影响文件的 before 快照；工具执行后再保存 after 快照。如果 before 和 after 不一样，就把这次变化写入 SQLite。之后用户执行 `/rewind <checkpoint_id>` 时，我会把这个 checkpoint 以及它之后的所有文件修改一起还原到当时之前的状态。

核心代码在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:17>)。

执行文件工具时记录 checkpoint，位置在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:182>)：

```python
paths = self.file_checkpoints.affected_paths(call["name"], call["args"])
before = self.file_checkpoints.capture(paths)

result = self.toolset.by_name[call["name"]].invoke(call["args"])

after = self.file_checkpoints.capture(paths)
checkpoint_id = self.file_checkpoints.record(
    self.session_id, call["name"], before, after,
)
```

可以按这三步理解。

**1. 判断哪些文件会被影响**

在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:36>)：

```python
def affected_paths(self, tool_name: str, args: dict) -> list[Path]:
    if tool_name not in TRACKED_FILE_EDITS:
        return []
    names = ("source", "destination") if tool_name in {"move_file", "copy_file"} else ("path",)
    return list(dict.fromkeys(self.resolve_path(args[name]) for name in names))
```

它只追踪内置文件编辑工具，比如：

```text
write_file
edit_file
append_file
delete_file
move_file
copy_file
```

`run_command` 不追踪，因为命令可能改很多未知文件，不能可靠知道影响范围。

**2. 保存 before / after 快照**

在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:52>)：

```python
def capture(self, paths: list[Path]) -> dict[str, str | None]:
    return {str(path.relative_to(self.workspace)): self._read(path) for path in paths}
```

每个文件保存成：

```python
{
  "src/a.py": "base64后的文件内容",
  "src/b.py": None
}
```

这里 `None` 表示文件当时不存在。  
比如 `write_file` 创建新文件时：

```text
before = None
after = 文件内容
```

比如 `delete_file` 删除文件时：

```text
before = 原文件内容
after = None
```

文件内容用 base64 存，是为了能保存二进制内容，不只支持文本。

**3. 写入 SQLite**

在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:65>)：

```python
changes = [
    {"path": path, "before": old, "after": after[path]}
    for path, old in before.items() if old != after[path]
]
```

只有文件真的变化了，才创建 checkpoint：

```python
if not changes:
    return None
```

表结构是 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:24>)：

```sql
CREATE TABLE IF NOT EXISTS codepilot_file_checkpoints (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    changes_json TEXT NOT NULL
)
```

这里 `seq` 很重要，它代表修改顺序。后面 rewind 会靠它把目标 checkpoint 之后的改动一起回滚。

**真正 rewind 时做什么**

CLI 的 `/rewind` 在 [cli.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/cli.py:281>)。

如果只输入：

```text
/rewind
```

它会列出当前会话的文件检查点：

```python
rows = app.file_checkpoints.list(app.session_id)
```

如果输入：

```text
/rewind 8f21c0e7
```

会先让用户确认，然后调用：

```python
paths = app.rewind_files(value)
```

`app.rewind_files()` 在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:283>)：

```python
paths = self.file_checkpoints.rewind(self.session_id, checkpoint_prefix)
```

然后它还会往对话里追加一条消息：

```python
HumanMessage(content="[用户回退了文件修改] " + ", ".join(paths))
```

所以模型之后知道：用户刚才回退过文件。

**rewind 的核心算法**

在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:102>)：

```python
matching = [item for item in self.list(session_id) if item["id"].startswith(checkpoint_prefix)]
if len(matching) != 1:
    raise ValueError("Checkpoint ID not found or prefix is ambiguous")
```

支持输入短 ID，但必须唯一。

然后查出这个 checkpoint 以及之后的所有 checkpoint：

```python
rows = self.connection.execute(
    """SELECT changes_json FROM codepilot_file_checkpoints
       WHERE session_id = ? AND seq >= ? ORDER BY seq DESC""",
    (session_id, first_seq),
).fetchall()
```

为什么要回滚“这个 checkpoint 以及之后”的所有改动？

因为如果文件经历了：

```text
original -> first -> second
```

你要 rewind 到第一个 checkpoint 之前，就应该回到：

```text
original
```

而不是只撤销第一次修改。测试在 [test_checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/tests/test_checkpoints.py:25>) 就验证了这个行为。

它会构造两个字典：

```python
expected = {}
target = {}
```

逻辑在这里：

```python
expected.setdefault(path, change["after"])
target[path] = change["before"]
```

含义是：

- `expected`：当前磁盘上应该是什么内容
- `target`：最终要恢复成什么内容

举个例子：

```text
checkpoint1: original -> first
checkpoint2: first -> second
```

倒序读取时：

```text
expected = second
target = original
```

然后先做安全检查：

```python
if self._read(path) != expected[name]:
    raise ValueError(f"Cannot rewind: {name} changed since the checkpoint")
```

这一步非常关键。它防止覆盖用户后来手动改的内容。

比如 Agent 改完后，用户自己又改了这个文件。此时磁盘内容已经不是 checkpoint 记录的 after 了，`rewind` 就拒绝执行。

最后才真正恢复：

```python
self.restore_snapshot(target)
```

恢复逻辑在 [checkpoints.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/checkpoints.py:55>)：

```python
if encoded is None:
    if path.is_file():
        path.unlink()
else:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(encoded))
```

也就是：

- checkpoint 前文件不存在：回滚时删除文件
- checkpoint 前文件存在：写回当时的内容

如果恢复过程中失败，会尝试恢复回 `expected`，避免半回滚状态：

```python
except Exception:
    self.restore_snapshot(expected)
    raise
```

成功后删除这些已经被回滚的 checkpoint：

```python
DELETE FROM codepilot_file_checkpoints
WHERE session_id = ? AND seq >= ?
```

**面试简洁版**

你可以这样回答：

> `/rewind` 是我给文件工具做的一套独立回滚机制。它不回滚 LangGraph 对话，只回滚文件。  
>  
> 每次内置文件工具执行前，我先根据工具参数算出受影响路径，比如 `write_file` 是 `path`，`move_file` 是 `source` 和 `destination`。然后把这些文件的 before 内容读出来，用 base64 存一份。工具执行后再 capture 一次 after。如果 before 和 after 不同，就把 path、before、after、tool_name、session_id 写入 SQLite。  
>  
> 用户执行 `/rewind <id>` 时，我会找到这个 checkpoint，并把它之后的 checkpoint 一起倒序合并，算出当前磁盘应该是什么状态，以及目标要恢复到什么状态。恢复前会先检查磁盘内容是否还等于记录里的 after；如果用户或外部程序后来改过文件，就拒绝覆盖。检查通过后，才把文件写回 before 状态，并删除已经回滚的 checkpoint。  
>  
> 所以它的核心是：执行前后快照、SQLite 记录变化、回滚前做外部修改检测。它适合内置文件工具的可控修改，不覆盖 `run_command` 这种不可预测副作用。



因为这个项目里的 `/rewind` 设计目标是**回退文件状态**，不是**时间倒流整个 Agent 会话**。

如果把历史消息也回退到之前，会有几个麻烦点。

第一，LangGraph 的对话历史里不只是聊天文本，还包含模型工具调用、工具结果、审批中断状态、checkpoint 状态等。文件回滚只是把磁盘恢复了，但如果把消息也删回去，容易让图状态和真实世界不一致。

举个例子：

```text
用户：删除 docs/111.txt
模型：我要调用 delete_file
工具：Deleted docs/111.txt, File checkpoint: 519aee17fcdc
模型：文件已删除
用户：/rewind 519aee17fcdc
```

如果这时把历史消息倒回删除之前，模型以后就不知道用户曾经删过又撤销过。可是现实中这件事发生过，而且用户明确做了回退操作。更好的记忆是：

```text
前面的历史保留
新追加一条：[用户回退了文件修改] docs/111.txt
```

这样模型知道两件事：

```text
1. 之前确实执行过删除
2. 用户后来把它撤销了
```

第二，文件 checkpoint 和 LangGraph checkpoint 不是同一种 checkpoint。

文件 checkpoint 存的是：

```text
某些文件的 before / after 内容
```

LangGraph checkpoint 存的是：

```text
对话消息、节点位置、中断状态、图执行状态
```

`/rewind` 当前只知道文件怎么恢复，不知道“应该回到哪一个 LangGraph 图状态”。一个文件 checkpoint 可能发生在一次工具调用中间，也可能一轮里有多个工具调用；直接回退图状态会牵扯到很多边界。

第三，保留历史更安全。  
因为如果用户回退文件后又问：

```text
刚才你做了什么？为什么要回退？
```

保留历史时，模型能回答。  
如果把历史也删了，模型会失去这段上下文，反而像“失忆”。

所以当前设计是：

```text
文件状态：真的回滚
对话历史：不删除，只追加一条回退事件
```

代码就在 [agent.py](</C:/Users/524/Desktop/简历/benweiclaude/src/codepilot/agent.py:287>)：

```python
paths = self.file_checkpoints.rewind(self.session_id, checkpoint_prefix)
self.graph.update_state(
    config,
    {"messages": [HumanMessage(content="[用户回退了文件修改] " + ", ".join(paths))]},
    as_node="model",
)
```

这条消息相当于给模型一个状态同步通知：

```text
你之前看到的文件修改，现在已经被用户撤销了。
```

面试可以这样说：

> 我没有把历史消息一起回退，是因为 `/rewind` 的语义是撤销文件修改，不是撤销整个会话。对话历史里包含模型决策、工具结果和审批记录，这些都是已经发生过的事实。回退文件后，我会追加一条 HumanMessage 通知模型哪些文件被恢复，这样既保持审计轨迹，又让后续模型上下文和当前磁盘状态同步。