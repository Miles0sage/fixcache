"""
test_windsurf_sync.py — Tests for .windsurfrules sync target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.sync import (
    _WINDSURF_MARKER_END,
    _WINDSURF_MARKER_START,
    sync_all,
    sync_windsurfrules,
)


@pytest.fixture
def seeded_store(tmp_path: Path) -> MemoryStore:
    """Store with a mix of facts and one fix recipe."""
    store = MemoryStore(str(tmp_path / "ws.db"))
    store.add("Use type hints on all public APIs", memory_type="fact")
    store.add("Prefer dataclasses over plain dicts", memory_type="fact")
    return store


def test_sync_windsurfrules_creates_file(
    seeded_store: MemoryStore, tmp_path: Path
) -> None:
    target = tmp_path / ".windsurfrules"
    content = sync_windsurfrules(seeded_store, target)
    assert target.exists()
    assert _WINDSURF_MARKER_START in content
    assert _WINDSURF_MARKER_END in content
    assert "Use type hints" in content
    assert "dataclasses" in content


def test_sync_windsurfrules_preserves_user_content(
    seeded_store: MemoryStore, tmp_path: Path
) -> None:
    """User content outside the markers must survive a re-sync."""
    target = tmp_path / ".windsurfrules"
    target.write_text(
        "# User rules\n- Always use TypeScript strict mode\n\n",
        encoding="utf-8",
    )
    sync_windsurfrules(seeded_store, target)
    content = target.read_text(encoding="utf-8")
    assert "Always use TypeScript strict mode" in content
    assert _WINDSURF_MARKER_START in content


def test_sync_windsurfrules_idempotent(
    seeded_store: MemoryStore, tmp_path: Path
) -> None:
    """Running sync twice produces the same output (no duplication)."""
    target = tmp_path / ".windsurfrules"
    sync_windsurfrules(seeded_store, target)
    first = target.read_text(encoding="utf-8")
    sync_windsurfrules(seeded_store, target)
    second = target.read_text(encoding="utf-8")
    assert first == second
    # Only one block
    assert first.count(_WINDSURF_MARKER_START) == 1
    assert first.count(_WINDSURF_MARKER_END) == 1


def test_sync_all_includes_windsurf(
    seeded_store: MemoryStore, tmp_path: Path
) -> None:
    """sync_all reports windsurfrules in the created list."""
    report = sync_all(seeded_store, tmp_path)
    assert ".windsurfrules" in report["created"]
    assert (tmp_path / ".windsurfrules").exists()
