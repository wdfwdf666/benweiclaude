"""Structured subprocess result."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False
    output_truncated: bool = False

    def render(self) -> str:
        code = "timeout" if self.timed_out else str(self.returncode)
        suffix = "\n... [sandbox output truncated]" if self.output_truncated else ""
        return f"exit code {code}\n{self.stdout}{self.stderr}{suffix}"
