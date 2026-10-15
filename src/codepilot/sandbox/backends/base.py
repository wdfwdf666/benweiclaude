"""Shared backend capability model."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class SandboxCapabilities:
    backend: str
    process_tree: bool
    memory_limit: bool
    process_limit: bool
    filesystem_isolation: bool
    network_isolation: bool

    def as_dict(self) -> dict[str, str | bool]:
        return asdict(self)
