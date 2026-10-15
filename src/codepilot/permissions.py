"""Small, explicit permission policy for tool calls."""

from __future__ import annotations

from dataclasses import dataclass

PERMISSION_MODES = ("default", "acceptEdits", "plan")

# These can change workspace files. Shell commands are kept separate because an
# approved command can have effects outside the built-in file tool boundary.
FILE_EDITS = frozenset({
    "write_file", "edit_file", "append_file", "make_directory",
    "delete_file", "move_file", "copy_file",
})
TRACKED_FILE_EDITS = FILE_EDITS - {"make_directory"}
OTHER_APPROVALS = frozenset({
    "run_command", "propose_plan", "apply_subagent_changes",
    "memory_propose", "memory_update", "memory_forget",
})


@dataclass(frozen=True)
class PermissionDecision:
    kind: str  # allow, ask, or deny
    reason: str = ""


def decide(mode: str, tool_name: str) -> PermissionDecision:
    if mode not in PERMISSION_MODES:
        raise ValueError(f"Unknown permission mode: {mode}")
    if mode == "plan" and (tool_name.startswith("mcp__") or tool_name in FILE_EDITS | {
        "run_command", "delegate_coding_tasks", "apply_subagent_changes",
        "memory_propose", "memory_update", "memory_forget",
    }):
        return PermissionDecision("deny", "计划模式只允许调查和提交计划")
    if tool_name.startswith("mcp__"):
        return PermissionDecision("ask")
    if mode == "acceptEdits" and tool_name in FILE_EDITS - {"delete_file"}:
        return PermissionDecision("allow")
    if tool_name in FILE_EDITS | OTHER_APPROVALS:
        return PermissionDecision("ask")
    return PermissionDecision("allow")
