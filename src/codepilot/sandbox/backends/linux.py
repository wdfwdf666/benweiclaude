"""Linux bubblewrap filesystem and network namespace backend."""

from __future__ import annotations

import shutil

from ..policy import SandboxPolicy
from .base import SandboxCapabilities


class BubblewrapBackend:
    def __init__(self) -> None:
        self.executable = shutil.which("bwrap")

    @property
    def available(self) -> bool:
        return self.executable is not None

    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            backend="bubblewrap" if self.available else "posix-rlimit",
            process_tree=True,
            memory_limit=True,
            process_limit=True,
            filesystem_isolation=self.available,
            network_isolation=self.available,
        )

    def wrap(self, argv: list[str], policy: SandboxPolicy) -> list[str]:
        if not self.available:
            if policy.strict_isolation:
                raise RuntimeError("Strict isolation requires bubblewrap on Linux")
            return argv
        if policy.network_mode == "allowlist":
            raise ValueError("Linux allowlist mode requires a configured sandbox proxy")
        command = [
            str(self.executable),
            "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try", "--ro-bind", "/", "/",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        if policy.network_mode == "none":
            command.append("--unshare-net")
        bind_flag = "--ro-bind" if policy.filesystem_mode == "workspace-read" else "--bind"
        if policy.filesystem_mode != "unrestricted":
            root = str(policy.workspace)
            command.extend([bind_flag, root, root])
        command.extend(["--chdir", str(policy.workspace), "--", *argv])
        return command
