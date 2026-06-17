"""Minimal ReAct loop for Phase 1."""
from __future__ import annotations

from typing import Any

from agent.config import AGENT_DEFAULT_MODEL, MAX_ITERATIONS
from agent.llm.claude import ClaudeClient
from agent.tools.mcp_client import call_tool, list_tools, mcp_session

SYSTEM_PROMPT = """你是 TokenHub 网关运维专家 Agent，负责只读故障诊断。

规则：
- 所有结论必须引用真实的 trace_id 或 MCP 工具返回的原始字段，禁止编造
- 时间统一使用 +08:00 时区
- 查询前必须确认时间窗口，若用户未提供则默认最近 15 分钟
- 响应中的 API Key、Token、Secret 不得完整展示
- 每次工具调用后分析结果，判断是否需要继续深查或已可出报告

输出格式（最终报告）：
## 结论
（一句话根因）

## 证据
（引用 trace_id、logstore、字段值）

## 修复建议
（具体操作步骤）
"""


def _tools_for_claude(tools: list[dict]) -> list[dict]:
    """Convert MCP tool list to Anthropic tool_use format."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["input_schema"],
        }
        for t in tools
        if t["name"] != "ping"
    ]


def _tool_result_block(tool_id: str, content: str) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
    }


async def run_investigation(user_query: str, on_event=None) -> str:
    """Run ReAct loop. on_event(type, data) called for streaming events."""

    def emit(event_type: str, data: Any = None):
        if on_event:
            on_event(event_type, data)

    async with mcp_session() as session:
        mcp_tools = await list_tools(session)
        claude_tools = _tools_for_claude(mcp_tools)
        llm = ClaudeClient(model=AGENT_DEFAULT_MODEL)

        messages: list[dict] = [{"role": "user", "content": user_query}]

        for iteration in range(MAX_ITERATIONS):
            emit("llm_call", {"iteration": iteration + 1})
            response = llm.invoke(messages, tools=claude_tools, system=SYSTEM_PROMPT)

            # Append assistant message (raw content blocks for Anthropic API)
            messages.append({"role": "assistant", "content": response.raw_content})

            if not response.tool_calls:
                # No tool calls → final answer
                emit("done", response.text)
                return response.text

            # Execute tool calls
            tool_results = []
            for tc in response.tool_calls:
                emit("tool_call", {"name": tc.name, "input": tc.input})
                result = await call_tool(session, tc.name, tc.input)
                emit("tool_result", {"name": tc.name, "result_len": len(result)})
                tool_results.append(_tool_result_block(tc.id, result))

            messages.append({"role": "user", "content": tool_results})

        emit("max_iterations")
        return "达到最大迭代次数，请缩小查询范围后重试。"
