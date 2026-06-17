"""RAG store abstract interface.

Defines the Chunk dataclass and RAGStore ABC that all backend implementations
must conform to. Currently only SQLite is implemented (sqlite_store.py);
pgvector adapter planned for production migration.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Chunk:
    """Single chunk stored in / retrieved from the RAG store."""
    chunk_id: str
    source_type: str          # 'skill' | 'history' | 'doc' | 'code'
    source_id: str            # e.g. 'S02', thread_id, doc filename
    title: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0        # populated during retrieval


class RAGStore(ABC):
    """Abstract interface for the RAG vector + text store."""

    @abstractmethod
    def upsert(
        self,
        chunk_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """Insert or update a chunk."""

    @abstractmethod
    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Vector similarity search. Returns chunks sorted by descending score."""

    @abstractmethod
    def search_text(
        self,
        query: str,
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        """Full-text search (FTS5). Returns chunks sorted by relevance."""

    @abstractmethod
    def delete_by_source(self, source_type: str, source_id: str | None = None) -> int:
        """Delete chunks by source_type (and optionally source_id). Returns count deleted."""

    @abstractmethod
    def count(self, filters: dict[str, Any] | None = None) -> int:
        """Count chunks matching optional filters."""
