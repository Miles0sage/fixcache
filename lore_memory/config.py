"""
config.py — YAML config loader with sensible defaults for lore-memory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG: dict[str, Any] = {
    "db_path": "~/.lore-memory/default.db",
    "layers": {
        "identity": {
            "max_tokens": 100,
        },
        "drawers": {
            "count": 15,
            "max_tokens": 800,
        },
        "temporal": {
            "decay_halflife_days": 30,
            "recency_weight": 0.3,
        },
        "search": {
            "engine": "fts5",   # fts5 | hybrid | neural
            "top_k": 5,
            "context_window": 2,
        },
        "knowledge_graph": {
            "auto_extract": True,
            "max_walk_hops": 2,
            "max_tokens": 500,
        },
        "reflection": {
            "auto_reflect_every": 50,
            "require_llm": False,
        },
    },
    "darwin": {
        "enabled": True,
        "pattern_threshold": 3,
        "fleet_sync": False,
    },
    "security": {
        "auto_scan": True,
        "redact_pii": False,
        "lore_review_path": None,
    },
    "embedding": {
        "model": None,
        "chunk_size": 512,
        "chunk_overlap": 64,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Returns a new dict (immutable pattern)."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


class LoreConfig:
    """
    Configuration for lore-memory. Loaded from .lore-memory.yml (or provided dict).

    Search order:
        1. Explicit path passed to constructor
        2. ./.lore-memory.yml  (current working directory)
        3. ~/.lore-memory.yml  (user home)
        4. Built-in defaults
    """

    def __init__(self, config_path: str | None = None, overrides: dict | None = None) -> None:
        raw = self._load(config_path)
        if overrides:
            raw = _deep_merge(raw, overrides)
        self._data = raw

    # ── Loader ────────────────────────────────────────────────────────────

    def _load(self, explicit_path: str | None) -> dict:
        candidates: list[Path] = []
        if explicit_path:
            candidates.append(Path(explicit_path).expanduser())
        candidates += [
            Path(".lore-memory.yml"),
            Path("~/.lore-memory.yml").expanduser(),
        ]

        for p in candidates:
            if p.exists():
                with open(p) as f:
                    user_cfg = yaml.safe_load(f) or {}
                return _deep_merge(_DEFAULT_CONFIG, user_cfg)

        return dict(_DEFAULT_CONFIG)

    # ── Accessors ─────────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Dot-notation accessor: config.get('layers.search.top_k')"""
        parts = key.split(".")
        node: Any = self._data
        for part in parts:
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return default
            if node is None:
                return default
        return node

    @property
    def db_path(self) -> str:
        raw = self._data.get("db_path", "~/.lore-memory/default.db")
        return str(Path(raw).expanduser())

    @property
    def layers(self) -> dict:
        return self._data.get("layers", {})

    @property
    def darwin(self) -> dict:
        return self._data.get("darwin", {})

    @property
    def security(self) -> dict:
        return self._data.get("security", {})

    @property
    def embedding(self) -> dict:
        return self._data.get("embedding", {})

    def to_dict(self) -> dict:
        """Return the full resolved config as a plain dict."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"LoreConfig(db_path={self.db_path!r})"
