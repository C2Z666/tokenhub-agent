"""OpenAI-compatible LLM client (stub for Phase 2).

Phase 2 仅占位，Phase 3 接入 OpenAI 系列模型时实现。
需要时实现 OpenAI tool_call ↔ Anthropic tool_use 的 schema 归一化。
"""
from __future__ import annotations

from agent.llm.base import LLMResponse


class OpenAIClient:
    def __init__(self, model: str):
        self.model = model
        raise NotImplementedError(
            "OpenAI client not implemented in Phase 2. "
            "Use ClaudeClient via relay (RELAY_API_KEY) instead."
        )

    def invoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        raise NotImplementedError
