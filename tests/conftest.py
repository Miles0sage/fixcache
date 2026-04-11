"""
conftest.py — shared pytest fixtures for lore-memory tests.
"""

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.layers.identity import IdentityLayer
from lore_memory import LoreMemory


@pytest.fixture
def store():
    """In-memory SQLite store, fresh for each test."""
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def identity(store):
    """IdentityLayer backed by in-memory store."""
    return IdentityLayer(store)


@pytest.fixture
def mem():
    """LoreMemory instance backed by in-memory SQLite."""
    m = LoreMemory(db_path=":memory:")
    yield m
    m.close()
