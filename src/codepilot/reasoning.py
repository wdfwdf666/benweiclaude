"""Extract only reasoning content that a model provider actually exposes."""

from __future__ import annotations


def exposed_reasoning(message) -> str:
    """Return provider-visible reasoning, without trying to reconstruct hidden state."""
    try:
        blocks = message.content_blocks
    except (AttributeError, ValueError):
        blocks = []
    parts = [
        str(block.get("reasoning") or block.get("text") or "")
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "reasoning"
    ]
    if parts:
        return "".join(parts)
    extra = getattr(message, "additional_kwargs", {}) or {}
    value = extra.get("reasoning_content") or extra.get("reasoning")
    return value if isinstance(value, str) else ""
