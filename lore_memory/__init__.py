"""
lore-memory — Memory that learns from forgetting.

A 1-dependency Python library that gives any LLM persistent memory
with auto-evolving knowledge graphs, temporal reasoning, and
fleet-wide failure learning.

Quick start:
    from lore_memory import LoreMemory

    mem = LoreMemory()
    mem.remember("User prefers dark mode")
    results = mem.recall("what theme does the user prefer?")
"""

from __future__ import annotations

from .config import LoreConfig
from .core.store import MemoryStore
from .layers.identity import IdentityLayer

__version__ = "0.1.0"
__all__ = ["LoreMemory", "LoreConfig", "MemoryStore", "IdentityLayer"]


class LoreMemory:
    """
    High-level API for lore-memory.

    Usage:
        mem = LoreMemory()                        # uses defaults
        mem = LoreMemory("~/.lore-memory/my.db")  # custom db path
        mem = LoreMemory(config=LoreConfig("my.yml"))

        mem.remember("User prefers dark mode")
        results = mem.recall("theme preference")
        mem.identity.set({"name": "Miles", "role": "CTO"})
    """

    def __init__(
        self,
        db_path: str | None = None,
        config: LoreConfig | None = None,
    ) -> None:
        self.config = config or LoreConfig()
        resolved_path = db_path or self.config.db_path
        self.store = MemoryStore(resolved_path)
        self.identity = IdentityLayer(self.store)

    # ── Remember ──────────────────────────────────────────────────────────

    def remember(
        self,
        content: str,
        memory_type: str = "fact",
        source_format: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """
        Store a memory. Returns the memory ID.

        Args:
            content: Text to remember.
            memory_type: fact | experience | opinion | meta
            source_format: Where this came from (plain, conversation, file, etc.)
            metadata: Optional dict of extra data.
        """
        return self.store.add(
            content=content,
            memory_type=memory_type,
            source_format=source_format,
            metadata=metadata,
        )

    # ── Recall ────────────────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        top_k: int | None = None,
        memory_type: str | None = None,
    ) -> list[dict]:
        """
        Retrieve memories by query using FTS5 BM25.

        Args:
            query: Natural language query.
            top_k: Max results (defaults to config layers.search.top_k).
            memory_type: Optional filter.

        Returns:
            List of memory dicts ordered by relevance.
        """
        k = top_k if top_k is not None else self.config.get("layers.search.top_k", 5)
        return self.store.search(query, top_k=k, memory_type=memory_type)

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return memory statistics."""
        s = self.store.stats()
        s["identity_configured"] = self.identity.exists()
        s["identity_tokens"] = self.identity.token_count()
        return s

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying database connection."""
        self.store.close()

    def __enter__(self) -> "LoreMemory":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"LoreMemory(db={self.store.db_path!r})"
