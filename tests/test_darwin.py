"""
tests/test_darwin.py — Tests for the Darwin Evolution Engine.

Covers:
  - Bayesian confidence updates (success raises, failure lowers)
  - evolve_patterns finds demotions and promotions
  - consolidate prunes old unused memories
  - Full loop: fix → match → report_outcome → evolve → confidence changes
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.darwin import (
    consolidate,
    evolve_patterns,
    log_outcome,
    update_confidence,
)
from lore_memory.mcp import server as mcp_server
from lore_memory.layers.identity import IdentityLayer


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Each test gets a fresh in-memory store via monkeypatching the server module."""
    s = MemoryStore(":memory:")
    identity = IdentityLayer(s)
    monkeypatch.setattr(mcp_server, "_store", s)
    monkeypatch.setattr(mcp_server, "_identity", identity)
    yield s
    s.close()
    monkeypatch.setattr(mcp_server, "_store", None)
    monkeypatch.setattr(mcp_server, "_identity", None)


def _make_pattern(store: MemoryStore, sig: str = "ImportError", confidence: float = 0.5) -> str:
    """Insert a darwin_pattern directly and return its ID."""
    pat_id = str(uuid.uuid4())
    now = time.time()
    store.conn.execute(
        """
        INSERT INTO darwin_patterns
            (id, pattern_type, description, rule, frequency, confidence, created_at, last_triggered)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (pat_id, "error_recipe", f"Fix for: {sig}", json.dumps(["step1"]), 1, confidence, now, now),
    )
    store.conn.commit()
    return pat_id


# ── log_outcome ───────────────────────────────────────────────────────────────

class TestLogOutcome:
    def test_records_success(self, store):
        pat_id = _make_pattern(store)
        journal_id = log_outcome(store, pat_id, "success")
        row = store.conn.execute(
            "SELECT outcome, result_ids FROM darwin_journal WHERE id=?", (journal_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "success"
        assert row[1] == pat_id

    def test_records_failure(self, store):
        pat_id = _make_pattern(store)
        journal_id = log_outcome(store, pat_id, "failure", context="traceback here")
        row = store.conn.execute(
            "SELECT outcome, correction FROM darwin_journal WHERE id=?", (journal_id,)
        ).fetchone()
        assert row[0] == "failure"
        assert row[1] == "traceback here"

    def test_invalid_outcome_defaults_to_partial(self, store):
        pat_id = _make_pattern(store)
        journal_id = log_outcome(store, pat_id, "bogus_outcome")
        row = store.conn.execute(
            "SELECT outcome FROM darwin_journal WHERE id=?", (journal_id,)
        ).fetchone()
        assert row[0] == "partial"

    def test_context_stored_in_metadata(self, store):
        pat_id = _make_pattern(store)
        ctx = "Python 3.11, missing module"
        journal_id = log_outcome(store, pat_id, "failure", context=ctx)
        row = store.conn.execute(
            "SELECT metadata FROM darwin_journal WHERE id=?", (journal_id,)
        ).fetchone()
        meta = json.loads(row[0])
        assert meta["context"] == ctx


# ── update_confidence ─────────────────────────────────────────────────────────

class TestUpdateConfidence:
    def test_success_raises_confidence(self, store):
        pat_id = _make_pattern(store, confidence=0.5)
        result = update_confidence(store, pat_id, "success")
        assert result["success"] is True
        assert result["new_confidence"] > result["old_confidence"]
        assert result["new_confidence"] > 0.5

    def test_failure_lowers_confidence(self, store):
        pat_id = _make_pattern(store, confidence=0.8)
        result = update_confidence(store, pat_id, "failure")
        assert result["success"] is True
        assert result["new_confidence"] < result["old_confidence"]
        assert result["new_confidence"] < 0.8

    def test_partial_neutral_update(self, store):
        pat_id = _make_pattern(store, confidence=0.5)
        result = update_confidence(store, pat_id, "partial")
        assert result["success"] is True
        # partial nudges very slightly; confidence stays near 0.5
        assert 0.3 < result["new_confidence"] < 0.7

    def test_multiple_successes_compound(self, store):
        pat_id = _make_pattern(store, confidence=0.5)
        for _ in range(5):
            update_confidence(store, pat_id, "success")
        row = store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        assert row[0] > 0.7  # should be substantially higher after 5 successes

    def test_multiple_failures_compound(self, store):
        pat_id = _make_pattern(store, confidence=0.8)
        for _ in range(5):
            update_confidence(store, pat_id, "failure")
        row = store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        assert row[0] < 0.4  # should be substantially lower after 5 failures

    def test_beta_params_stored_in_metadata(self, store):
        pat_id = _make_pattern(store, confidence=0.5)
        update_confidence(store, pat_id, "success")
        row = store.conn.execute(
            "SELECT metadata FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        meta = json.loads(row[0])
        assert "beta_alpha" in meta
        assert "beta_beta" in meta
        assert meta["beta_alpha"] > meta["beta_beta"]  # more successes than failures

    def test_frequency_incremented(self, store):
        pat_id = _make_pattern(store, confidence=0.5)
        result = update_confidence(store, pat_id, "success")
        assert result["frequency"] == 2  # started at 1

    def test_unknown_pattern_returns_error(self, store):
        result = update_confidence(store, "nonexistent-id", "success")
        assert result.get("success") is False
        assert "not found" in result["error"].lower()

    def test_confidence_stays_in_valid_range(self, store):
        pat_id = _make_pattern(store, confidence=0.99)
        for _ in range(20):
            update_confidence(store, pat_id, "success")
        row = store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        assert 0.0 <= row[0] <= 1.0


# ── evolve_patterns ───────────────────────────────────────────────────────────

class TestEvolvePatterns:
    def test_demotes_pattern_with_many_failures(self, store):
        pat_id = _make_pattern(store, confidence=0.8)
        # Insert 3 failure journal entries pointing at this pattern
        for _ in range(3):
            jid = str(uuid.uuid4())
            store.conn.execute(
                """INSERT INTO darwin_journal (id, query, result_ids, outcome, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (jid, f"outcome:{pat_id}", pat_id, "failure", time.time(), "{}"),
            )
        store.conn.commit()

        report = evolve_patterns(store, min_failures=3)
        assert len(report["demoted"]) >= 1
        demoted_ids = [d["pattern_id"] for d in report["demoted"]]
        assert pat_id in demoted_ids

        # Confidence should be below demotion threshold
        row = store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        assert row[0] <= 0.2

    def test_does_not_demote_below_threshold_failures(self, store):
        pat_id = _make_pattern(store, confidence=0.8)
        # Only 2 failures — below min_failures=3
        for _ in range(2):
            jid = str(uuid.uuid4())
            store.conn.execute(
                """INSERT INTO darwin_journal (id, query, result_ids, outcome, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (jid, f"outcome:{pat_id}", pat_id, "failure", time.time(), "{}"),
            )
        store.conn.commit()

        report = evolve_patterns(store, min_failures=3)
        demoted_ids = [d["pattern_id"] for d in report["demoted"]]
        assert pat_id not in demoted_ids

    def test_promotes_best_of_competing_patterns(self, store):
        sig = "ConnectionError"
        # Pattern A: high confidence
        pid_a = _make_pattern(store, sig=sig, confidence=0.8)
        # Pattern B: low confidence
        pid_b = _make_pattern(store, sig=sig, confidence=0.3)

        # Give pid_a a success entry so it qualifies for promotion
        jid = str(uuid.uuid4())
        store.conn.execute(
            """INSERT INTO darwin_journal (id, query, result_ids, outcome, timestamp, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (jid, f"outcome:{pid_a}", pid_a, "success", time.time(), "{}"),
        )
        store.conn.commit()

        report = evolve_patterns(store, min_failures=3)
        promoted_ids = [p["pattern_id"] for p in report["promoted"]]
        assert pid_a in promoted_ids

        # Promoted pattern confidence should be higher than before
        row = store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pid_a,)
        ).fetchone()
        assert row[0] > 0.8

    def test_flags_unmatched_recurring_errors(self, store):
        error_text = "ModuleNotFoundError: No module named 'foobar'"
        # 3 failure journal entries with no matching pattern
        for _ in range(3):
            jid = str(uuid.uuid4())
            store.conn.execute(
                """INSERT INTO darwin_journal (id, query, result_ids, outcome, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (jid, error_text, None, "failure", time.time(), "{}"),
            )
        store.conn.commit()

        report = evolve_patterns(store, min_failures=3)
        assert any(error_text[:50] in nr for nr in report["needs_recipe"])

    def test_returns_correct_report_structure(self, store):
        report = evolve_patterns(store)
        assert "demoted" in report
        assert "promoted" in report
        assert "needs_recipe" in report
        assert isinstance(report["demoted"], list)
        assert isinstance(report["promoted"], list)
        assert isinstance(report["needs_recipe"], list)

    def test_empty_store_returns_empty_report(self, store):
        report = evolve_patterns(store)
        assert report == {"demoted": [], "promoted": [], "needs_recipe": []}


# ── consolidate ───────────────────────────────────────────────────────────────

class TestConsolidate:
    def test_decays_old_unused_memories(self, store):
        # Insert a memory with access_count=0, created 40 days ago
        mid = str(uuid.uuid4())
        old_time = time.time() - (40 * 86400)
        store.conn.execute(
            """INSERT INTO memories (id, content, memory_type, created_at, access_count, decay_score, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, "old unused memory", "fact", old_time, 0, 1.0, "{}"),
        )
        store.conn.commit()

        result = consolidate(store, max_age_days=30)
        assert result["decayed"] >= 1

        row = store.conn.execute(
            "SELECT decay_score FROM memories WHERE id=?", (mid,)
        ).fetchone()
        assert row[0] < 1.0  # halved

    def test_does_not_decay_recently_accessed_memories(self, store):
        mid = str(uuid.uuid4())
        old_time = time.time() - (40 * 86400)
        store.conn.execute(
            """INSERT INTO memories (id, content, memory_type, created_at, access_count, decay_score, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (mid, "accessed memory", "fact", old_time, 5, 1.0, "{}"),  # access_count=5
        )
        store.conn.commit()

        result = consolidate(store, max_age_days=30)

        row = store.conn.execute(
            "SELECT decay_score FROM memories WHERE id=?", (mid,)
        ).fetchone()
        assert row[0] == 1.0  # unchanged — it was accessed

    def test_deduplicates_identical_content(self, store):
        content = "identical content for dedup test"
        meta_high = json.dumps({"trust_score": 0.9})
        meta_low = json.dumps({"trust_score": 0.3})

        mid_high = str(uuid.uuid4())
        mid_low = str(uuid.uuid4())
        now = time.time()
        store.conn.execute(
            """INSERT INTO memories (id, content, memory_type, created_at, decay_score, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mid_high, content, "fact", now, 1.0, meta_high),
        )
        store.conn.execute(
            """INSERT INTO memories (id, content, memory_type, created_at, decay_score, metadata)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mid_low, content, "fact", now - 1, 1.0, meta_low),
        )
        store.conn.commit()

        result = consolidate(store)
        assert result["deduped"] >= 1

        # High-trust keeper should still be alive
        row_high = store.conn.execute(
            "SELECT decay_score FROM memories WHERE id=?", (mid_high,)
        ).fetchone()
        assert row_high[0] > 0

        # Low-trust duplicate should be tombstoned
        row_low = store.conn.execute(
            "SELECT decay_score FROM memories WHERE id=?", (mid_low,)
        ).fetchone()
        assert row_low[0] == 0.0

    def test_deprecates_low_confidence_stale_patterns(self, store):
        pat_id = str(uuid.uuid4())
        old_time = time.time() - (40 * 86400)
        store.conn.execute(
            """INSERT INTO darwin_patterns
               (id, pattern_type, description, rule, frequency, confidence, created_at, last_triggered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pat_id, "error_recipe", "Fix for: OldError", "[]", 1, 0.05, old_time, old_time),
        )
        store.conn.commit()

        result = consolidate(store, max_age_days=30)
        assert result["deprecated"] >= 1

        row = store.conn.execute(
            "SELECT metadata FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        meta = json.loads(row[0]) if row[0] else {}
        assert meta.get("deprecated") is True

    def test_does_not_deprecate_recent_patterns(self, store):
        pat_id = str(uuid.uuid4())
        now = time.time()
        store.conn.execute(
            """INSERT INTO darwin_patterns
               (id, pattern_type, description, rule, frequency, confidence, created_at, last_triggered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (pat_id, "error_recipe", "Fix for: RecentError", "[]", 1, 0.05, now, now),
        )
        store.conn.commit()

        result = consolidate(store, max_age_days=30)

        row = store.conn.execute(
            "SELECT metadata FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        meta = json.loads(row[0]) if row[0] else {}
        assert not meta.get("deprecated")

    def test_returns_correct_stats_structure(self, store):
        result = consolidate(store)
        assert "decayed" in result
        assert "deduped" in result
        assert "deprecated" in result
        assert isinstance(result["decayed"], int)
        assert isinstance(result["deduped"], int)
        assert isinstance(result["deprecated"], int)


# ── MCP tool: lore_report_outcome ─────────────────────────────────────────────

class TestLoreReportOutcome:
    def test_success_updates_confidence_up(self, isolated_store):
        fix_result = mcp_server.handle_lore_fix(
            error_signature="TypeError: unsupported operand",
            solution_steps=["Check types before operation"],
        )
        pat_id = fix_result["pattern_id"]
        old_conf = fix_result["confidence"]

        result = mcp_server.handle_lore_report_outcome(pat_id, "success")
        assert result["success"] is True
        assert result["new_confidence"] > old_conf
        assert result["journal_id"] is not None

    def test_failure_updates_confidence_down(self, isolated_store):
        fix_result = mcp_server.handle_lore_fix(
            error_signature="KeyError in dict access",
            solution_steps=["Use .get() with default"],
        )
        pat_id = fix_result["pattern_id"]

        result = mcp_server.handle_lore_report_outcome(pat_id, "failure")
        assert result["success"] is True
        assert result["new_confidence"] < fix_result["confidence"]

    def test_partial_outcome_accepted(self, isolated_store):
        fix_result = mcp_server.handle_lore_fix(
            error_signature="RecursionError",
            solution_steps=["Increase recursion limit"],
        )
        result = mcp_server.handle_lore_report_outcome(
            fix_result["pattern_id"], "partial", context="Partial fix applied"
        )
        assert result["success"] is True

    def test_invalid_pattern_id_returns_error(self):
        result = mcp_server.handle_lore_report_outcome("nonexistent-id", "success")
        assert result["success"] is False

    def test_invalid_outcome_returns_error(self, isolated_store):
        fix_result = mcp_server.handle_lore_fix(
            error_signature="SyntaxError",
            solution_steps=["Fix syntax"],
        )
        result = mcp_server.handle_lore_report_outcome(
            fix_result["pattern_id"], "invalid_outcome"
        )
        assert result["success"] is False

    def test_context_stored_in_journal(self, isolated_store):
        fix_result = mcp_server.handle_lore_fix(
            error_signature="AttributeError: NoneType",
            solution_steps=["Add None check"],
        )
        pat_id = fix_result["pattern_id"]
        ctx = "Happened in production with Python 3.12"
        mcp_server.handle_lore_report_outcome(pat_id, "failure", context=ctx)

        row = isolated_store.conn.execute(
            "SELECT correction FROM darwin_journal WHERE result_ids=? AND outcome='failure' ORDER BY timestamp DESC LIMIT 1",
            (pat_id,),
        ).fetchone()
        assert row is not None
        assert ctx in row[0]


# ── MCP tool: lore_evolve ─────────────────────────────────────────────────────

class TestLoreEvolve:
    def test_returns_combined_report(self, isolated_store):
        result = mcp_server.handle_lore_evolve()
        assert "evolution" in result
        assert "consolidation" in result
        assert "summary" in result

    def test_summary_has_correct_keys(self, isolated_store):
        result = mcp_server.handle_lore_evolve()
        summary = result["summary"]
        assert "patterns_demoted" in summary
        assert "patterns_promoted" in summary
        assert "errors_needing_recipe" in summary
        assert "memories_decayed" in summary
        assert "memories_deduped" in summary
        assert "patterns_deprecated" in summary

    def test_demotes_via_evolve_tool(self, isolated_store):
        # Create a pattern and give it 3 failures via lore_fix + report_outcome
        fix_result = mcp_server.handle_lore_fix(
            error_signature="NameError: name 'x' is not defined",
            solution_steps=["Define x before use"],
        )
        pat_id = fix_result["pattern_id"]

        # Manually insert 3 failure journal entries
        import uuid as _uuid
        for _ in range(3):
            jid = str(_uuid.uuid4())
            isolated_store.conn.execute(
                """INSERT INTO darwin_journal (id, query, result_ids, outcome, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (jid, f"outcome:{pat_id}", pat_id, "failure", time.time(), "{}"),
            )
        isolated_store.conn.commit()

        result = mcp_server.handle_lore_evolve(min_failures=3)
        assert result["summary"]["patterns_demoted"] >= 1

    def test_invalid_params_use_defaults(self, isolated_store):
        # Should not error even with bad params
        result = mcp_server.handle_lore_evolve(min_failures=-1, max_age_days=0)
        assert "summary" in result


# ── Full feedback loop integration ────────────────────────────────────────────

class TestDarwinFeedbackLoop:
    def test_full_loop_fix_match_report_evolve(self, isolated_store):
        """
        Complete Darwin loop:
        1. Store a fix recipe (lore_fix)
        2. Match it against an error (lore_match_procedure)
        3. Report outcome (lore_report_outcome)
        4. Run evolution (lore_evolve)
        5. Verify confidence changed
        """
        # Step 1: Store a fix
        fix_result = mcp_server.handle_lore_fix(
            error_signature="ImportError: cannot import name",
            solution_steps=[
                "Check the module path",
                "Verify the symbol exists in the module",
                "Check for circular imports",
            ],
        )
        assert fix_result["success"] is True
        pat_id = fix_result["pattern_id"]
        initial_confidence = fix_result["confidence"]

        # Step 2: Match it
        match_result = mcp_server.handle_lore_match_procedure(
            "ImportError: cannot import name 'foo' from 'bar'"
        )
        assert match_result["found"] is True
        assert match_result["pattern_id"] == pat_id
        assert "lore_report_outcome" in match_result["hint"]

        # Step 3: Report success
        report_result = mcp_server.handle_lore_report_outcome(
            pat_id, "success", context="Fixed by checking circular imports"
        )
        assert report_result["success"] is True
        assert report_result["new_confidence"] > initial_confidence

        # Step 4: Run evolution
        evolve_result = mcp_server.handle_lore_evolve()
        assert "summary" in evolve_result

        # Step 5: Verify final confidence in DB
        row = isolated_store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        assert row[0] > initial_confidence

    def test_repeated_failures_trigger_demotion_via_evolve(self, isolated_store):
        """
        Repeated failures should push a pattern below demotion threshold via evolve.
        """
        fix_result = mcp_server.handle_lore_fix(
            error_signature="ZeroDivisionError",
            solution_steps=["Check divisor before divide"],
        )
        pat_id = fix_result["pattern_id"]

        # Report 3 failures
        for _ in range(3):
            mcp_server.handle_lore_report_outcome(pat_id, "failure")

        evolve_result = mcp_server.handle_lore_evolve(min_failures=3)

        row = isolated_store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        # After 3 failures + demotion, confidence should be well below 0.5
        assert row[0] < 0.5

    def test_confidence_converges_with_mixed_outcomes(self, isolated_store):
        """
        Mixed success/failure outcomes should converge to a stable confidence.
        """
        fix_result = mcp_server.handle_lore_fix(
            error_signature="TimeoutError",
            solution_steps=["Increase timeout", "Retry with backoff"],
        )
        pat_id = fix_result["pattern_id"]

        # 4 successes, 2 failures → should settle around 0.6-0.8
        for _ in range(4):
            update_confidence(isolated_store, pat_id, "success")
        for _ in range(2):
            update_confidence(isolated_store, pat_id, "failure")

        row = isolated_store.conn.execute(
            "SELECT confidence FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        final_conf = row[0]
        assert 0.5 < final_conf < 0.9  # biased toward success but not extreme

    def test_lore_rate_fix_uses_beta_distribution(self, isolated_store):
        """
        lore_rate_fix should now use update_confidence (Beta distribution),
        producing alpha/beta metadata in the pattern.
        """
        fix_result = mcp_server.handle_lore_fix(
            error_signature="ValueError: invalid literal",
            solution_steps=["Validate input before conversion"],
        )
        pat_id = fix_result["pattern_id"]

        rate_result = mcp_server.handle_lore_rate_fix(pat_id, "success")
        assert rate_result["success"] is True
        assert rate_result["new_confidence"] > rate_result["old_confidence"]

        # Verify metadata has Beta params stored
        row = isolated_store.conn.execute(
            "SELECT metadata FROM darwin_patterns WHERE id=?", (pat_id,)
        ).fetchone()
        meta = json.loads(row[0]) if row[0] else {}
        assert "beta_alpha" in meta
        assert "beta_beta" in meta
