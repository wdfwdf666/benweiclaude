"""Workspace trust records for project-controlled executable configuration."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

EXECUTABLE_CONFIGS = (Path(".mcp.json"), Path(".codepilot/hooks.json"))


def executable_configs(workspace: Path) -> list[Path]:
    """Return project files that can cause local processes to be started."""
    root = workspace.resolve()
    return [root / relative for relative in EXECUTABLE_CONFIGS if (root / relative).is_file()]


def workspace_fingerprint(workspace: Path) -> str | None:
    """Hash executable configuration; any byte-level change invalidates trust."""
    root = workspace.resolve()
    paths = executable_configs(root)
    if not paths:
        return None
    digest = hashlib.sha256()
    digest.update(os.path.normcase(str(root)).encode("utf-8"))
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def describe_executable_configs(workspace: Path) -> list[dict[str, Any]]:
    """Build a bounded, non-secret preview of commands controlled by the project."""
    root = workspace.resolve()
    descriptions: list[dict[str, Any]] = []
    mcp_path = root / ".mcp.json"
    if mcp_path.is_file():
        item: dict[str, Any] = {"path": ".mcp.json", "commands": []}
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {}) if isinstance(data, dict) else {}
            if isinstance(servers, dict):
                for name, config in sorted(servers.items()):
                    if isinstance(config, dict):
                        command = config.get("command", "<invalid>")
                        args = config.get("args", [])
                        if not isinstance(args, list):
                            args = ["<invalid args>"]
                        item["commands"].append({
                            "name": str(name)[:100],
                            "command": " ".join([str(command), *(str(arg) for arg in args)])[:1000],
                        })
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        descriptions.append(item)

    hooks_path = root / ".codepilot" / "hooks.json"
    if hooks_path.is_file():
        item = {"path": ".codepilot/hooks.json", "commands": []}
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
            hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
            if isinstance(hooks, dict):
                for event, groups in hooks.items():
                    if not isinstance(groups, list):
                        continue
                    for group in groups:
                        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                            continue
                        for handler in group["hooks"]:
                            if isinstance(handler, dict):
                                command = handler.get("command", "<invalid>")
                                args = handler.get("args", [])
                                if not isinstance(args, list):
                                    args = ["<invalid args>"]
                                item["commands"].append({
                                    "name": str(event)[:100],
                                    "command": " ".join([str(command), *(str(arg) for arg in args)])[:1000],
                                })
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        descriptions.append(item)
    return descriptions


class WorkspaceTrustStore:
    """Persist fingerprints outside projects so a repository cannot trust itself."""

    def __init__(self, path: Path | None = None) -> None:
        # Deliberately do not accept a project-loaded environment override here:
        # an untrusted repository must not be able to choose or edit its trust store.
        self.path = (path or Path.home() / ".codepilot" / "trusted_workspaces.json").resolve()

    @staticmethod
    def _key(workspace: Path) -> str:
        return os.path.normcase(str(workspace.resolve()))

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "workspaces": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"version": 1, "workspaces": {}}
        if not isinstance(data, dict) or not isinstance(data.get("workspaces"), dict):
            return {"version": 1, "workspaces": {}}
        return data

    def is_trusted(self, workspace: Path) -> bool:
        try:
            fingerprint = workspace_fingerprint(workspace)
        except OSError:
            return False
        if fingerprint is None:
            return True
        record = self._load()["workspaces"].get(self._key(workspace))
        return isinstance(record, dict) and record.get("fingerprint") == fingerprint

    def trust(self, workspace: Path) -> None:
        fingerprint = workspace_fingerprint(workspace)
        if fingerprint is None:
            return
        data = self._load()
        data["workspaces"][self._key(workspace)] = {"fingerprint": fingerprint}
        self._save(data)

    def revoke(self, workspace: Path) -> None:
        data = self._load()
        data["workspaces"].pop(self._key(workspace), None)
        self._save(data)

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
