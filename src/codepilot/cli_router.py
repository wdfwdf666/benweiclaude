"""Small, testable slash-command parser and registry."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    value: str


CommandHandler = Callable[[Any, str, str], bool]


class SlashCommandRouter:
    """Route CLI commands without coupling parsing to interactive rendering."""

    def __init__(self, commands: Iterable[str], handler: CommandHandler) -> None:
        self._handlers = {name: handler for name in commands}

    @staticmethod
    def parse(line: str) -> ParsedCommand:
        name, _, value = line.strip().partition(" ")
        return ParsedCommand(name=name.lower(), value=value.strip())

    def dispatch(self, app: Any, line: str) -> bool | None:
        parsed = self.parse(line)
        handler = self._handlers.get(parsed.name)
        if handler is None:
            return None
        return handler(app, parsed.name, parsed.value)
