"""Agent runtime configuration."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根目录的 .env（不覆盖已有环境变量）
load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


MCP_SERVER_PATH = Path(__file__).parent.parent / "mcp" / "server.py"

# --- Model ---
AGENT_DEFAULT_MODEL = _env("AGENT_DEFAULT_MODEL", "anthropic/claude-opus-4.6")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
RELAY_API_KEY = _env("RELAY_API_KEY", "")
RELAY_BASE_URL = _env("RELAY_BASE_URL", "https://aiberm.com/v1")

# --- Persistence ---
AGENT_DB_PATH = _env("AGENT_DB_PATH", "") or str(Path(__file__).parent.parent / "data" / "agent.db")

# --- Phase 1 (react) ---
MAX_ITERATIONS = 10

# --- Phase 2 (graph) ---
MAX_PLAN_VERIFY_LOOPS = int(_env("MAX_PLAN_VERIFY_LOOPS", "5")) # 最大计划验证轮数,复杂的可能会需要4-5轮,简单的一般2轮
MAX_TOTAL_TOOL_CALLS = int(_env("MAX_TOTAL_TOOL_CALLS", "1250")) # 会话级保护上限,包含注入当前 state 的历史 evidence
MAX_QUERY_TOOL_CALLS = int(_env("MAX_QUERY_TOOL_CALLS", "50")) # 单次用户问题内最多实际执行的工具调用数

# --- Phase 3 (Code Reading) ---
GATEWAY_SOURCE_DIR = _env(
    "GATEWAY_SOURCE_DIR",
    str(Path(__file__).parent.parent / "data" / "source-code"),
)
GATEWAY_SOURCE_INDEX = _env(
    "GATEWAY_SOURCE_INDEX",
    str(Path(GATEWAY_SOURCE_DIR) / "architecture-index.md"),
)
GATEWAY_SOURCE_DOC = _env(
    "GATEWAY_SOURCE_DOC",
    str(Path(GATEWAY_SOURCE_DIR) / "debug" / "docs" / "gateway-api-introduction-en.md"),
)

# --- Phase 3 (RAG) ---
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIM = int(_env("EMBEDDING_DIM", "1536"))
RAG_SIMILARITY_THRESHOLD = float(_env("RAG_SIMILARITY_THRESHOLD", "0.42"))
RAG_SKILL_THRESHOLD = float(_env("RAG_SKILL_THRESHOLD", "0.55"))
RAG_HISTORY_THRESHOLD = float(_env("RAG_HISTORY_THRESHOLD", "0.50"))
RAG_HISTORY_DUP_THRESHOLD = float(_env("RAG_HISTORY_DUP_THRESHOLD", "0.92"))
RAG_TOP_K = int(_env("RAG_TOP_K", "3"))
RAG_DB_PATH = _env("RAG_DB_PATH", "") or str(Path(__file__).parent.parent / "data" / "rag.db")
RAG_STORE_BACKEND = _env("RAG_STORE_BACKEND", "sqlite")

# --- Phase 3 (Multi-turn) ---
SESSION_MAX_TURNS = int(_env("SESSION_MAX_TURNS", "20")) # 最大轮数,一轮为一次用户对话+一次回答
# 原文轮数阈值,控制压缩证据evidence,不减少history,压缩前面SESSION_COMPRESS_AFTER轮前的evidence,每轮结束压缩一次前面的
# 同时也是历史压缩原文保留轮数(保留的是history里面最新的SESSION_COMPRESS_AFTER*2条,前面压成一条)
SESSION_COMPRESS_AFTER = int(_env("SESSION_COMPRESS_AFTER", "3")) # 压缩间隔轮数,一次对话+回答算一次,针对evidence
# 压缩history,减少条数,保留最近2*SESSION_COMPRESS_AFTER条,前面的压成一条
SESSION_HISTORY_COMPRESS_AFTER = int(_env("SESSION_HISTORY_COMPRESS_AFTER", "10")) # history 超过多少条后压缩(一次用户对话为两条)

# --- Phase 4 (Optimization) ---
SESSION_MAX_BATCH_SEGMENTS = int(_env("SESSION_MAX_BATCH_SEGMENTS", "20")) # batch 归档最多保留数量
SESSION_MAX_DEEP_SEGMENTS = int(_env("SESSION_MAX_DEEP_SEGMENTS", "30")) # deep 归档最多保留数量


## --- Debug ---
AGENT_DEBUG_INFO_ALL = _env("AGENT_DEBUG_INFO_ALL", "true").lower() in ("1", "true", "yes", "on")
AGENT_DEBUG_INFO_ALL_DIR = _env("AGENT_DEBUG_INFO_ALL_DIR", "") or str(
    Path(__file__).parent / "debug" / "info-all"
)
