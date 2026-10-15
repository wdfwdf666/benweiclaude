"""Claude Code-style command hooks for CodePilot lifecycle events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .sandbox import SandboxPolicy, SandboxRunner

EVENTS = frozenset({
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
    "PermissionRequest", "PostToolUse", "PostToolUseFailure", "Stop",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch",
})
BLOCKABLE = frozenset({
    "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "Stop", "PreCompact", "PreModelSwitch", "SubagentStart",
})
MAX_OUTPUT = 64_000


@dataclass(frozen=True)
class HookResult:
    blocked: bool = False
    reason: str = ""
    context: str = ""


class HookManager:
    """Load project hooks and execute matching commands with JSON on stdin."""

    def __init__(self, workspace: Path, event_sink=None, enabled: bool = True,
                 trust_validator=None) -> None:
        self.workspace = workspace.resolve()
        self.path = self.workspace / ".codepilot" / "hooks.json"
        self.event_sink = event_sink
        self.enabled = enabled
        self.trust_validator = trust_validator
        self.sandbox = SandboxRunner()
        self.hooks: dict[str, list[dict]] = {}
        if enabled:
            self.reload()

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.reload()
        self.enabled = enabled

    def _emit(self, event: str, **data) -> None:
        if self.event_sink:
            self.event_sink(event, **data)

    def reload(self) -> None:
        if not self.path.exists():
            self.hooks = {}
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("hooks"), dict):
            raise ValueError(f"Invalid hooks config: {self.path}")
        hooks = raw["hooks"]
        for event, groups in hooks.items():
            if event not in EVENTS or not isinstance(groups, list):
                raise ValueError(f"Invalid hook event: {event}")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError(f"Invalid hook group for {event}")
                matcher = group.get("matcher", "*")
                if not isinstance(matcher, str):
                    raise ValueError(f"Hook matcher must be a string: {event}")
                for handler in group["hooks"]:
                    if (not isinstance(handler, dict) or handler.get("type") != "command"
                            or not isinstance(handler.get("command"), str)
                            or not handler["command"].strip()
                            or not isinstance(handler.get("args", []), list)
                            or not all(isinstance(arg, str) for arg in handler.get("args", []))):
                        raise ValueError(f"Invalid command hook for {event}")
                    timeout = handler.get("timeout", 10)
                    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                        raise ValueError(f"Hook timeout must be 0-120 seconds: {event}")
                    allowlist = handler.get("envAllowlist", [])
                    if (not isinstance(allowlist, list)
                            or not all(isinstance(item, str) and item for item in allowlist)):
                        raise ValueError(f"Hook envAllowlist must contain variable names: {event}")
        self.hooks = hooks

    @staticmethod
    def _matches(pattern: str, value: str) -> bool:
        if pattern in {"", "*"}:
            return True
        if re.fullmatch(r"[\w\- ,|]+", pattern):
            return value in {part.strip() for part in re.split(r"[,|]", pattern)}
        try:
            return re.search(pattern, value) is not None
        except re.error as exc:
            raise ValueError(f"Invalid hook matcher {pattern!r}: {exc}") from exc

    def configured(self) -> list[dict[str, str]]:
        return [
            {"event": event, "matcher": group.get("matcher", "*"),
             "command": handler["command"]}
            for event, groups in self.hooks.items()
            for group in groups for handler in group["hooks"]
        ]

    def run(self, event: str, *, match: str = "", **payload) -> HookResult:
        if event not in EVENTS:
            raise ValueError(f"Unknown hook event: {event}")
        if not self.enabled or self.trust_validator is not None and not self.trust_validator():
            return HookResult()
        blocked = False
        reasons: list[str] = []
        contexts: list[str] = []
        input_data = {
            "hook_event_name": event,
            "session_id": payload.get("session_id"),
            "cwd": str(self.workspace),
            **payload,
        }
        for group in self.hooks.get(event, []):
            if not self._matches(group.get("matcher", "*"), match):
                continue
            for handler in group["hooks"]:
                self._emit("hook_start", hook_event=event, command=handler["command"])
                try:
                    allowlist = handler.get("envAllowlist", [])
                    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
                        raise ValueError("Hook envAllowlist must contain strings")
                    completed = self.sandbox.run(
                        [handler["command"], *handler.get("args", [])],
                        SandboxPolicy(
                            self.workspace, profile="trusted-process",
                            environment_allowlist=tuple(allowlist),
                            timeout_seconds=handler.get("timeout", 10),
                            max_output_bytes=MAX_OUTPUT,
                        ),
                        stdin=json.dumps(input_data, ensure_ascii=False),
                    )
                    if completed.timed_out:
                        raise TimeoutError(f"Hook exceeded {handler.get('timeout', 10)} seconds")
                    stdout = completed.stdout[:MAX_OUTPUT]
                    stderr = completed.stderr[:MAX_OUTPUT]
                    output = json.loads(stdout) if stdout.strip() and completed.returncode == 0 else {}
                    if not isinstance(output, dict):
                        raise ValueError("Hook stdout must be a JSON object")
                    specific = output.get("hookSpecificOutput", {})
                    if not isinstance(specific, dict):
                        specific = {}
                    denied = (completed.returncode == 2
                              or output.get("decision") == "block"
                              or specific.get("permissionDecision") == "deny")
                    if denied and event in BLOCKABLE:
                        blocked = True
                        reasons.append(str(output.get("reason") or specific.get("permissionDecisionReason")
                                           or stderr.strip() or "Hook blocked the action")[:2000])
                    context = output.get("additionalContext") or specific.get("additionalContext")
                    if isinstance(context, str) and context.strip():
                        contexts.append(context[:4000])
                    if completed.returncode not in (0, 2):
                        self._emit("hook_error", hook_event=event,
                                   error=f"exit {completed.returncode}: {stderr.strip()[:500]}")
                    self._emit("hook_result", hook_event=event,
                               command=handler["command"], blocked=denied and event in BLOCKABLE)
                except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                    self._emit("hook_error", hook_event=event,
                               error=f"{type(exc).__name__}: {exc}")
        return HookResult(blocked, "; ".join(reasons), "\n".join(contexts))
