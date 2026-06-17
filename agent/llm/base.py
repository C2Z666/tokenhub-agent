"""LLM client abstraction for Phase 2.

Defines the ChatModel protocol that all LLM clients must implement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ToolCall:
    """Normalized tool call representation."""
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    raw_content: Any = None  # Provider-specific raw content for message history


@runtime_checkable
class ChatModel(Protocol):
    """Protocol that all LLM clients must implement."""

    def invoke(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Send messages to the LLM and return a normalized response.

        Args:
            messages: Conversation history in [{role, content}] format.
            tools: Tool definitions in Anthropic tool_use format.
            system: System prompt.

        Returns:
            LLMResponse with text, tool_calls, and raw_content.
        """
        ...
