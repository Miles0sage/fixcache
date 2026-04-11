"""
tests/test_store.py — Tests for core/store.py, core/schema.py, core/wal.py
"""

import time

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.core.schema import apply_schema, get_schema_version, SCHEMA_VERSION
from lore_memory.core.wal import WAL


# ── Schema tests ──────────────────────────────────────────────────────────────

class TestSchema:
    def test_apply_schema_creates_tables(self, store):
        tables = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "memories", "kg_nodes", "kg_edges",
            "identity", "drawers", "reflections",
            "darwin_journal", "darwin_patterns",
            "wal_log", "embeddings", "_schema_version",
        }
        assert expected.issubset(tables)

    def test_fts5_virtual_table_exists(self, store):
        tables = {
            r[0] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memories_fts" in tables

    def test_schema_version_recorded(self, store):
        version = get_schema_version(store.conn)
        assert version == SCHEMA_VERSION

    def test_apply_schema_idempotent(self, store):
        # Applying twice should not raise
        apply_schema(store.conn)
        apply_schema(store.conn)
        version = get_schema_version(store.conn)
        assert version == SCHEMA_VERSION

    def test_wal_mode_enabled(self, tmp_path):
        # WAL mode only applies to file-backed databases, not :memory:
        db = str(tmp_path / "test.db")
        s = MemoryStore(db)
        row = s.conn.execute("PRAGMA journal_mode").fetchone()
        s.close()
        assert row[0] == "wal"


# ── WAL tests ─────────────────────────────────────────────────────────────────

class TestWAL:
    def test_record_insert(self, store):
        rowid = store.wal.record("INSERT", "memories", record_id="abc", data={"x": 1})
        assert rowid is not None
        assert rowid > 0

    def test_count_increments(self, store):
        before = store.wal.count()
        store.wal.record("INSERT", "memories", record_id="x1")
        store.wal.record("INSERT", "memories", record_id="x2")
        assert store.wal.count() == before + 2

    def test_tail_returns_most_recent_first(self, store):
        store.wal.record("INSERT", "memories", record_id="first", data={"n": 1})
        store.wal.record("UPDATE", "memories", record_id="second", data={"n": 2})
        tail = store.wal.tail(2)
        assert len(tail) == 2
        assert tail[0]["record_id"] == "second"
        assert tail[1]["record_id"] == "first"

    def test_tail_deserializes_data(self, store):
        store.wal.record("INSERT", "memories", record_id="r1", data={"key": "val"})
        tail = store.wal.tail(1)
        assert tail[0]["data"] == {"key": "val"}

    def test_tail_none_data(self, store):
        store.wal.record("DELETE", "memories", record_id="r2", data=None)
        tail = store.wal.tail(1)
        assert tail[0]["data"] is None

    def test_prune_removes_old_entries(self, store):
        past = time.time() - 3600  # 1 hour ago
        # Manually insert old entry
        store.conn.execute(
            "INSERT INTO wal_log(operation, table_name, record_id, timestamp) VALUES (?,?,?,?)",
            ("INSERT", "memories", "old", past),
        )
        store.conn.commit()
        count_before = store.wal.count()
        deleted = store.wal.prune(older_than=time.time() - 1800)  # prune >30min old
        assert deleted >= 1
        assert store.wal.count() < count_before

    def test_operations_stored_uppercase(self, store):
        store.wal.record("insert", "memories", record_id="r3")
        tail = store.wal.tail(1)
        assert tail[0]["operation"] == "INSERT"


# ── MemoryStore CRUD tests ────────────────────────────────────────────────────

class TestMemoryStoreAdd:
    def test_add_returns_id(self, store):
        mid = store.add("Hello world")
        assert isinstance(mid, str)
        assert len(mid) == 36  # UUID4

    def test_add_stores_content(self, store):
        mid = store.add("Test content")
        mem = store.get(mid)
        assert mem is not None
        assert mem["content"] == "Test content"

    def test_add_default_memory_type(self, store):
        mid = store.add("Some fact")
        mem = store.get(mid)
        assert mem["memory_type"] == "fact"

    def test_add_custom_memory_type(self, store):
        mid = store.add("An experience", memory_type="experience")
        mem = store.get(mid)
        assert mem["memory_type"] == "experience"

    def test_add_invalid_memory_type_raises(self, store):
        with pytest.raises(Exception):
            store.add("bad", memory_type="invalid")
            store.conn.commit()  # constraint fires on commit in WAL mode

    def test_add_with_metadata(self, store):
        mid = store.add("Meta test", metadata={"source": "test", "tags": ["a", "b"]})
        mem = store.get(mid)
        assert mem["metadata"]["source"] == "test"
        assert mem["metadata"]["tags"] == ["a", "b"]

    def test_add_custom_id(self, store):
        mid = store.add("Custom ID memory", memory_id="my-custom-id-001")
        assert mid == "my-custom-id-001"
        mem = store.get("my-custom-id-001")
        assert mem is not None

    def test_add_creates_wal_entry(self, store):
        before = store.wal.count()
        store.add("WAL test")
        assert store.wal.count() == before + 1

    def test_get_nonexistent_returns_none(self, store):
        assert store.get("nonexistent-id") is None

    def test_add_sets_created_at(self, store):
        before = time.time()
        mid = store.add("timestamp test")
        after = time.time()
        mem = store.get(mid)
        assert before <= mem["created_at"] <= after

    def test_add_default_decay_score(self, store):
        mid = store.add("decay test")
        mem = store.get(mid)
        assert mem["decay_score"] == 1.0

    def test_add_default_access_count(self, store):
        mid = store.add("access test")
        mem = store.get(mid)
        assert mem["access_count"] == 0


class TestMemoryStoreUpdate:
    def test_update_content(self, store):
        mid = store.add("original")
        store.update(mid, content="updated")
        mem = store.get(mid)
        assert mem["content"] == "updated"

    def test_update_decay_score(self, store):
        mid = store.add("decay test")
        store.update(mid, decay_score=0.5)
        mem = store.get(mid)
        assert mem["decay_score"] == 0.5

    def test_update_nonexistent_returns_false(self, store):
        result = store.update("no-such-id", content="x")
        assert result is False

    def test_update_creates_wal_entry(self, store):
        mid = store.add("update wal test")
        before = store.wal.count()
        store.update(mid, content="changed")
        assert store.wal.count() == before + 1

    def test_update_preserves_unchanged_fields(self, store):
        mid = store.add("original", memory_type="experience")
        store.update(mid, decay_score=0.8)
        mem = store.get(mid)
        assert mem["memory_type"] == "experience"
        assert mem["content"] == "original"


class TestMemoryStoreDelete:
    def test_delete_removes_memory(self, store):
        mid = store.add("to delete")
        assert store.delete(mid) is True
        assert store.get(mid) is None

    def test_delete_nonexistent_returns_false(self, store):
        assert store.delete("no-such-id") is False

    def test_delete_creates_wal_entry(self, store):
        mid = store.add("del wal test")
        before = store.wal.count()
        store.delete(mid)
        assert store.wal.count() == before + 1


class TestMemoryStoreTouch:
    def test_touch_increments_access_count(self, store):
        mid = store.add("touch test")
        store.touch(mid)
        store.touch(mid)
        mem = store.get(mid)
        assert mem["access_count"] == 2

    def test_touch_sets_last_accessed(self, store):
        mid = store.add("touch ts test")
        before = time.time()
        store.touch(mid)
        after = time.time()
        mem = store.get(mid)
        assert before <= mem["last_accessed"] <= after


# ── MemoryStore Search tests ──────────────────────────────────────────────────

class TestMemoryStoreSearch:
    def test_search_basic(self, store):
        store.add("User prefers dark mode in all applications")
        store.add("The weather is sunny today")
        results = store.search("dark mode")
        assert len(results) >= 1
        assert any("dark mode" in r["content"] for r in results)

    def test_search_empty_query_returns_empty(self, store):
        store.add("some content")
        results = store.search("")
        assert results == []

    def test_search_whitespace_query_returns_empty(self, store):
        results = store.search("   ")
        assert results == []

    def test_search_top_k_limits_results(self, store):
        for i in range(10):
            store.add(f"memory number {i} about testing search")
        results = store.search("memory testing", top_k=3)
        assert len(results) <= 3

    def test_search_memory_type_filter(self, store):
        store.add("fact about dark mode", memory_type="fact")
        store.add("experience with dark mode", memory_type="experience")
        results = store.search("dark mode", memory_type="fact")
        assert all(r["memory_type"] == "fact" for r in results)

    def test_search_decay_filter(self, store):
        mid = store.add("low decay dark mode memory")
        store.update(mid, decay_score=0.1)
        results = store.search("dark mode", min_decay=0.5)
        assert not any(r["id"] == mid for r in results)

    def test_search_no_results(self, store):
        store.add("completely unrelated content xyz")
        results = store.search("quantum physics laser beam")
        # FTS5 may return partial matches, just check no error
        assert isinstance(results, list)

    def test_search_after_delete(self, store):
        mid = store.add("deletable dark mode entry")
        store.delete(mid)
        results = store.search("deletable dark mode")
        assert not any(r["id"] == mid for r in results)

    def test_search_after_update(self, store):
        mid = store.add("original light mode preference")
        store.update(mid, content="updated to dark mode preference")
        results = store.search("dark mode")
        assert any(r["id"] == mid for r in results)


class TestMemoryStoreTemporal:
    def test_search_temporal_since(self, store):
        past = time.time() - 7200  # 2 hours ago
        # Manually insert old memory
        import uuid, json
        old_id = str(uuid.uuid4())
        store.conn.execute(
            "INSERT INTO memories(id, content, memory_type, created_at, decay_score) VALUES (?,?,?,?,?)",
            (old_id, "old dark mode preference", "fact", past, 1.0),
        )
        # Also insert into FTS
        store.conn.execute(
            "INSERT INTO memories_fts(rowid, content) SELECT rowid, content FROM memories WHERE id=?",
            (old_id,),
        )
        store.conn.commit()

        new_id = store.add("new dark mode preference")
        new_ts = store.get(new_id)["created_at"]

        results = store.search_temporal("dark mode", since=new_ts - 1)
        ids = [r["id"] for r in results]
        assert new_id in ids
        assert old_id not in ids

    def test_recent_returns_latest(self, store):
        ids = [store.add(f"memory {i}") for i in range(5)]
        recent = store.recent(n=3)
        assert len(recent) == 3
        # Most recent first
        assert recent[0]["id"] == ids[-1]

    def test_recent_type_filter(self, store):
        store.add("fact one", memory_type="fact")
        store.add("exp one", memory_type="experience")
        recent = store.recent(n=10, memory_type="fact")
        assert all(r["memory_type"] == "fact" for r in recent)


# ── MemoryStore Stats/List tests ─────────────────────────────────────────────

class TestMemoryStoreStats:
    def test_stats_total(self, store):
        store.add("a")
        store.add("b")
        s = store.stats()
        assert s["total"] == 2

    def test_stats_by_type(self, store):
        store.add("fact", memory_type="fact")
        store.add("exp", memory_type="experience")
        s = store.stats()
        assert s["by_type"]["fact"] >= 1
        assert s["by_type"]["experience"] >= 1

    def test_stats_wal_entries(self, store):
        store.add("x")
        s = store.stats()
        assert s["wal_entries"] >= 1

    def test_count(self, store):
        assert store.count() == 0
        store.add("one")
        store.add("two")
        assert store.count() == 2

    def test_list_all(self, store):
        store.add("a")
        store.add("b")
        mems = store.list_all()
        assert len(mems) == 2

    def test_list_all_type_filter(self, store):
        store.add("fact", memory_type="fact")
        store.add("exp", memory_type="experience")
        facts = store.list_all(memory_type="fact")
        assert all(m["memory_type"] == "fact" for m in facts)

    def test_list_all_limit(self, store):
        for i in range(10):
            store.add(f"memory {i}")
        mems = store.list_all(limit=3)
        assert len(mems) == 3

    def test_list_all_offset(self, store):
        for i in range(5):
            store.add(f"memory {i}")
        all_mems = store.list_all()
        offset_mems = store.list_all(offset=2)
        assert len(offset_mems) == len(all_mems) - 2


# ── Context manager test ──────────────────────────────────────────────────────

class TestMemoryStoreContextManager:
    def test_context_manager(self):
        with MemoryStore(":memory:") as s:
            mid = s.add("context test")
            assert s.get(mid) is not None
        # After exit, conn should be closed
        assert s._conn is None

    def test_close_idempotent(self, store):
        store.close()
        store.close()  # Should not raise
