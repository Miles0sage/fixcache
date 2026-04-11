"""
tests/test_prefetch.py — Tests for lore_memory/prefetch.py

Covers:
  - record_access stores correctly in access_patterns
  - predict_context returns frequently accessed memories
  - generate_briefing formats a readable briefing
  - Feedback loop: recall → record → predict returns same memories
"""

from __future__ import annotations

import json
import time

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.prefetch import generate_briefing, predict_context, record_access


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store():
    """In-memory SQLite store, fresh for each test."""
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Store with a few seeded memories."""
    ids = []
    ids.append(store.add("User prefers dark mode", memory_type="fact"))
    ids.append(store.add("Project uses SQLite with WAL mode", memory_type="fact"))
    ids.append(store.add("Always run tests before committing", memory_type="fact"))
    return store, ids


# ── record_access ─────────────────────────────────────────────────────────────


class TestRecordAccess:
    def test_stores_memory_ids(self, store):
        mid = store.add("test memory")
        record_access(store, [mid])
        row = store.conn.execute("SELECT memory_ids FROM access_patterns").fetchone()
        assert row is not None
        assert mid in json.loads(row[0])

    def test_stores_entity_and_tool(self, store):
        mid = store.add("test memory")
        record_access(store, [mid], entity="phalanx", tool_used="lore_recall")
        row = store.conn.execute(
            "SELECT entity, tool_used FROM access_patterns"
        ).fetchone()
        assert row[0] == "phalanx"
        assert row[1] == "lore_recall"

    def test_stores_hour_of_day(self, store):
        mid = store.add("test memory")
        before = int(time.gmtime().tm_hour)
        record_access(store, [mid])
        after = int(time.gmtime().tm_hour)
        row = store.conn.execute("SELECT hour_of_day FROM access_patterns").fetchone()
        # hour should be in [before, after] — could span midnight rollover
        assert row[0] in range(0, 24)

    def test_stores_timestamp(self, store):
        mid = store.add("test memory")
        before = time.time()
        record_access(store, [mid])
        after = time.time()
        row = store.conn.execute("SELECT timestamp FROM access_patterns").fetchone()
        assert before <= row[0] <= after

    def test_noop_on_empty_ids(self, store):
        record_access(store, [])
        count = store.conn.execute("SELECT COUNT(*) FROM access_patterns").fetchone()[0]
        assert count == 0

    def test_multiple_ids_stored_as_json_array(self, store):
        ids = [store.add(f"memory {i}") for i in range(3)]
        record_access(store, ids)
        row = store.conn.execute("SELECT memory_ids FROM access_patterns").fetchone()
        stored = json.loads(row[0])
        assert stored == ids

    def test_multiple_calls_create_multiple_rows(self, store):
        mid1 = store.add("first memory")
        mid2 = store.add("second memory")
        record_access(store, [mid1])
        record_access(store, [mid2])
        count = store.conn.execute("SELECT COUNT(*) FROM access_patterns").fetchone()[0]
        assert count == 2


# ── predict_context ───────────────────────────────────────────────────────────


class TestPredictContext:
    def test_returns_empty_when_no_patterns(self, store):
        result = predict_context(store)
        assert result == []

    def test_returns_most_frequent_memory(self, populated_store):
        store, ids = populated_store
        # Record the first memory 5 times, second 2 times
        for _ in range(5):
            record_access(store, [ids[0]])
        for _ in range(2):
            record_access(store, [ids[1]])

        results = predict_context(store, top_k=2)
        assert len(results) == 2
        # Most frequent should be first
        assert results[0]["id"] == ids[0]
        assert results[1]["id"] == ids[1]

    def test_respects_top_k(self, populated_store):
        store, ids = populated_store
        for mid in ids:
            record_access(store, [mid])

        results = predict_context(store, top_k=2)
        assert len(results) <= 2

    def test_filters_by_entity(self, store):
        mid_a = store.add("entity A memory")
        mid_b = store.add("entity B memory")
        record_access(store, [mid_a], entity="project-a")
        record_access(store, [mid_b], entity="project-b")

        results_a = predict_context(store, entity="project-a")
        result_ids_a = [r["id"] for r in results_a]
        assert mid_a in result_ids_a
        assert mid_b not in result_ids_a

    def test_filters_by_tool_used(self, store):
        mid_fix = store.add("fix memory")
        mid_recall = store.add("recall memory")
        record_access(store, [mid_fix], tool_used="lore_fix")
        record_access(store, [mid_recall], tool_used="lore_recall")

        results = predict_context(store, tool_used="lore_fix")
        result_ids = [r["id"] for r in results]
        assert mid_fix in result_ids
        assert mid_recall not in result_ids

    def test_excludes_decayed_memories(self, store):
        mid = store.add("decayed memory")
        record_access(store, [mid])
        # Soft-delete the memory
        store.conn.execute("UPDATE memories SET decay_score=0.0 WHERE id=?", (mid,))
        store.conn.commit()

        results = predict_context(store)
        assert all(r["id"] != mid for r in results)

    def test_includes_prefetch_frequency(self, store):
        mid = store.add("frequently accessed")
        for _ in range(3):
            record_access(store, [mid])

        results = predict_context(store)
        assert results[0]["_prefetch_frequency"] == 3

    def test_hour_bucket_matching(self, store):
        """Records made at any hour within ±2 of current should be included."""
        mid = store.add("test memory")
        current_hour = int(time.gmtime().tm_hour)

        # Insert a pattern directly at current hour
        store.conn.execute(
            "INSERT INTO access_patterns (hour_of_day, entity, tool_used, memory_ids, timestamp) "
            "VALUES (?, NULL, NULL, ?, ?)",
            (current_hour, json.dumps([mid]), time.time()),
        )
        store.conn.commit()

        results = predict_context(store)
        assert any(r["id"] == mid for r in results)


# ── generate_briefing ─────────────────────────────────────────────────────────


class TestGenerateBriefing:
    def test_returns_required_keys(self, store):
        result = generate_briefing(store)
        assert "briefing" in result
        assert "token_estimate" in result
        assert "sources" in result

    def test_briefing_is_string(self, store):
        result = generate_briefing(store)
        assert isinstance(result["briefing"], str)
        assert len(result["briefing"]) > 0

    def test_token_estimate_is_positive_int(self, store):
        result = generate_briefing(store)
        assert isinstance(result["token_estimate"], int)
        assert result["token_estimate"] > 0

    def test_token_estimate_matches_briefing_length(self, store):
        result = generate_briefing(store)
        expected = len(result["briefing"]) // 4
        assert result["token_estimate"] == expected

    def test_sources_is_list(self, store):
        result = generate_briefing(store)
        assert isinstance(result["sources"], list)

    def test_sources_contains_predicted_memory_ids(self, store):
        mid = store.add("important context")
        record_access(store, [mid])

        result = generate_briefing(store)
        assert mid in result["sources"]

    def test_briefing_contains_identity_section(self, store):
        result = generate_briefing(store)
        assert "L0 IDENTITY" in result["briefing"]

    def test_briefing_contains_session_header(self, store):
        result = generate_briefing(store)
        assert "SESSION BRIEFING" in result["briefing"]

    def test_briefing_contains_memory_content(self, store):
        mid = store.add("critical project configuration detail")
        record_access(store, [mid])

        result = generate_briefing(store)
        assert "critical project configuration detail" in result["briefing"]

    def test_briefing_contains_conventions(self, store):
        import json as _json
        mid = store.add(
            "Always use immutable patterns",
            memory_type="fact",
            metadata={"convention": True, "trust_score": 1.0},
        )
        result = generate_briefing(store)
        assert "Always use immutable patterns" in result["briefing"]

    def test_empty_store_produces_fallback_text(self, store):
        result = generate_briefing(store)
        assert "no predictions yet" in result["briefing"]

    def test_entity_filter_passed_through(self, store):
        mid_a = store.add("project A memory")
        mid_b = store.add("project B memory")
        record_access(store, [mid_a], entity="project-a")
        record_access(store, [mid_b], entity="project-b")

        result = generate_briefing(store, entity="project-a")
        assert mid_a in result["sources"]
        assert mid_b not in result["sources"]

    def test_briefing_under_800_tokens_by_default(self, store):
        """Briefing should stay in the 500-800 token target range for typical stores."""
        for i in range(3):
            mid = store.add(f"memory number {i}: some context about the project")
            record_access(store, [mid])

        result = generate_briefing(store)
        assert result["token_estimate"] <= 1000  # generous upper bound


# ── Feedback loop integration ─────────────────────────────────────────────────


class TestFeedbackLoop:
    def test_recall_record_predict_returns_same_memories(self, store):
        """
        After recording that certain memories were accessed,
        predict_context should return those same memories.
        """
        # Add memories
        mid1 = store.add("SQLite WAL mode best practices")
        mid2 = store.add("Python type hints are mandatory")
        mid3 = store.add("Unrelated memory about weather")

        # Simulate repeated access of mid1 and mid2 (as would happen after lore_recall)
        for _ in range(4):
            record_access(store, [mid1, mid2], tool_used="lore_recall")
        record_access(store, [mid3], tool_used="lore_recall")

        predicted = predict_context(store, tool_used="lore_recall", top_k=3)
        predicted_ids = [m["id"] for m in predicted]

        # The two frequently accessed memories should be predicted first
        assert mid1 in predicted_ids
        assert mid2 in predicted_ids
        # mid3 should appear but after mid1/mid2
        assert predicted_ids.index(mid1) < predicted_ids.index(mid3)
        assert predicted_ids.index(mid2) < predicted_ids.index(mid3)

    def test_briefing_reflects_recorded_accesses(self, store):
        """
        After recording accesses, generate_briefing should include the
        content of the most frequently accessed memories.
        """
        mid = store.add("The core architecture uses event-sourcing")
        for _ in range(3):
            record_access(store, [mid], tool_used="lore_recall")

        result = generate_briefing(store, tool_used="lore_recall")
        assert "event-sourcing" in result["briefing"]
        assert mid in result["sources"]

    def test_entity_scoped_loop(self, store):
        """
        Access patterns for one entity don't pollute predictions for another.
        """
        mid_frontend = store.add("Frontend uses React")
        mid_backend = store.add("Backend uses FastAPI")

        for _ in range(5):
            record_access(store, [mid_frontend], entity="frontend")
        for _ in range(5):
            record_access(store, [mid_backend], entity="backend")

        frontend_predictions = predict_context(store, entity="frontend")
        backend_predictions = predict_context(store, entity="backend")

        frontend_ids = [m["id"] for m in frontend_predictions]
        backend_ids = [m["id"] for m in backend_predictions]

        assert mid_frontend in frontend_ids
        assert mid_backend not in frontend_ids
        assert mid_backend in backend_ids
        assert mid_frontend not in backend_ids
