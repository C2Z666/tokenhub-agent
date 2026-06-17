"""SQLite-backed RAG store.

Uses native sqlite3 (not SQLAlchemy) to support FTS5 virtual tables.
Embeddings stored as float32 numpy byte arrays in BLOB columns;
similarity computed in Python via numpy cosine.

Separate DB file (rag.db) from agent.db to avoid FTS5/ORM conflicts.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np

from agent.config import RAG_DB_PATH
from agent.rag.store import Chunk, RAGStore

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_chunks (
    id          TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    title       TEXT DEFAULT '',
    content     TEXT NOT NULL,
    metadata    TEXT DEFAULT '{}',
    embedding   BLOB,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rag_source_type ON rag_chunks(source_type);
CREATE INDEX IF NOT EXISTS idx_rag_source_id ON rag_chunks(source_id);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
    title, content, source_type,
    content='rag_chunks', content_rowid='rowid'
);
"""

# Triggers to keep FTS5 in sync with rag_chunks
_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS rag_chunks_ai AFTER INSERT ON rag_chunks BEGIN
    INSERT INTO rag_chunks_fts(rowid, title, content, source_type)
    VALUES (new.rowid, new.title, new.content, new.source_type);
END;

CREATE TRIGGER IF NOT EXISTS rag_chunks_ad AFTER DELETE ON rag_chunks BEGIN
    INSERT INTO rag_chunks_fts(rag_chunks_fts, rowid, title, content, source_type)
    VALUES ('delete', old.rowid, old.title, old.content, old.source_type);
END;

CREATE TRIGGER IF NOT EXISTS rag_chunks_au AFTER UPDATE ON rag_chunks BEGIN
    INSERT INTO rag_chunks_fts(rag_chunks_fts, rowid, title, content, source_type)
    VALUES ('delete', old.rowid, old.title, old.content, old.source_type);
    INSERT INTO rag_chunks_fts(rowid, title, content, source_type)
    VALUES (new.rowid, new.title, new.content, new.source_type);
END;
"""


def _embedding_to_blob(embedding: list[float]) -> bytes:
    """Pack float list to bytes (float32)."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _blob_to_embedding(blob: bytes) -> np.ndarray:
    """Unpack bytes to numpy float32 array."""
    n = len(blob) // 4  # 4 bytes per float32
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two vectors."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


class SQLiteRAGStore(RAGStore):
    """SQLite + FTS5 + numpy cosine RAG store."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path or RAG_DB_PATH
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.executescript(_FTS_SQL)
            conn.executescript(_TRIGGER_SQL)
            conn.commit()
        finally:
            conn.close()
        logger.info("RAG SQLite store initialized at %s", self._db_path)

    def upsert(
        self,
        chunk_id: str,
        content: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        source_type = metadata.get("source_type", "")
        source_id = metadata.get("source_id", "")
        title = metadata.get("title", "")
        meta_json = json.dumps(metadata, ensure_ascii=False)
        emb_blob = _embedding_to_blob(embedding)

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO rag_chunks (id, source_type, source_id, title, content, metadata, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_type = excluded.source_type,
                    source_id   = excluded.source_id,
                    title       = excluded.title,
                    content     = excluded.content,
                    metadata    = excluded.metadata,
                    embedding   = excluded.embedding,
                    updated_at  = datetime('now')
                """,
                (chunk_id, source_type, source_id, title, content, meta_json, emb_blob),
            )
            conn.commit()
        finally:
            conn.close()

    def search_similar(
        self,
        query_embedding: list[float],
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        query_vec = np.array(query_embedding, dtype=np.float32)

        where_clauses: list[str] = []
        params: list[Any] = []
        if filters:
            for key, val in filters.items():
                where_clauses.append(f"{key} = ?")
                params.append(val)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT id, source_type, source_id, title, content, metadata, embedding "
                f"FROM rag_chunks {where_sql}",
                params,
            ).fetchall()
        finally:
            conn.close()

        scored: list[tuple[float, dict]] = []
        for row in rows:
            emb_blob = row["embedding"]
            if not emb_blob:
                continue
            row_vec = _blob_to_embedding(emb_blob)
            sim = _cosine_similarity(query_vec, row_vec)
            scored.append((sim, dict(row)))

        scored.sort(key=lambda x: -x[0])

        results: list[Chunk] = []
        for sim, row_dict in scored[:top_k]:
            meta = json.loads(row_dict["metadata"]) if row_dict["metadata"] else {}
            results.append(Chunk(
                chunk_id=row_dict["id"],
                source_type=row_dict["source_type"],
                source_id=row_dict["source_id"],
                title=row_dict["title"] or "",
                content=row_dict["content"],
                metadata=meta,
                score=sim,
            ))
        return results

    def search_text(
        self,
        query: str,
        top_k: int = 3,
        filters: dict[str, Any] | None = None,
    ) -> list[Chunk]:
        # Build FTS5 match query
        # Escape special FTS5 characters
        safe_query = query.replace('"', '""')

        source_filter = ""
        if filters and "source_type" in filters:
            source_filter = f' AND source_type:"{filters["source_type"]}"'

        fts_query = f'"{safe_query}"{source_filter}'

        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT c.id, c.source_type, c.source_id, c.title, c.content, c.metadata,
                       rank
                FROM rag_chunks_fts f
                JOIN rag_chunks c ON c.rowid = f.rowid
                WHERE rag_chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, top_k),
            ).fetchall()
        finally:
            conn.close()

        results: list[Chunk] = []
        for row in rows:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            results.append(Chunk(
                chunk_id=row["id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                title=row["title"] or "",
                content=row["content"],
                metadata=meta,
                score=abs(float(row["rank"])),  # FTS5 rank is negative; lower = better
            ))
        return results

    def delete_by_source(self, source_type: str, source_id: str | None = None) -> int:
        conn = self._connect()
        try:
            if source_id:
                cursor = conn.execute(
                    "DELETE FROM rag_chunks WHERE source_type = ? AND source_id = ?",
                    (source_type, source_id),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM rag_chunks WHERE source_type = ?",
                    (source_type,),
                )
            deleted = cursor.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    def count(self, filters: dict[str, Any] | None = None) -> int:
        where_clauses: list[str] = []
        params: list[Any] = []
        if filters:
            for key, val in filters.items():
                where_clauses.append(f"{key} = ?")
                params.append(val)
        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT COUNT(*) as cnt FROM rag_chunks {where_sql}", params
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()
