"""
core/store.py — SQLite memory store with WAL mode and auto-migration.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .schema import apply_schema
from .wal import WAL


class MemoryStore:
    """
    SQLite-backed memory store. Single connection, WAL mode, auto-migrates on open.

    Usage:
        store = MemoryStore("~/.lore-memory/default.db")
        mid = store.add("User prefers dark mode")
        results = store.search("theme preferences")
        store.close()
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        if db_path != ":memory:":
            resolved = Path(db_path).expanduser().resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(resolved)
        else:
            self.db_path = ":memory:"

        self._conn: sqlite3.Connection | None = None
        self._wal: WAL | None = None
        self._open()

    # ── Connection management ──────────────────────────────────────────────

    def _open(self) -> None:
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=10,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        apply_schema(self._conn)
        self._wal = WAL(self._conn)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store is closed")
        return self._conn

    @property
    def wal(self) -> WAL:
        if self._wal is None:
            raise RuntimeError("Store is closed")
        return self._wal

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            self._wal = None

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── Core CRUD ──────────────────────────────────────────────────────────

    def add(
        self,
        content: str,
        memory_type: str = "fact",
        source_format: str | None = None,
        drawer_id: int | None = None,
        chunk_index: int | None = None,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        memory_id: str | None = None,
    ) -> str:
        """
        Insert a new memory. Returns the generated ID.

        Args:
            content: The text content to remember.
            memory_type: One of fact, experience, opinion, meta.
            source_format: Original format (plain, conversation, file, etc.).
            drawer_id: Which L1 drawer this belongs to.
            chunk_index: Position within a chunked document.
            parent_id: Parent memory ID for chunk hierarchies.
            metadata: Arbitrary JSON-serializable dict.
            memory_id: Supply your own ID (useful for testing/idempotent imports).

        Returns:
            The memory ID (UUID4 string).
        """
        mid = memory_id or str(uuid.uuid4())
        now = time.time()
        meta_json = json.dumps(metadata) if metadata else None

        payload = {
            "content": content,
            "memory_type": memory_type,
            "source_format": source_format,
            "drawer_id": drawer_id,
            "chunk_index": chunk_index,
            "parent_id": parent_id,
            "metadata": metadata,
        }
        self.wal.record("INSERT", "memories", record_id=mid, data=payload)

        self.conn.execute(
            """
            INSERT INTO memories
                (id, content, memory_type, source_format, created_at,
                 drawer_id, chunk_index, parent_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mid, content, memory_type, source_format, now,
             drawer_id, chunk_index, parent_id, meta_json),
        )
        self.conn.commit()
        return mid

    def get(self, memory_id: str) -> dict[str, Any] | None:
        """Fetch a single memory by ID. Returns None if not found."""
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def update(
        self,
        memory_id: str,
        content: str | None = None,
        memory_type: str | None = None,
        decay_score: float | None = None,
        drawer_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """
        Update mutable fields on a memory. Returns True if a row was updated.
        Only provided (non-None) fields are changed.
        """
        existing = self.get(memory_id)
        if existing is None:
            return False

        new_content = content if content is not None else existing["content"]
        new_type = memory_type if memory_type is not None else existing["memory_type"]
        new_decay = decay_score if decay_score is not None else existing["decay_score"]
        new_drawer = drawer_id if drawer_id is not None else existing["drawer_id"]
        new_meta = metadata if metadata is not None else existing["metadata"]
        meta_json = json.dumps(new_meta) if new_meta is not None else None

        payload = {
            "content": new_content,
            "memory_type": new_type,
            "decay_score": new_decay,
            "drawer_id": new_drawer,
        }
        self.wal.record("UPDATE", "memories", record_id=memory_id, data=payload)

        cursor = self.conn.execute(
            """
            UPDATE memories
            SET content=?, memory_type=?, decay_score=?, drawer_id=?, metadata=?
            WHERE id=?
            """,
            (new_content, new_type, new_decay, new_drawer, meta_json, memory_id),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def delete(self, memory_id: str) -> bool:
        """
        Hard-delete a memory. Returns True if deleted.
        Prefer setting decay_score=0 (soft delete) in production.
        """
        self.wal.record("DELETE", "memories", record_id=memory_id)
        cursor = self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        self.conn.commit()
        return cursor.rowcount > 0

    def touch(self, memory_id: str) -> None:
        """Record an access: update last_accessed and increment access_count."""
        now = time.time()
        self.conn.execute(
            "UPDATE memories SET last_accessed=?, access_count=access_count+1 WHERE id=?",
            (now, memory_id),
        )
        self.conn.commit()

    # ── Search (FTS5 BM25) ─────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        memory_type: str | None = None,
        min_decay: float = 0.01,
    ) -> list[dict[str, Any]]:
        """
        Full-text search using FTS5 BM25 ranking.

        Args:
            query: Search query string.
            top_k: Maximum number of results.
            memory_type: Optional filter by type (fact/experience/opinion/meta).
            min_decay: Minimum decay_score (filter out heavily decayed memories).

        Returns:
            List of memory dicts ordered by BM25 relevance descending.
        """
        if not query.strip():
            return []

        # FTS5 rank() returns negative BM25 score (closer to 0 = more relevant)
        sql = """
            SELECT m.*, rank
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.rowid
            WHERE memories_fts MATCH ?
              AND m.decay_score >= ?
        """
        # Pass the raw query to FTS5 — only escape double-quotes so FTS5 can
        # tokenize natively and use BM25 ranking properly.
        # Fall back to quoted-token approach if FTS5 rejects the query.
        safe_query = query.replace('"', '""')
        params: list[Any] = [safe_query, min_decay]

        if memory_type is not None:
            sql += " AND m.memory_type = ?"
            params.append(memory_type)

        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except Exception:
            # FTS5 rejected the query (special chars, syntax) — fall back to
            # quoted-token OR approach which is more forgiving.
            tokens = query.split()
            fallback_query = (
                " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                if tokens
                else query
            )
            params[0] = fallback_query
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(
        self,
        memory_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List memories, optionally filtered by type, ordered by creation time desc."""
        if memory_type is not None:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE memory_type=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (memory_type, limit, offset),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self, memory_type: str | None = None) -> int:
        """Count memories, optionally filtered by type."""
        if memory_type is not None:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type=?", (memory_type,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    # ── Temporal queries ───────────────────────────────────────────────────

    def search_temporal(
        self,
        query: str,
        since: float | None = None,
        until: float | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        FTS5 search filtered to a time window.

        Args:
            query: Search query.
            since: Unix timestamp lower bound (inclusive).
            until: Unix timestamp upper bound (inclusive).
            top_k: Max results.
        """
        if not query.strip():
            return []

        sql = """
            SELECT m.*, rank
            FROM memories_fts
            JOIN memories m ON memories_fts.rowid = m.rowid
            WHERE memories_fts MATCH ?
        """
        params: list[Any] = [query]

        if since is not None:
            sql += " AND m.created_at >= ?"
            params.append(since)
        if until is not None:
            sql += " AND m.created_at <= ?"
            params.append(until)

        sql += " ORDER BY rank LIMIT ?"
        params.append(top_k)

        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def recent(self, n: int = 10, memory_type: str | None = None) -> list[dict[str, Any]]:
        """Return the N most recently created memories."""
        if memory_type is not None:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE memory_type=? ORDER BY created_at DESC LIMIT ?",
                (memory_type, n),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (n,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ── Stats ──────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Return memory statistics."""
        total = self.count()
        by_type = {}
        for t in ("fact", "experience", "opinion", "meta"):
            by_type[t] = self.count(t)

        decay_row = self.conn.execute(
            "SELECT AVG(decay_score), MIN(decay_score), MAX(decay_score) FROM memories"
        ).fetchone()

        return {
            "total": total,
            "by_type": by_type,
            "wal_entries": self.wal.count(),
            "decay_avg": round(decay_row[0] or 0.0, 4),
            "decay_min": round(decay_row[1] or 0.0, 4),
            "decay_max": round(decay_row[2] or 0.0, 4),
        }

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        if d.get("metadata") and isinstance(d["metadata"], str):
            try:
                d["metadata"] = json.loads(d["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
        # Strip FTS rank key if present (not a real column)
        d.pop("rank", None)
        return d
