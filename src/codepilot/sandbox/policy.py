"""Declarative limits for child processes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENVIRONMENT = (
    "COMSPEC", "LANG", "LC_ALL", "NUMBER_OF_PROCESSORS", "OS", "PATH",
    "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "WINDIR",
)


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    profile: str = "workspace-write"
    filesystem_mode: str = "workspace-write"
    network_mode: str = "none"
    allowed_hosts: tuple[str, ...] = ()
    strict_isolation: bool = field(default_factory=lambda: os.getenv(
        "CODEPILOT_SANDBOX_STRICT", "",
    ).strip().lower() in {"1", "true", "yes", "on"})
    environment_allowlist: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    max_memory_mb: int = 1024
    max_processes: int = 16
    max_output_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        root = self.workspace.resolve()
        if not root.is_dir():
            raise ValueError(f"Sandbox workspace does not exist: {root}")
        if self.profile not in {"inspect", "workspace-write", "network-read", "trusted-process"}:
            raise ValueError(f"Unknown sandbox profile: {self.profile}")
        if self.network_mode not in {"none", "allowlist", "unrestricted"}:
            raise ValueError(f"Unknown sandbox network mode: {self.network_mode}")
        if self.filesystem_mode not in {"workspace-read", "workspace-write", "unrestricted"}:
            raise ValueError(f"Unknown sandbox filesystem mode: {self.filesystem_mode}")
        if self.network_mode == "allowlist" and not self.allowed_hosts:
            raise ValueError("Sandbox allowlist mode requires allowed_hosts")
        if not 0 < self.timeout_seconds <= 600:
            raise ValueError("Sandbox timeout must be between 0 and 600 seconds")
        if not 16 <= self.max_memory_mb <= 65536:
            raise ValueError("Sandbox memory limit must be between 16 and 65536 MB")
        if not 1 <= self.max_processes <= 1024:
            raise ValueError("Sandbox process limit must be between 1 and 1024")
        if not 1024 <= self.max_output_bytes <= 100_000_000:
            raise ValueError("Sandbox output limit must be between 1024 and 100000000 bytes")
        object.__setattr__(self, "workspace", root)

    @property
    def allowed_environment(self) -> frozenset[str]:
        return frozenset((*DEFAULT_ENVIRONMENT, *self.environment_allowlist))
