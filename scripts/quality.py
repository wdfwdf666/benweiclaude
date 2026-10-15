"""Run the same local quality gates used by CI."""

from __future__ import annotations

import subprocess
import sys

COMMANDS = [
    [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
    [sys.executable, "-m", "mypy"],
    [
        sys.executable, "-m", "pytest", "--cov=codepilot", "--cov-branch",
        "--cov-report=term-missing",
    ],
]


def main() -> int:
    for command in COMMANDS:
        print("+", " ".join(command), flush=True)
        completed = subprocess.run(command, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
