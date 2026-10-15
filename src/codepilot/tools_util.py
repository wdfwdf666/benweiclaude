"""Small helpers shared by tools and subagents."""

MAX_OUTPUT = 16_000


def clipped_text(value: str, limit: int = MAX_OUTPUT) -> str:
    return value if len(value) <= limit else value[:limit] + "\n... [truncated]"
