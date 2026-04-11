"""
tests/test_layers.py — Tests for layers/identity.py and LoreMemory high-level API.
"""

import pytest

from lore_memory.layers.identity import IdentityLayer
from lore_memory.core.store import MemoryStore
from lore_memory import LoreMemory
from lore_memory.config import LoreConfig


# ── IdentityLayer tests ───────────────────────────────────────────────────────

class TestIdentityLayerGet:
    def test_get_empty_returns_empty_dict(self, identity):
        assert identity.get() == {}

    def test_exists_false_initially(self, identity):
        assert identity.exists() is False

    def test_render_empty_returns_default(self, identity):
        rendered = identity.render()
        assert "L0 IDENTITY" in rendered
        assert "not configured" in rendered

    def test_token_count_empty(self, identity):
        count = identity.token_count()
        assert isinstance(count, int)
        assert count >= 0


class TestIdentityLayerSet:
    def test_set_stores_data(self, identity):
        identity.set({"name": "Miles", "role": "CTO"})
        data = identity.get()
        assert data["name"] == "Miles"
        assert data["role"] == "CTO"

    def test_exists_true_after_set(self, identity):
        identity.set({"name": "Alice"})
        assert identity.exists() is True

    def test_set_replaces_existing(self, identity):
        identity.set({"name": "Alice", "role": "engineer"})
        identity.set({"name": "Bob"})
        data = identity.get()
        assert data["name"] == "Bob"
        assert "role" not in data  # Full replace

    def test_set_raises_on_non_dict(self, identity):
        with pytest.raises(TypeError):
            identity.set("not a dict")

    def test_set_creates_wal_entry(self, store, identity):
        before = store.wal.count()
        identity.set({"name": "WAL test"})
        assert store.wal.count() == before + 1

    def test_set_complex_data(self, identity):
        data = {
            "name": "Miles",
            "role": "CTO",
            "style": "ship fast",
            "preferences": ["dark mode", "vim", "terminal"],
        }
        identity.set(data)
        retrieved = identity.get()
        assert retrieved["preferences"] == ["dark mode", "vim", "terminal"]


class TestIdentityLayerUpdate:
    def test_update_merges_fields(self, identity):
        identity.set({"name": "Alice", "role": "engineer"})
        identity.update({"role": "senior engineer", "team": "platform"})
        data = identity.get()
        assert data["name"] == "Alice"          # preserved
        assert data["role"] == "senior engineer"  # updated
        assert data["team"] == "platform"         # added

    def test_update_on_empty_creates(self, identity):
        identity.update({"name": "Bob"})
        assert identity.get()["name"] == "Bob"

    def test_update_overwrites_specific_key(self, identity):
        identity.set({"theme": "light"})
        identity.update({"theme": "dark"})
        assert identity.get()["theme"] == "dark"


class TestIdentityLayerRender:
    def test_render_contains_yaml(self, identity):
        identity.set({"name": "Miles", "role": "CTO"})
        rendered = identity.render()
        assert "L0 IDENTITY" in rendered
        assert "Miles" in rendered
        assert "CTO" in rendered

    def test_render_respects_max_chars(self, identity):
        # Set a very large identity
        big_data = {f"key_{i}": f"value_{i}" * 20 for i in range(50)}
        identity.set(big_data)
        rendered = identity.render()
        # Should be truncated — the YAML blob + header should not be enormous
        assert len(rendered) <= 600  # header + 400 max content + some buffer

    def test_token_count_after_set(self, identity):
        identity.set({"name": "Miles", "role": "CTO", "style": "ship fast"})
        count = identity.token_count()
        assert count > 0
        assert count <= 100  # within L0 budget


class TestIdentityLayerClear:
    def test_clear_removes_identity(self, identity):
        identity.set({"name": "Alice"})
        identity.clear()
        assert identity.exists() is False
        assert identity.get() == {}

    def test_clear_on_empty_no_error(self, identity):
        identity.clear()  # Should not raise

    def test_clear_creates_wal_entry(self, store, identity):
        identity.set({"name": "test"})
        before = store.wal.count()
        identity.clear()
        assert store.wal.count() == before + 1


