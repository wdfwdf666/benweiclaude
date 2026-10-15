"""Lazy model construction; keys are loaded from .env by the CLI."""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langchain_qwq import ChatQwen

MODEL_ALIASES = ("deepseek", "qwen", "openai")


class ModelRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, BaseChatModel] = {}

    def get(self, alias: str) -> BaseChatModel:
        if alias not in MODEL_ALIASES:
            raise ValueError(f"Unknown model {alias!r}; choose {', '.join(MODEL_ALIASES)}")
        if alias in self._cache:
            return self._cache[alias]

        key_name = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "QWEN_API_KEY", "openai": "OPENAI_API_KEY"}[alias]
        key = os.getenv(key_name)
        if not key:
            raise ValueError(f"Missing {key_name}. Add it to .env before using {alias}.")

        if alias == "deepseek":
            model = ChatDeepSeek(model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), api_key=key)
        elif alias == "qwen":
            model = ChatQwen(
                model=os.getenv("QWEN_MODEL", "qwen-plus"),
                api_key=key,
                base_url=os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                enable_thinking=os.getenv("QWEN_ENABLE_THINKING", "true").lower() not in {"0", "false", "no"},
            )
        else:
            model = ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), api_key=key)
        self._cache[alias] = model
        return model
