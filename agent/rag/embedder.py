"""Embedding generator via relay (OpenAI-compatible API).

Uses the same RELAY_API_KEY + RELAY_BASE_URL as ClaudeClient,
but calls the embeddings endpoint via the openai SDK.
"""
from __future__ import annotations

import hashlib
import logging
from functools import lru_cache

from openai import OpenAI

from agent.config import EMBEDDING_DIM, EMBEDDING_MODEL, RELAY_API_KEY, RELAY_BASE_URL

logger = logging.getLogger(__name__)


class Embedder:
    """Generate embeddings via relay's OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str = EMBEDDING_MODEL,
        dim: int = EMBEDDING_DIM,
    ):
        self.model = model
        self.dim = dim
        self._client = OpenAI(
            api_key=RELAY_API_KEY,
            base_url=RELAY_BASE_URL,
        )
        self._cache: dict[str, list[float]] = {}

    @staticmethod
    def _cache_key(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text. Results cached by md5."""
        key = self._cache_key(text)
        if key in self._cache:
            return self._cache[key]

        resp = self._client.embeddings.create(
            model=self.model,
            input=text,
        )
        embedding = resp.data[0].embedding
        self._cache[key] = embedding
        return embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for a batch of texts.

        Checks cache first; only sends uncached texts to the API.
        """
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []
        uncached_texts: list[str] = []

        for i, text in enumerate(texts):
            key = self._cache_key(text)
            if key in self._cache:
                results[i] = self._cache[key]
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            resp = self._client.embeddings.create(
                model=self.model,
                input=uncached_texts,
            )
            for j, item in enumerate(resp.data):
                idx = uncached_indices[j]
                embedding = item.embedding
                results[idx] = embedding
                self._cache[self._cache_key(uncached_texts[j])] = embedding

        return results  # type: ignore[return-value]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Return a singleton Embedder instance."""
    return Embedder()
