"""Anthropic Claude LLM client.

中转站优先：若配置了 RELAY_API_KEY，使用中转站接口（base_url 指向中转站）。
否则直连 Anthropic 官方 API。

中转站模型名（原样透传给中转站）：
- anthropic/claude-opus-4.6
- anthropic/claude-sonnet-4.6
- anthropic/claude-haiku-4.5
"""
from __future__ import annotations

from typing import Any

import anthropic

from agent.config import ANTHROPIC_API_KEY, RELAY_API_KEY, RELAY_BASE_URL
from agent.llm.base import LLMResponse, ToolCall


class ClaudeClient:
    def __init__(self, model: str = "claude-opus-4-6", temperature: float | None = None, node: str = "unknown"):
        self.model = model
        self.temperature = temperature
        self.node = node
        if RELAY_API_KEY:
            # Anthropic SDK 会自动拼 /v1/messages，所以 base_url 只传到域名，去掉 /v1 后缀
            base = RELAY_BASE_URL.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            self._client = anthropic.Anthropic(api_key=RELAY_API_KEY, base_url=base)
        else:
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    def invoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        resp = self._client.messages.create(**kwargs)

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        for block in resp.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=dict(block.input) if block.input else {},
                ))
            elif block.type == "text":
                text_parts.append(block.text)

        response = LLMResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "",
            raw_content=resp.content,
        )
        try:
            from agent.debug import info_all
            info_all.record_llm(self.node, {
                "model": self.model,
                "temperature": self.temperature,
                "messages": messages,
                "tools": tools or [],
                "system": system,
            }, {
                "text": response.text,
                "tool_calls": [tc.__dict__ for tc in response.tool_calls],
                "stop_reason": response.stop_reason,
            })
        except Exception:
            pass
        return response
