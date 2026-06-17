"""Node-to-model registry.

读取 .env 中的 LLM_MODEL_<NODE> 变量，按节点名返回对应的 ChatModel 实例。

约定模型字符串格式（原样透传给中转站，不做 split 解析）：
- anthropic/claude-opus-4.6
- anthropic/claude-sonnet-4.6
- anthropic/claude-haiku-4.5
- openai/gpt-5.5  （Phase 3 才支持）

默认配置（与 P2 决策一致）：
- planner / verifier → opus-4.6
- reporter          → sonnet-4.6
- intake / skill_router → haiku-4.5
"""
from __future__ import annotations

import os
from functools import lru_cache

from agent.llm.base import ChatModel
from agent.llm.claude import ClaudeClient

DEFAULT_MODELS: dict[str, str] = {
    "planner": "anthropic/claude-opus-4.6",
    "verifier": "anthropic/claude-opus-4.6",
    "reporter": "openai/gpt-5.5",
    "intake": "openai/gpt-5.4",
    "skill_router": "openai/gpt-5.4",
    "code_explorer": "anthropic/claude-sonnet-4.6",
}

# Per-node temperature overrides (None = use model default)
NODE_TEMPERATURES: dict[str, float] = {
    "reporter": 0.0,  # Reduce randomness to prevent repetitive/looping output
}



ENV_PREFIX = "LLM_MODEL_"


def model_for(node: str) -> str:
    """Resolve model string for a node from env or defaults."""
    env_key = f"{ENV_PREFIX}{node.upper()}"
    return os.environ.get(env_key) or DEFAULT_MODELS.get(node, DEFAULT_MODELS["planner"])


@lru_cache(maxsize=None)
def get_client(node: str) -> ChatModel:
    """Get a cached LLM client for a node.

    Currently all models route through ClaudeClient (Anthropic SDK + optional relay).
    Future: dispatch by provider prefix to OpenAI/Anthropic/etc.
    """
    model_str = model_for(node)
    temperature = NODE_TEMPERATURES.get(node)
    # 中转站接受原样模型字符串（含 provider 前缀）
    return ClaudeClient(model=model_str, temperature=temperature, node=node)
