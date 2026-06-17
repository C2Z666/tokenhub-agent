"""TokenHub local MCP server."""

from __future__ import annotations

import logging
import sys

from mcp.server.fastmcp import FastMCP

from db.tools import db_get_user_api_key_prefixes
from sls.tools import (
    sls_aggregate_gateway_usage,
    sls_get_request_response,
    sls_get_trace,
    sls_list_logstores,
    sls_query_gateway_usage_overview,
    sls_search_errors,
)


logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

mcp = FastMCP(
    "tokenhub-local",
    instructions=(
        "本服务用于 TokenHub 网关运维排查，提供数据库业务查询和 SLS 日志查询。"
        "数据库工具不暴露自由 SQL，仅提供白名单功能。"
        "SLS 查询必须限制时间窗口和返回条数。"
        "返回结果必须脱敏 API Key、Authorization、Token、Secret 等敏感信息。"
        "遇到可能产生高成本或大范围扫描的查询，先收窄查询条件。"
    ),
)


@mcp.tool()
def ping() -> dict:
    """Check whether the TokenHub local MCP server is reachable."""

    return {"ok": True, "data": {"service": "tokenhub-local", "status": "ready"}, "warnings": []}

# 注册MCP工具
mcp.tool()(sls_list_logstores)
mcp.tool()(sls_search_errors)
mcp.tool()(sls_get_trace)
mcp.tool()(sls_get_request_response)
mcp.tool()(sls_query_gateway_usage_overview)
mcp.tool()(sls_aggregate_gateway_usage)
mcp.tool()(db_get_user_api_key_prefixes)


if __name__ == "__main__":
    mcp.run()
