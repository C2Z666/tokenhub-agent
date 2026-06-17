"""Stdio MCP client wrapping the tokenhub MCP server."""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.config import MCP_SERVER_PATH


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(MCP_SERVER_PATH)],
        env=None,
    )


@asynccontextmanager
async def mcp_session():
    """Async context manager that yields an active MCP ClientSession."""
    async with stdio_client(_server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(session: ClientSession) -> list[dict]:
    result = await session.list_tools()
    tools = []
    for t in result.tools:
        tools.append({
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        })
    return tools


async def call_tool(session: ClientSession, name: str, args: dict[str, Any]) -> str:
    result = await session.call_tool(name, args)
    parts = []
    for c in result.content:
        if hasattr(c, "text"):
            parts.append(c.text)
    return "\n".join(parts) if parts else "(empty)"
