"""MCP-facing database tools."""

from __future__ import annotations

import logging
import time
from typing import Any

from config import load_config
from db.client import DatabaseClient
from security import mask_api_key, safe_error_detail


LOGGER = logging.getLogger("tokenhub_mcp.audit")


def _error_response(tool: str, start: float, code: str, message: str, exc: Exception) -> dict[str, Any]:
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    LOGGER.warning("%s failed code=%s elapsed_ms=%s detail=%s", tool, code, elapsed_ms, safe_error_detail(exc))
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "safe_detail": safe_error_detail(exc),
        },
        "meta": {"tool": tool, "elapsed_ms": elapsed_ms},
    }


def db_get_user_api_key_prefixes(username: str, reason: str = "") -> dict[str, Any]:
    """Return masked API key prefixes for a username."""

    start = time.perf_counter()
    tool = "db_get_user_api_key_prefixes"
    warnings: list[str] = []

    if not username or not username.strip():
        return {
            "ok": False,
            "error": {"code": "INVALID_ARGUMENT", "message": "username 不能为空", "safe_detail": "empty username"},
            "meta": {"tool": tool, "elapsed_ms": 0},
        }

    try:
        client = DatabaseClient(load_config().db)
        user_rows = client.fetch_all(
            "select id from users where users.`name` = %s limit 101",
            (username.strip(),),
        )
        matched_user_count = len(user_rows)
        if matched_user_count > 1:
            warnings.append("存在多个同名用户，返回结果可能包含多个用户的 API Key 前缀。")

        rows = client.fetch_all(
            """
            select key_prefix
            from users
            left join api_keys on users.id = api_keys.user_id
            where users.`name` = %s
            limit 100
            """,
            (username.strip(),),
        )
        key_prefixes = [mask_api_key(str(row["key_prefix"])) for row in rows if row.get("key_prefix")]
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        LOGGER.info(
            "%s ok username=%s matched_user_count=%s count=%s elapsed_ms=%s reason=%s",
            tool,
            username.strip(),
            matched_user_count,
            len(key_prefixes),
            elapsed_ms,
            reason[:120],
        )
        return {
            "ok": True,
            "data": {
                "username": username.strip(),
                "key_prefixes": key_prefixes,
                "count": len(key_prefixes),
                "matched_user_count": matched_user_count,
            },
            "meta": {"tool": tool, "elapsed_ms": elapsed_ms, "limit": 100},
            "warnings": warnings,
        }
    except Exception as exc:
        return _error_response(tool, start, "DB_QUERY_FAILED", "数据库查询失败，请检查配置或稍后重试", exc)