# ── LoreMemory high-level API tests ──────────────────────────────────────────

class TestLoreMemoryRemember:
    def test_remember_returns_id(self, mem):
        mid = mem.remember("User prefers dark mode")
        assert isinstance(mid, str)
        assert len(mid) == 36

    def test_remember_stores_fact(self, mem):
        mid = mem.remember("User likes vim")
        result = mem.store.get(mid)
        assert result["content"] == "User likes vim"
        assert result["memory_type"] == "fact"

    def test_remember_with_type(self, mem):
        mid = mem.remember("Had a great meeting", memory_type="experience")
        result = mem.store.get(mid)
        assert result["memory_type"] == "experience"

    def test_remember_with_metadata(self, mem):
        mid = mem.remember("dark mode", metadata={"session": "abc"})
        result = mem.store.get(mid)
        assert result["metadata"]["session"] == "abc"


class TestLoreMemoryRecall:
    def test_recall_finds_relevant(self, mem):
        mem.remember("User prefers dark mode in all applications")
        mem.remember("The stock market is up today")
        results = mem.recall("dark mode preference")
        assert len(results) >= 1
        assert any("dark mode" in r["content"] for r in results)

    def test_recall_empty_returns_empty(self, mem):
        results = mem.recall("")
        assert results == []

    def test_recall_top_k(self, mem):
        for i in range(10):
            mem.remember(f"memory {i} about recall testing search")
        results = mem.recall("recall testing", top_k=2)
        assert len(results) <= 2

    def test_recall_type_filter(self, mem):
        mem.remember("fact about python", memory_type="fact")
        mem.remember("experience with python", memory_type="experience")
        results = mem.recall("python", memory_type="fact")
        assert all(r["memory_type"] == "fact" for r in results)


class TestLoreMemoryStats:
    def test_stats_empty(self, mem):
        s = mem.stats()
        assert s["total"] == 0
        assert s["identity_configured"] is False

    def test_stats_after_remember(self, mem):
        mem.remember("a")
        mem.remember("b")
        s = mem.stats()
        assert s["total"] == 2

    def test_stats_with_identity(self, mem):
        mem.identity.set({"name": "Miles"})
        s = mem.stats()
        assert s["identity_configured"] is True
        assert s["identity_tokens"] > 0


class TestLoreMemoryContextManager:
    def test_context_manager_closes(self):
        with LoreMemory(db_path=":memory:") as m:
            mid = m.remember("test")
            assert mid is not None
        assert m.store._conn is None

    def test_repr(self):
        m = LoreMemory(db_path=":memory:")
        r = repr(m)
        assert "LoreMemory" in r
        m.close()


# ── LoreConfig tests ──────────────────────────────────────────────────────────

class TestLoreConfig:
    def test_defaults(self):
        cfg = LoreConfig()
        assert cfg.get("layers.search.top_k") == 5
        assert cfg.get("layers.drawers.count") == 15
        assert cfg.get("layers.identity.max_tokens") == 100

    def test_dot_notation_nested(self):
        cfg = LoreConfig()
        assert cfg.get("layers.temporal.decay_halflife_days") == 30

    def test_dot_notation_missing_returns_default(self):
        cfg = LoreConfig()
        assert cfg.get("nonexistent.key", "fallback") == "fallback"

    def test_overrides_merge(self):
        cfg = LoreConfig(overrides={"layers": {"search": {"top_k": 10}}})
        assert cfg.get("layers.search.top_k") == 10
        # Other defaults preserved
        assert cfg.get("layers.drawers.count") == 15

    def test_db_path_expands_home(self):
        cfg = LoreConfig()
        assert not cfg.db_path.startswith("~")

    def test_to_dict(self):
        cfg = LoreConfig()
        d = cfg.to_dict()
        assert isinstance(d, dict)
        assert "layers" in d
        assert "darwin" in d

    def test_repr(self):
        cfg = LoreConfig()
        r = repr(cfg)
        assert "LoreConfig" in r
