"""Deterministic engineering benchmarks for CodePilot.

The benchmark intentionally uses local fake models and local subprocesses. It
measures CodePilot's orchestration overhead without API latency or model drift.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from codepilot.agent import CodePilot
from codepilot.checkpoints import FileCheckpoints
from codepilot.hooks import EVENTS, HookManager
from codepilot.mcp import McpManager, add_stdio_server
from codepilot.memory import MemoryManager
from codepilot.subagents import SubagentRunner
from codepilot.tools import ToolSet


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * quantile)))
    return ordered[index]


def latency(values: list[float]) -> dict[str, float]:
    return {
        "mean_ms": round(statistics.fmean(values), 3),
        "p50_ms": round(percentile(values, 0.50), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
    }


class InstantModel(BaseChatModel):
    delay_seconds: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "codepilot-evaluation-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="完成"))])


class Registry:
    def __init__(self, model=None):
        self.model = model or InstantModel()

    def get(self, alias):
        return self.model


def tool_inventory(root: Path) -> dict:
    with tempfile.TemporaryDirectory(dir=root) as raw:
        workspace = Path(raw)
        memory = MemoryManager(workspace / "state.db", workspace)
        toolset = ToolSet(
            workspace, Registry(), lambda: "benchmark", lambda: "deepseek",
            enable_delegation=True, memory=memory,
        )
        result = {
            "builtin_tools": len(toolset.tools),
            "unique_tool_names": len({tool.name for tool in toolset.tools}),
            "hook_events": len(EVENTS),
        }
        memory.close()
        return result


def memory_benchmark(root: Path, distractors: int = 500) -> dict:
    lexical_cases = [
        ("JWT认证", "认证模块采用 JWT认证，并位于 src/auth.py"),
        ("会话断点恢复", "SqliteSaver 用于会话断点恢复"),
        ("危险工具审批", "危险工具审批通过 interrupt 暂停"),
        ("MCP工具协议", "MCP工具协议负责外部工具发现"),
        ("并行子代理", "并行子代理使用线程池调度"),
        ("撤销文件修改", "rewind 可以撤销文件修改"),
        ("生命周期钩子", "生命周期钩子会在工具前后触发"),
        ("任务清单", "任务清单保存在 SQLite 中"),
        ("模型热切换", "模型热切换保留同一会话状态"),
        ("中文词法检索", "中文词法检索使用 bigram 回退"),
    ]
    # These queries describe the same facts without sharing useful lexical tokens.
    # A lexical-only store is expected to struggle; this is the vector-search gap.
    semantic_cases = [
        ("登录鉴权放在哪里", "身份校验逻辑使用令牌机制，代码位于 src/security.py"),
        ("如何把代码恢复到之前", "文件快照能够还原工作区的旧版本"),
        ("怎样减少聊天占用", "早期消息会被整理成结构化概要"),
        ("怎么连接外部能力", "标准化服务协议负责发现并执行远端函数"),
        ("重启后继续交流", "持久化检查点允许进程重启后接续历史状态"),
    ]
    with tempfile.TemporaryDirectory(dir=root) as raw:
        workspace = Path(raw)
        manager = MemoryManager(workspace / "state.db", workspace)
        expected: dict[str, str] = {}
        for query, content in lexical_cases + semantic_cases:
            item = manager.save(scope="project", memory_type="fact", content=content)
            expected[query] = item["id"]
        for index in range(distractors):
            manager.save(
                scope="project", memory_type="note",
                content=f"无关记录 {index}：模块编号 component_{index} 的普通配置说明",
            )

        times: list[float] = []

        def score(cases: list[tuple[str, str]]) -> dict:
            hit1 = hit5 = 0
            reciprocal_ranks = []
            for query, _ in cases:
                started = time.perf_counter()
                result = manager.retrieve(query, limit=5)
                times.append((time.perf_counter() - started) * 1000)
                ids = [item["id"] for item in result]
                target = expected[query]
                if ids and ids[0] == target:
                    hit1 += 1
                if target in ids:
                    hit5 += 1
                    reciprocal_ranks.append(1 / (ids.index(target) + 1))
                else:
                    reciprocal_ranks.append(0)
            return {
                "queries": len(cases),
                "recall_at_1_pct": round(100 * hit1 / len(cases), 1),
                "recall_at_5_pct": round(100 * hit5 / len(cases), 1),
                "mrr_at_5": round(statistics.fmean(reciprocal_ranks), 3),
            }

        result = {
            "corpus_size": distractors + len(lexical_cases) + len(semantic_cases),
            "fts5_enabled": manager.fts_enabled,
            "lexical": score(lexical_cases),
            "semantic_synonym": score(semantic_cases),
        }
        result["retrieval_latency"] = latency(times)
        manager.close()
        return result


def session_benchmark(root: Path, trials: int = 20) -> dict:
    durations: list[float] = []
    correct = 0
    for _ in range(trials):
        with tempfile.TemporaryDirectory(dir=root) as raw:
            workspace = Path(raw)
            db = workspace / "state.db"
            first = CodePilot(workspace, db, Registry())
            session_id = first.new_session()
            first.send("需要持久化的消息")
            first.close()
            started = time.perf_counter()
            second = CodePilot(workspace, db, Registry())
            second.resume(session_id)
            history = second.history()
            durations.append((time.perf_counter() - started) * 1000)
            correct += int(any(getattr(item, "content", "") == "需要持久化的消息" for item in history))
            second.close()
    return {
        "trials": trials,
        "resume_success_pct": round(100 * correct / trials, 1),
        "resume_latency": latency(durations),
    }


def checkpoint_benchmark(root: Path, trials: int = 30) -> dict:
    rewind_times: list[float] = []
    rewind_success = 0
    conflicts_blocked = 0
    with tempfile.TemporaryDirectory(dir=root) as raw:
        workspace = Path(raw)

        def resolve(name: str) -> Path:
            candidate = (workspace / name).resolve()
            candidate.relative_to(workspace.resolve())
            return candidate

        checkpoints = FileCheckpoints(workspace / "state.db", workspace, resolve)
        for index in range(trials):
            path = workspace / f"file-{index}.txt"
            path.write_text("before" * 512, encoding="utf-8")
            before = checkpoints.capture([path])
            path.write_text("after" * 512, encoding="utf-8")
            after = checkpoints.capture([path])
            checkpoint_id = checkpoints.record("benchmark", "write_file", before, after)
            started = time.perf_counter()
            checkpoints.rewind("benchmark", checkpoint_id or "")
            rewind_times.append((time.perf_counter() - started) * 1000)
            rewind_success += int(path.read_text(encoding="utf-8") == "before" * 512)

        for index in range(10):
            path = workspace / f"conflict-{index}.txt"
            path.write_text("before", encoding="utf-8")
            before = checkpoints.capture([path])
            path.write_text("assistant", encoding="utf-8")
            after = checkpoints.capture([path])
            checkpoint_id = checkpoints.record("conflicts", "write_file", before, after)
            path.write_text("user", encoding="utf-8")
            try:
                checkpoints.rewind("conflicts", checkpoint_id or "")
            except ValueError:
                conflicts_blocked += 1
        checkpoints.close()
    return {
        "rewind_trials": trials,
        "rewind_success_pct": round(100 * rewind_success / trials, 1),
        "external_conflict_block_pct": round(100 * conflicts_blocked / 10, 1),
        "rewind_latency": latency(rewind_times),
    }


def hook_benchmark(root: Path, trials: int = 20) -> dict:
    with tempfile.TemporaryDirectory(dir=root) as raw:
        workspace = Path(raw)
        script = workspace / "hook.py"
        script.write_text(
            "import json,sys\n"
            "data=json.load(sys.stdin)\n"
            "deny=data.get('tool_input',{}).get('deny',False)\n"
            "print(json.dumps({'decision':'block','reason':'benchmark deny'} if deny else {}))\n",
            encoding="utf-8",
        )
        config_dir = workspace / ".codepilot"
        config_dir.mkdir()
        (config_dir / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{
            "matcher": "*", "hooks": [{
                "type": "command", "command": sys.executable, "args": [str(script)], "timeout": 5,
            }],
        }]}}), encoding="utf-8")
        manager = HookManager(workspace)
        times: list[float] = []
        correct = 0
        for index in range(trials):
            should_deny = index % 2 == 1
            started = time.perf_counter()
            result = manager.run("PreToolUse", match="write_file", tool_name="write_file",
                                 tool_input={"deny": should_deny})
            times.append((time.perf_counter() - started) * 1000)
            correct += int(result.blocked == should_deny)
        return {
            "trials": trials,
            "decision_accuracy_pct": round(100 * correct / trials, 1),
            "subprocess_hook_latency": latency(times),
        }


def write_mcp_server(workspace: Path) -> Path:
    script = workspace / "fake_mcp.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " msg=json.loads(line); method=msg.get('method')\n"
        " if 'id' not in msg: continue\n"
        " if method=='initialize': result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'eval','version':'1'}}\n"
        " elif method=='tools/list': result={'tools':[{'name':'echo','description':'Echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]}\n"
        " elif method=='tools/call': result={'content':[{'type':'text','text':'echo:'+msg['params']['arguments']['text']}]}\n"
        " else: result={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    return script


def mcp_benchmark(root: Path, trials: int = 20) -> dict:
    with tempfile.TemporaryDirectory(dir=root) as raw:
        workspace = Path(raw)
        script = write_mcp_server(workspace)
        add_stdio_server(workspace, "local", sys.executable, [str(script)])
        started = time.perf_counter()
        manager = McpManager(workspace)
        discovery_ms = (time.perf_counter() - started) * 1000
        try:
            times: list[float] = []
            correct = 0
            for index in range(trials):
                started = time.perf_counter()
                output = manager.call("mcp__local__echo", {"text": str(index)})
                times.append((time.perf_counter() - started) * 1000)
                correct += int(output == f"echo:{index}")
            return {
                "discovered_tools": len(manager.tools),
                "discovery_cold_start_ms": round(discovery_ms, 3),
                "call_success_pct": round(100 * correct / trials, 1),
                "warm_connection_call_latency": latency(times),
            }
        finally:
            manager.close()


def subagent_benchmark(root: Path) -> dict:
    tasks = [
        {"role": "explore", "task": "任务一"},
        {"role": "review", "task": "任务二"},
        {"role": "plan", "task": "任务三"},
    ]
    model = InstantModel(delay_seconds=0.08)
    runner = SubagentRunner(Registry(model), lambda: "deepseek", [])
    started = time.perf_counter()
    sequential = [runner.run_one(item["task"], item["role"]) for item in tasks]
    sequential_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    parallel = runner.run_many(tasks)
    parallel_ms = (time.perf_counter() - started) * 1000
    return {
        "tasks": len(tasks),
        "sequential_success_pct": round(100 * sum(item["status"] == "completed" for item in sequential) / len(tasks), 1),
        "parallel_success_pct": round(100 * sum(item["status"] == "completed" for item in parallel) / len(tasks), 1),
        "sequential_ms": round(sequential_ms, 3),
        "parallel_ms": round(parallel_ms, 3),
        "parallel_speedup_x": round(sequential_ms / parallel_ms, 2) if parallel_ms else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("evaluation/latest.json"))
    args = parser.parse_args()
    root = Path.cwd().resolve()
    started = time.perf_counter()
    result = {
        "method": "Local deterministic benchmark; no real model or network API",
        "tool_inventory": tool_inventory(root),
        "memory": memory_benchmark(root),
        "session": session_benchmark(root),
        "file_checkpoint": checkpoint_benchmark(root),
        "hook": hook_benchmark(root),
        "mcp": mcp_benchmark(root),
        "subagent": subagent_benchmark(root),
    }
    result["total_seconds"] = round(time.perf_counter() - started, 3)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
