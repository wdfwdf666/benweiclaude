"""Example PreToolUse hook: deny deleting files under src/."""

import json
import sys

event = json.load(sys.stdin)
path = str(event.get("tool_input", {}).get("path", "")).replace("\\", "/")
if event.get("tool_name") == "delete_file" and path.startswith("docs/"):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "src/ 下的文件不能通过 delete_file 删除",
        }
    }, ensure_ascii=False))
