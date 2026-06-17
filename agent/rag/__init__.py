"""RAG module — vector + text search infrastructure.

Provides two main singletons:
- get_store()    → RAGStore (SQLite backend)
- get_embedder() → Embedder (relay embedding API)
"""
from __future__ import annotations

from functools import lru_cache

from agent.rag.embedder import Embedder, get_embedder
from agent.rag.store import Chunk, RAGStore

__all__ = [
    "Chunk",
    "RAGStore",
    "Embedder",
    "get_store",
    "get_embedder",
]


@lru_cache(maxsize=1)
def get_store() -> RAGStore:
    """Return a singleton RAGStore instance (SQLite backend)."""
    from agent.config import RAG_STORE_BACKEND

    if RAG_STORE_BACKEND == "sqlite":
        from agent.rag.sqlite_store import SQLiteRAGStore
        return SQLiteRAGStore()
    else:
        raise ValueError(f"Unknown RAG_STORE_BACKEND: {RAG_STORE_BACKEND}")
