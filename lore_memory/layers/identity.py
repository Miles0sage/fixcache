"""
layers/identity.py — L0 Identity layer.

Stores a small (~100 token) persona/context blob in SQLite.
User-editable, version-tracked via WAL, loaded on every wake-up.
"""

from __future__ import annotations

import time
from typing import Any

import yaml

from ..core.store import MemoryStore

# Max characters for identity (~100 tokens * 4 chars/token)
_MAX_CHARS = 400
_IDENTITY_KEY = "default"


class IdentityLayer:
    """
    L0 Identity — get/set YAML identity stored in the identity table.

    The identity is a small dict (name, role, traits, etc.) that
    describes the agent/user context. Budget: 100 tokens max.

    Usage:
        layer = IdentityLayer(store)
        layer.set({"name": "Miles", "role": "CTO", "style": "ship fast"})
        persona = layer.get()
        text = layer.render()   # YAML string, injected into system prompt
    """

    def __init__(self, store: MemoryStore, key: str = _IDENTITY_KEY) -> None:
        self._store = store
        self._key = key

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self) -> dict[str, Any]:
        """Return the identity as a dict. Empty dict if not set."""
        row = self._store.conn.execute(
            "SELECT value FROM identity WHERE key = ?", (self._key,)
        ).fetchone()
        if row is None:
            return {}
        try:
            parsed = yaml.safe_load(row[0])
            return parsed if isinstance(parsed, dict) else {}
        except yaml.YAMLError:
            return {}

    def render(self) -> str:
        """
        Return identity as a compact YAML string for injection into prompts.
        Truncated to _MAX_CHARS if needed.
        Budget: ~100 tokens.
        """
        data = self.get()
        if not data:
            return "## L0 IDENTITY\n(not configured)"

        yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True).strip()
        if len(yaml_str) > _MAX_CHARS:
            yaml_str = yaml_str[:_MAX_CHARS - 3] + "..."

        return f"## L0 IDENTITY\n{yaml_str}"

    def token_count(self) -> int:
        """Estimate token count of the rendered identity (~4 chars per token)."""
        return len(self.render()) // 4

    # ── Write ─────────────────────────────────────────────────────────────

    def set(self, data: dict[str, Any]) -> None:
        """
        Set the identity. Replaces any existing identity.

        Args:
            data: Dict of identity fields (name, role, traits, etc.)
        """
        if not isinstance(data, dict):
            raise TypeError(f"Identity data must be a dict, got {type(data).__name__}")

        yaml_str = yaml.dump(data, default_flow_style=False, allow_unicode=True)
        now = time.time()

        self._store.wal.record("UPSERT", "identity", record_id=self._key, data=data)

        self._store.conn.execute(
            "INSERT INTO identity(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (self._key, yaml_str, now),
        )
        self._store.conn.commit()

    def update(self, updates: dict[str, Any]) -> None:
        """
        Merge updates into the existing identity (non-destructive).

        Args:
            updates: Partial dict — only provided keys are changed.
        """
        current = self.get()
        merged = {**current, **updates}
        self.set(merged)

    def clear(self) -> None:
        """Remove the identity record."""
        self._store.wal.record("DELETE", "identity", record_id=self._key)
        self._store.conn.execute("DELETE FROM identity WHERE key = ?", (self._key,))
        self._store.conn.commit()

    def exists(self) -> bool:
        """Return True if an identity has been set."""
        row = self._store.conn.execute(
            "SELECT 1 FROM identity WHERE key = ?", (self._key,)
        ).fetchone()
        return row is not None
