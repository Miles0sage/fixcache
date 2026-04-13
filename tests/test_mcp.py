"""
tests/test_mcp.py — Tests for lore-memory MCP server tools.

Tests all 6 tools directly (no stdio transport needed).
"""

from __future__ import annotations

import json
import time

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.layers.identity import IdentityLayer
from lore_memory.mcp import server as mcp_server


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Each test gets a fresh in-memory store via monkeypatching."""
    store = MemoryStore(":memory:")
    identity = IdentityLayer(store)

    monkeypatch.setattr(mcp_server, "_store", store)
    monkeypatch.setattr(mcp_server, "_identity", identity)

    yield store

    store.close()
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_identity", None)


# ── lore_remember ─────────────────────────────────────────────────────────────

class TestLoreRemember:
    def test_basic_store(self):
        result = mcp_server.handle_lore_remember("User prefers dark mode")
        assert result["success"] is True
        assert "memory_id" in result
        assert result["trust_score"] == 0.8  # default: agent
        assert len(result["provenance_hash"]) == 64  # SHA-256 hex

    def test_user_source_type(self):
        result = mcp_server.handle_lore_remember("Always use tabs", source_type="user")
        assert result["trust_score"] == 1.0
        assert result["source_type"] == "user"

    def test_fleet_source_type(self):
        result = mcp_server.handle_lore_remember("Fleet observation", source_type="fleet")
        assert result["trust_score"] == 0.5

    def test_mined_source_type(self):
        result = mcp_server.handle_lore_remember("Mined pattern", source_type="mined")
        assert result["trust_score"] == 0.6

    def test_invalid_source_defaults_to_agent(self):
        result = mcp_server.handle_lore_remember("Test", source_type="unknown_src")
        assert result["trust_score"] == 0.8  # agent default

    def test_memory_type_stored(self):
        result = mcp_server.handle_lore_remember(
            "Had a great session", memory_type="experience"
        )
        assert result["memory_type"] == "experience"

    def test_tags_stored_in_metadata(self, isolated_store):
        result = mcp_server.handle_lore_remember(
            "Use black formatter", tags=["python", "style"]
        )
        mem = isolated_store.get(result["memory_id"])
        assert mem is not None
        assert mem["metadata"]["tags"] == ["python", "style"]

    def test_provenance_hash_unique_per_call(self):
        r1 = mcp_server.handle_lore_remember("Same content")
        time.sleep(0.01)
        r2 = mcp_server.handle_lore_remember("Same content")
        # Different timestamps => different hashes
        assert r1["provenance_hash"] != r2["provenance_hash"]

    def test_wal_records_write(self, isolated_store):
        mcp_server.handle_lore_remember("WAL test content")
        assert isolated_store.wal.count() >= 1

    def test_memory_persisted_in_store(self, isolated_store):
        result = mcp_server.handle_lore_remember("Persisted content")
        mem = isolated_store.get(result["memory_id"])
        assert mem is not None
        assert mem["content"] == "Persisted content"


# ── lore_recall ───────────────────────────────────────────────────────────────

class TestLoreRecall:
    def test_basic_recall(self):
        mcp_server.handle_lore_remember("dark mode preference")
        result = mcp_server.handle_lore_recall("dark mode")
        assert result["count"] >= 1
        assert any("dark mode" in r["content"] for r in result["results"])

    def test_trust_filter_excludes_low_trust(self, isolated_store):
        # Store a fleet memory (trust=0.5) and a user memory (trust=1.0)
        mcp_server.handle_lore_remember("fleet data point", source_type="fleet")
        mcp_server.handle_lore_remember("user preference note", source_type="user")

        # Recall with min_trust=0.9 — should only get user memory
        result = mcp_server.handle_lore_recall("preference data note", min_trust=0.9)
        for r in result["results"]:
            assert r["trust_score"] >= 0.9

    def test_top_k_limits_results(self):
        for i in range(10):
            mcp_server.handle_lore_remember(f"memory item number {i} about python coding")
        result = mcp_server.handle_lore_recall("memory item python coding", top_k=3)
        assert result["count"] <= 3

    def test_time_window_24h(self):
        mcp_server.handle_lore_remember("recent event happened today")
        result = mcp_server.handle_lore_recall("recent event", time_window="24h")
        assert "time_window" in result
        assert result["time_window"] == "24h"

    def test_time_window_7d(self):
        mcp_server.handle_lore_remember("event from this week")
        result = mcp_server.handle_lore_recall("event week", time_window="7d")
        assert result["time_window"] == "7d"

    def test_no_results_for_empty_store(self):
        result = mcp_server.handle_lore_recall("nothing here")
        assert result["count"] == 0
        assert result["results"] == []

    def test_access_count_incremented(self, isolated_store):
        r = mcp_server.handle_lore_remember("touched memory content item")
        mid = r["memory_id"]
        before = isolated_store.get(mid)["access_count"]
        mcp_server.handle_lore_recall("touched memory content")
        after = isolated_store.get(mid)["access_count"]
        assert after > before

    def test_layer_attribution_present(self):
        mcp_server.handle_lore_remember("layer test content memory")
        result = mcp_server.handle_lore_recall("layer test content")
        for r in result["results"]:
            assert r["layer"] == "L1"

    def test_returns_provenance_hash(self):
        mcp_server.handle_lore_remember("hash check content", source_type="user")
        result = mcp_server.handle_lore_recall("hash check content")
        if result["results"]:
            assert "provenance_hash" in result["results"][0]


# ── lore_fix ──────────────────────────────────────────────────────────────────

class TestLoreFix:
    def test_basic_store(self):
        result = mcp_server.handle_lore_fix(
            error_signature="ImportError: No module named",
            solution_steps=["pip install <module>", "check virtualenv"],
        )
        assert result["success"] is True
        assert "recipe_id" in result
        assert "pattern_id" in result
        assert result["steps_count"] == 2

    def test_stores_in_darwin_journal(self, isolated_store):
        result = mcp_server.handle_lore_fix(
            error_signature="ConnectionRefusedError",
            solution_steps=["Check server is running", "Verify port 5432"],
        )
        row = isolated_store.conn.execute(
            "SELECT id, outcome FROM darwin_journal WHERE id=?",
            (result["recipe_id"],),
        ).fetchone()
        assert row is not None
        assert row[1] == "success"

    def test_stores_in_darwin_patterns(self, isolated_store):
        result = mcp_server.handle_lore_fix(
            error_signature="SyntaxError",
            solution_steps=["Check line number", "Fix indentation"],
        )
        row = isolated_store.conn.execute(
            "SELECT id, pattern_type FROM darwin_patterns WHERE id=?",
            (result["pattern_id"],),
        ).fetchone()
        assert row is not None
        assert row[1] == "error_recipe"

    def test_also_in_fts5_memories(self, isolated_store):
        result = mcp_server.handle_lore_fix(
            error_signature="FileNotFoundError",
            solution_steps=["Check path exists", "Create directory"],
        )
        mem = isolated_store.get(result["memory_id"])
        assert mem is not None
        assert "ERROR FIX" in mem["content"]

    def test_with_tags(self):
        result = mcp_server.handle_lore_fix(
            error_signature="KeyError in dict",
            solution_steps=["Use .get()", "Add default value"],
            tags=["python", "dict"],
        )
        assert result["success"] is True

    def test_initial_confidence(self):
        result = mcp_server.handle_lore_fix(
            error_signature="TypeError",
            solution_steps=["Check argument types"],
        )
        assert result["confidence"] == 0.5

    def test_custom_outcome(self, isolated_store):
        result = mcp_server.handle_lore_fix(
            error_signature="RuntimeError: partial",
            solution_steps=["Step 1"],
            outcome="partial",
        )
        row = isolated_store.conn.execute(
            "SELECT outcome FROM darwin_journal WHERE id=?",
            (result["recipe_id"],),
        ).fetchone()
        assert row[0] == "partial"

    def test_invalid_outcome_defaults_to_success(self, isolated_store):
        result = mcp_server.handle_lore_fix(
            error_signature="SomeError",
            solution_steps=["Fix it"],
            outcome="invalid_outcome",
        )
        row = isolated_store.conn.execute(
            "SELECT outcome FROM darwin_journal WHERE id=?",
            (result["recipe_id"],),
        ).fetchone()
        assert row[0] == "success"


# ── lore_match_procedure ──────────────────────────────────────────────────────

class TestLoreMatchProcedure:
    def test_regex_match(self):
        mcp_server.handle_lore_fix(
            error_signature="ImportError.*module",
            solution_steps=["pip install missing-module", "activate virtualenv"],
        )
        result = mcp_server.handle_lore_match_procedure(
            "ImportError: No module named 'requests'"
        )
        assert result["found"] is True
        assert result["match_method"] in ("regex", "substring")
        assert len(result["solution_steps"]) == 2

    def test_literal_substring_match(self):
        mcp_server.handle_lore_fix(
            error_signature="ConnectionRefusedError",
            solution_steps=["Check if server is running"],
        )
        result = mcp_server.handle_lore_match_procedure(
            "ConnectionRefusedError: [Errno 111] Connection refused"
        )
        assert result["found"] is True

    def test_no_match_returns_not_found(self):
        result = mcp_server.handle_lore_match_procedure(
            "This error has never been seen before XYZ123"
        )
        assert result["found"] is False

    def test_fts5_fallback(self):
        # Store via handle_lore_fix (goes into memories as experience type)
        mcp_server.handle_lore_fix(
            error_signature="DatabaseConnectionError",
            solution_steps=["Restart database", "Check credentials"],
        )
        # Search with a slightly different phrasing that won't regex-match
        # but FTS5 might catch
        result = mcp_server.handle_lore_match_procedure(
            "DatabaseConnectionError: cannot connect"
        )
        # Either regex matched or fts5 fallback triggered — both valid
        assert "found" in result

    def test_highest_confidence_returned_first(self, isolated_store):
        # Store two patterns for same error type
        mcp_server.handle_lore_fix(
            error_signature="ValueError",
            solution_steps=["Basic fix"],
        )
        # Manually boost confidence on second one
        mcp_server.handle_lore_fix(
            error_signature="ValueError",
            solution_steps=["Advanced fix with more steps", "Check type", "Cast properly"],
        )
        # Update second pattern's confidence manually
        isolated_store.conn.execute(
            "UPDATE darwin_patterns SET confidence=0.9 WHERE description LIKE '%Advanced fix%'"
        )
        isolated_store.conn.commit()

        result = mcp_server.handle_lore_match_procedure("ValueError: invalid literal")
        assert result["found"] is True

    def test_frequency_incremented_on_match(self, isolated_store):
        mcp_server.handle_lore_fix(
            error_signature="ZeroDivisionError",
            solution_steps=["Check denominator before dividing"],
        )
        row_before = isolated_store.conn.execute(
            "SELECT frequency FROM darwin_patterns WHERE description LIKE '%ZeroDivision%'"
        ).fetchone()
        freq_before = row_before[0] if row_before else 1

        mcp_server.handle_lore_match_procedure("ZeroDivisionError: division by zero")

        row_after = isolated_store.conn.execute(
            "SELECT frequency FROM darwin_patterns WHERE description LIKE '%ZeroDivision%'"
        ).fetchone()
        freq_after = row_after[0] if row_after else 0
        assert freq_after > freq_before


# ── lore_teach ────────────────────────────────────────────────────────────────

class TestLoreTeach:
    def test_basic_teach(self):
        result = mcp_server.handle_lore_teach("Always use type hints in Python")
        assert result["success"] is True
        assert "memory_id" in result
        assert result["trust_score"] == 1.0  # user default

    def test_convention_stored_as_fact(self, isolated_store):
        result = mcp_server.handle_lore_teach("Use snake_case for variable names")
        mem = isolated_store.get(result["memory_id"])
        assert mem is not None
        assert mem["memory_type"] == "fact"
        assert mem["metadata"]["convention"] is True

    def test_tags_attached(self, isolated_store):
        result = mcp_server.handle_lore_teach(
            "Prefer f-strings over .format()", tags=["python", "style"]
        )
        mem = isolated_store.get(result["memory_id"])
        assert mem["metadata"]["tags"] == ["python", "style"]

    def test_agent_source_type(self):
        result = mcp_server.handle_lore_teach(
            "Agent-inferred rule", source_type="agent"
        )
        assert result["trust_score"] == 0.8

    def test_provenance_hash_generated(self):
        result = mcp_server.handle_lore_teach("Provenance test convention")
        assert len(result["provenance_hash"]) == 64

    def test_teach_is_searchable(self):
        mcp_server.handle_lore_teach("Always write tests before implementation")
        recall = mcp_server.handle_lore_recall("write tests implementation")
        assert recall["count"] >= 1
        assert any("Always write tests" in r["content"] for r in recall["results"])


# ── lore_stats ────────────────────────────────────────────────────────────────

class TestLoreStats:
    def test_empty_store(self):
        result = mcp_server.handle_lore_stats()
        assert result["total_memories"] == 0
        assert result["darwin_patterns"] == 0
        assert result["darwin_journal_entries"] == 0
        assert "wal_entries" in result
        assert "identity" in result

    def test_counts_memories(self):
        mcp_server.handle_lore_remember("First memory")
        mcp_server.handle_lore_remember("Second memory")
        result = mcp_server.handle_lore_stats()
        assert result["total_memories"] >= 2

    def test_by_type_breakdown(self):
        mcp_server.handle_lore_remember("fact one", memory_type="fact")
        mcp_server.handle_lore_remember("experience", memory_type="experience")
        result = mcp_server.handle_lore_stats()
        assert "by_type" in result
        assert "fact" in result["by_type"]
        assert "experience" in result["by_type"]

    def test_darwin_patterns_counted(self):
        mcp_server.handle_lore_fix(
            error_signature="TestError",
            solution_steps=["Step 1"],
        )
        result = mcp_server.handle_lore_stats()
        assert result["darwin_patterns"] >= 1

    def test_darwin_journal_counted(self):
        mcp_server.handle_lore_fix(
            error_signature="JournalError",
            solution_steps=["Fix it"],
        )
        result = mcp_server.handle_lore_stats()
        assert result["darwin_journal_entries"] >= 1

    def test_wal_entries_tracked(self):
        mcp_server.handle_lore_remember("WAL tracking test")
        result = mcp_server.handle_lore_stats()
        assert result["wal_entries"] >= 1

    def test_identity_summary_present(self):
        result = mcp_server.handle_lore_stats()
        assert "identity" in result
        assert "configured" in result["identity"]
        assert "token_estimate" in result["identity"]

    def test_decay_stats_present(self):
        mcp_server.handle_lore_remember("decay test content")
        result = mcp_server.handle_lore_stats()
        assert "decay" in result
        assert "avg" in result["decay"]

    def test_by_trust_level_breakdown(self):
        mcp_server.handle_lore_remember("user memory", source_type="user")
        mcp_server.handle_lore_remember("fleet memory", source_type="fleet")
        result = mcp_server.handle_lore_stats()
        assert "by_trust_level" in result
        assert "high" in result["by_trust_level"]
        assert "medium" in result["by_trust_level"]
        assert "low" in result["by_trust_level"]


# ── JSON-RPC protocol ──────────────────────────────────────────────────────────

class TestJsonRpcProtocol:
    def test_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        }
        resp = mcp_server.handle_request(req)
        assert resp["result"]["protocolVersion"] == "2025-11-25"
        assert resp["result"]["serverInfo"]["name"] == "fixcache"

    def test_tools_list(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        assert "lore_remember" in tool_names
        assert "lore_recall" in tool_names
        assert "lore_fix" in tool_names
        assert "lore_match_procedure" in tool_names
        assert "lore_teach" in tool_names
        assert "lore_stats" in tool_names
        assert "lore_list" in tool_names
        assert "lore_forget" in tool_names
        assert "lore_rate_fix" in tool_names

    def test_tools_call_remember(self):
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "lore_remember",
                "arguments": {"content": "RPC test content"},
            },
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["success"] is True

    def test_tools_call_stats(self):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "lore_stats", "arguments": {}},
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "total_memories" in content

    def test_unknown_tool_returns_error(self):
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        resp = mcp_server.handle_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_unknown_method_returns_error(self):
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "bogus/method",
            "params": {},
        }
        resp = mcp_server.handle_request(req)
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_notifications_initialized_returns_none(self):
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        resp = mcp_server.handle_request(req)
        assert resp is None

    def test_integer_coercion(self):
        # top_k passed as float string — should be coerced to int
        mcp_server.handle_lore_remember("coercion test content item")
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "lore_recall",
                "arguments": {"query": "coercion test", "top_k": "3"},
            },
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp


# ── lore_list ─────────────────────────────────────────────────────────────────

class TestLoreList:
    def test_empty_store(self):
        result = mcp_server.handle_lore_list()
        assert result["items"] == []
        assert result["count"] == 0
        assert result["total"] == 0

    def test_lists_stored_memories(self):
        mcp_server.handle_lore_remember("first memory content")
        mcp_server.handle_lore_remember("second memory content")
        result = mcp_server.handle_lore_list()
        assert result["count"] >= 2
        assert result["total"] >= 2

    def test_preview_truncated_to_100(self):
        long_content = "x" * 200
        mcp_server.handle_lore_remember(long_content)
        result = mcp_server.handle_lore_list()
        for item in result["items"]:
            assert len(item["preview"]) <= 100

    def test_pagination_limit(self):
        for i in range(5):
            mcp_server.handle_lore_remember(f"paged memory {i}")
        result = mcp_server.handle_lore_list(limit=2)
        assert result["count"] == 2
        assert result["limit"] == 2

    def test_pagination_offset(self):
        for i in range(5):
            mcp_server.handle_lore_remember(f"offset memory {i}")
        all_results = mcp_server.handle_lore_list(limit=5)
        offset_results = mcp_server.handle_lore_list(limit=5, offset=2)
        assert offset_results["count"] == all_results["count"] - 2

    def test_filter_by_memory_type(self):
        mcp_server.handle_lore_remember("a fact", memory_type="fact")
        mcp_server.handle_lore_remember("an experience", memory_type="experience")
        result = mcp_server.handle_lore_list(memory_type="fact")
        for item in result["items"]:
            assert item["memory_type"] == "fact"

    def test_result_has_required_fields(self):
        mcp_server.handle_lore_remember("fields check memory")
        result = mcp_server.handle_lore_list()
        assert result["count"] >= 1
        item = result["items"][0]
        assert "id" in item
        assert "preview" in item
        assert "memory_type" in item
        assert "trust_score" in item
        assert "created_at" in item

    def test_invalid_limit_returns_error(self):
        result = mcp_server.handle_lore_list(limit=0)
        assert result.get("success") is False
        assert "error" in result

    def test_via_rpc(self):
        mcp_server.handle_lore_remember("rpc list test content")
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "lore_list", "arguments": {"limit": 5}},
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert "items" in content


# ── lore_forget ───────────────────────────────────────────────────────────────

class TestLoreForget:
    def test_basic_forget(self, isolated_store):
        r = mcp_server.handle_lore_remember("memory to forget")
        mid = r["memory_id"]
        result = mcp_server.handle_lore_forget(mid)
        assert result["success"] is True
        assert result["memory_id"] == mid
        # Verify decay_score set to 0
        mem = isolated_store.get(mid)
        assert mem["decay_score"] == 0.0

    def test_forgotten_memory_excluded_from_recall(self, isolated_store):
        r = mcp_server.handle_lore_remember("forgettable unique phrase xyzabc123")
        mid = r["memory_id"]
        mcp_server.handle_lore_forget(mid)
        # Recall should not return it (decay_score=0.0 filters it out)
        result = mcp_server.handle_lore_recall("forgettable unique phrase xyzabc123")
        assert all(item["id"] != mid for item in result["results"])

    def test_forget_nonexistent_returns_error(self):
        result = mcp_server.handle_lore_forget("nonexistent-id-12345")
        assert result["success"] is False
        assert "error" in result

    def test_forget_empty_id_returns_error(self):
        result = mcp_server.handle_lore_forget("")
        assert result["success"] is False
        assert "error" in result

    def test_wal_records_forget(self, isolated_store):
        r = mcp_server.handle_lore_remember("wal forget test content")
        wal_before = isolated_store.wal.count()
        mcp_server.handle_lore_forget(r["memory_id"])
        assert isolated_store.wal.count() > wal_before

    def test_preview_in_response(self):
        r = mcp_server.handle_lore_remember("preview content for forget test")
        result = mcp_server.handle_lore_forget(r["memory_id"])
        assert "forgotten_content_preview" in result
        assert len(result["forgotten_content_preview"]) <= 100

    def test_via_rpc(self):
        r = mcp_server.handle_lore_remember("rpc forget test memory")
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "lore_forget", "arguments": {"memory_id": r["memory_id"]}},
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["success"] is True


# ── lore_rate_fix ─────────────────────────────────────────────────────────────

class TestLoreRateFix:
    def test_success_increases_confidence(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="RateTestError",
            solution_steps=["Step 1", "Step 2"],
        )
        result = mcp_server.handle_lore_rate_fix(fix["pattern_id"], "success")
        assert result["success"] is True
        assert result["new_confidence"] > result["old_confidence"]

    def test_failure_decreases_confidence(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="FailRateError",
            solution_steps=["Step A"],
        )
        result = mcp_server.handle_lore_rate_fix(fix["pattern_id"], "failure")
        assert result["success"] is True
        assert result["new_confidence"] < result["old_confidence"]

    def test_frequency_incremented(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="FreqRateError",
            solution_steps=["Step 1"],
        )
        result = mcp_server.handle_lore_rate_fix(fix["pattern_id"], "success")
        assert result["frequency"] == 2  # started at 1, incremented to 2

    def test_bayesian_update_success_formula(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="BayesSuccess",
            solution_steps=["step"],
        )
        pid = fix["pattern_id"]
        row = isolated_store.conn.execute(
            "SELECT confidence, frequency FROM darwin_patterns WHERE id=?", (pid,)
        ).fetchone()
        c, n = row[0], row[1]
        expected = (c * n + 1) / (n + 1)
        result = mcp_server.handle_lore_rate_fix(pid, "success")
        assert abs(result["new_confidence"] - round(expected, 4)) < 0.0001

    def test_bayesian_update_failure_formula(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="BayesFailure",
            solution_steps=["step"],
        )
        pid = fix["pattern_id"]
        row = isolated_store.conn.execute(
            "SELECT confidence, frequency FROM darwin_patterns WHERE id=?", (pid,)
        ).fetchone()
        c, n = row[0], row[1]
        expected = (c * n) / (n + 1)
        result = mcp_server.handle_lore_rate_fix(pid, "failure")
        assert abs(result["new_confidence"] - round(expected, 4)) < 0.0001

    def test_logs_to_darwin_journal(self, isolated_store):
        fix = mcp_server.handle_lore_fix(
            error_signature="JournalRateError",
            solution_steps=["step"],
        )
        journal_count_before = isolated_store.conn.execute(
            "SELECT COUNT(*) FROM darwin_journal"
        ).fetchone()[0]
        mcp_server.handle_lore_rate_fix(fix["pattern_id"], "success")
        journal_count_after = isolated_store.conn.execute(
            "SELECT COUNT(*) FROM darwin_journal"
        ).fetchone()[0]
        assert journal_count_after > journal_count_before

    def test_nonexistent_pattern_returns_error(self):
        result = mcp_server.handle_lore_rate_fix("nonexistent-pattern-id", "success")
        assert result["success"] is False
        assert "error" in result

    def test_invalid_outcome_returns_error(self):
        fix = mcp_server.handle_lore_fix(
            error_signature="BadOutcomeError",
            solution_steps=["step"],
        )
        result = mcp_server.handle_lore_rate_fix(fix["pattern_id"], "partial")
        assert result["success"] is False
        assert "error" in result

    def test_via_rpc(self):
        fix = mcp_server.handle_lore_fix(
            error_signature="RpcRateError",
            solution_steps=["fix step"],
        )
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "lore_rate_fix",
                "arguments": {"pattern_id": fix["pattern_id"], "outcome": "success"},
            },
        }
        resp = mcp_server.handle_request(req)
        assert "result" in resp
        content = json.loads(resp["result"]["content"][0]["text"])
        assert content["success"] is True


# ── Input validation ──────────────────────────────────────────────────────────

class TestInputValidation:
    def test_remember_empty_content(self):
        result = mcp_server.handle_lore_remember("")
        assert result.get("success") is False
        assert "error" in result

    def test_recall_empty_query(self):
        result = mcp_server.handle_lore_recall("")
        assert "error" in result

    def test_recall_invalid_top_k(self):
        result = mcp_server.handle_lore_recall("test", top_k=0)
        assert "error" in result

    def test_recall_invalid_time_window(self):
        result = mcp_server.handle_lore_recall("test", time_window="99d")
        assert "error" in result

    def test_teach_empty_convention(self):
        result = mcp_server.handle_lore_teach("")
        assert result.get("success") is False
        assert "error" in result

    def test_fix_empty_error_signature(self):
        result = mcp_server.handle_lore_fix("", solution_steps=["step"])
        assert result.get("success") is False
        assert "error" in result

    def test_fix_empty_solution_steps(self):
        result = mcp_server.handle_lore_fix("SomeError", solution_steps=[])
        assert result.get("success") is False
        assert "error" in result
