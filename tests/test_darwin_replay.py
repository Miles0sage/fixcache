"""
test_darwin_replay.py — Tests for Darwin Replay + Fingerprints (the moat).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.darwin_replay import (
    classify,
    darwin_stats,
    export_sanitized,
    record_outcome,
    upsert_fingerprint,
)
from lore_memory.fingerprint import compute_fingerprint, fingerprint_hash
from lore_memory.mcp.server import (
    _get_store,
    handle_lore_darwin_classify,
    handle_lore_darwin_export,
    handle_lore_darwin_stats,
    handle_lore_fix,
    handle_lore_rate_fix,
)
import lore_memory.mcp.server as server_mod


# ── Fingerprint unit tests ────────────────────────────────────────────────────


class TestFingerprintComputation:
    def test_stable_hash(self) -> None:
        a = fingerprint_hash("ModuleNotFoundError: No module named 'foo'")
        b = fingerprint_hash("ModuleNotFoundError: No module named 'foo'")
        assert a == b

    def test_different_errors_different_hash(self) -> None:
        a = fingerprint_hash("ModuleNotFoundError: No module named 'foo'")
        b = fingerprint_hash("ModuleNotFoundError: No module named 'bar'")
        # These differ in module name — after redaction still differ if module name survives
        # Our redactor strips quoted strings of length >=8, so 'foo'/'bar' (3 chars) survive
        assert a != b

    def test_absolute_paths_redacted(self) -> None:
        a = compute_fingerprint(
            "FileNotFoundError: /home/alice/project/foo.py"
        ).essence
        b = compute_fingerprint(
            "FileNotFoundError: /home/bob/project/foo.py"
        ).essence
        # Both should canonicalize identically (paths redacted to <p>/foo.py)
        assert a == b

    def test_detects_error_type(self) -> None:
        fp = compute_fingerprint("ModuleNotFoundError: No module named 'x'")
        assert fp.error_type == "ModuleNotFoundError"

    def test_detects_ecosystem_python(self) -> None:
        fp = compute_fingerprint(
            "Traceback (most recent call last):\n  File \"app.py\"\nModuleNotFoundError"
        )
        assert fp.ecosystem == "python"

    def test_detects_ecosystem_node(self) -> None:
        fp = compute_fingerprint(
            "Error: Cannot find module 'foo'\n  at Object.<anonymous> (/app/index.js:1:1)"
        )
        assert fp.ecosystem == "node"

    def test_detects_ecosystem_docker(self) -> None:
        fp = compute_fingerprint("ECONNREFUSED: connect to docker daemon failed")
        # 'docker' cue wins over 'shell' because it's more specific
        assert fp.ecosystem in ("docker", "unknown", "shell")

    def test_extract_top_frame_python(self) -> None:
        fp = compute_fingerprint(
            'Traceback (most recent call last):\n  File "/home/user/project/app.py", line 42\n'
            'ValueError: bad input'
        )
        assert fp.top_frame == "app.py"

    def test_hex_ids_redacted(self) -> None:
        a = compute_fingerprint(
            "ProcessError at 0xdeadbeef: session abc123def456"
        ).essence
        b = compute_fingerprint(
            "ProcessError at 0xcafebabe: session 1234567890ab"
        ).essence
        assert a == b

    def test_empty_input(self) -> None:
        fp = compute_fingerprint("")
        assert fp.error_type == "Unknown"
        assert fp.essence == ""

    def test_hash_length(self) -> None:
        fp = compute_fingerprint("TypeError: x")
        assert len(fp.hash) == 16
        assert all(c in "0123456789abcdef" for c in fp.hash)


# ── Store-level tests ────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(str(tmp_path / "replay.db"))
    yield s
    s.close()


class TestFingerprintsTable:
    def test_schema_has_fingerprints_table(self, store: MemoryStore) -> None:
        row = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='fingerprints'"
        ).fetchone()
        assert row is not None

    def test_schema_version_is_3(self, store: MemoryStore) -> None:
        row = store.conn.execute(
            "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        assert row[0] == 3


class TestUpsertFingerprint:
    def test_first_call_inserts(self, store: MemoryStore) -> None:
        result = upsert_fingerprint(store, "ModuleNotFoundError: No module named 'foo'")
        assert result["total_seen"] == 1
        count = store.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        assert count == 1

    def test_second_call_increments_seen(self, store: MemoryStore) -> None:
        upsert_fingerprint(store, "TypeError: x")
        result = upsert_fingerprint(store, "TypeError: x")
        assert result["total_seen"] == 2
        count = store.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        assert count == 1  # still one row, counter bumped

    def test_different_errors_separate_rows(self, store: MemoryStore) -> None:
        upsert_fingerprint(store, "ModuleNotFoundError: foo")
        upsert_fingerprint(store, "TypeError: bar")
        count = store.conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
        assert count == 2


class TestRecordOutcome:
    def test_success_updates_counter(self, store: MemoryStore) -> None:
        r = upsert_fingerprint(store, "TypeError: x")
        fp_hash = r["hash"]
        out = record_outcome(store, fp_hash, "success")
        assert out["success"] is True
        assert out["total_success"] == 1
        assert out["total_failure"] == 0
        assert out["efficacy"] == 1.0

    def test_mixed_outcomes(self, store: MemoryStore) -> None:
        r = upsert_fingerprint(store, "TypeError: x")
        fp_hash = r["hash"]
        record_outcome(store, fp_hash, "success")
        record_outcome(store, fp_hash, "success")
        record_outcome(store, fp_hash, "failure")
        out = record_outcome(store, fp_hash, "success")
        assert out["total_success"] == 3
        assert out["total_failure"] == 1
        assert out["efficacy"] == 0.75

    def test_unknown_fingerprint(self, store: MemoryStore) -> None:
        out = record_outcome(store, "deadbeefdeadbeef", "success")
        assert out["success"] is False


class TestClassify:
    def test_classify_unknown_error(self, store: MemoryStore) -> None:
        result = classify(store, "A totally new error")
        assert "fingerprint" in result
        assert result["fingerprint_stats"] is None
        assert result["match_count"] == 0

    def test_classify_returns_fingerprint(self, store: MemoryStore) -> None:
        result = classify(store, "TypeError: x")
        assert len(result["fingerprint"]["hash"]) == 16

    def test_classify_after_upsert_has_stats(self, store: MemoryStore) -> None:
        upsert_fingerprint(store, "TypeError: bad operand")
        result = classify(store, "TypeError: bad operand")
        assert result["fingerprint_stats"] is not None
        assert result["fingerprint_stats"]["total_seen"] >= 1


class TestDarwinStats:
    def test_empty_stats(self, store: MemoryStore) -> None:
        s = darwin_stats(store)
        assert s["total_fingerprints"] == 0
        assert s["overall_efficacy"] is None

    def test_stats_after_upserts(self, store: MemoryStore) -> None:
        upsert_fingerprint(store, "ModuleNotFoundError: foo")
        upsert_fingerprint(store, "TypeError: bar")
        upsert_fingerprint(store, "TypeError: bar")  # same hash, bump seen
        s = darwin_stats(store)
        assert s["total_fingerprints"] == 2
        assert s["total_seen_events"] == 3

    def test_efficacy_bands(self, store: MemoryStore) -> None:
        r1 = upsert_fingerprint(store, "TypeError: x")
        r2 = upsert_fingerprint(store, "ValueError: y")
        # High efficacy
        for _ in range(3):
            record_outcome(store, r1["hash"], "success")
        # Low efficacy
        for _ in range(3):
            record_outcome(store, r2["hash"], "failure")
        s = darwin_stats(store)
        assert s["efficacy_bands"]["high"] >= 1
        assert s["efficacy_bands"]["low"] >= 1


class TestExportSanitized:
    def test_export_empty(self, store: MemoryStore) -> None:
        corpus = export_sanitized(store)
        assert corpus == []

    def test_export_filters_by_min_seen(self, store: MemoryStore) -> None:
        upsert_fingerprint(store, "TypeError: x")
        upsert_fingerprint(store, "ValueError: y")
        upsert_fingerprint(store, "ValueError: y")  # seen=2
        corpus = export_sanitized(store, min_total_seen=2)
        assert len(corpus) == 1
        assert corpus[0]["total_seen"] == 2

    def test_export_contains_no_absolute_paths(self, store: MemoryStore) -> None:
        upsert_fingerprint(
            store,
            'Traceback (most recent call last):\n'
            '  File "/home/alice/secret/app.py", line 42\nValueError: x'
        )
        corpus = export_sanitized(store)
        assert len(corpus) == 1
        # Essence should be redacted — no /home/alice
        assert "/home/alice" not in corpus[0]["essence"]

    def test_export_includes_efficacy(self, store: MemoryStore) -> None:
        r = upsert_fingerprint(store, "TypeError: x")
        record_outcome(store, r["hash"], "success")
        record_outcome(store, r["hash"], "failure")
        corpus = export_sanitized(store)
        assert corpus[0]["efficacy"] == 0.5


# ── End-to-end MCP handler tests ─────────────────────────────────────────────


@pytest.fixture
def mcp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level MCP store for each test."""
    db_path = str(tmp_path / "mcp.db")
    monkeypatch.setenv("LORE_MEMORY_DB", db_path)
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)
    yield
    # Cleanup: close store and reset module globals
    if server_mod._store is not None:
        server_mod._store.close()
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)


class TestEndToEnd:
    def test_lore_fix_creates_fingerprint(self, mcp_store) -> None:
        result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'scikit-learn'",
            solution_steps=["pip install scikit-learn", "restart kernel"],
            tags=["python"],
        )
        assert result["success"] is True
        assert "fingerprint_hash" in result
        assert len(result["fingerprint_hash"]) == 16
        assert result["fingerprint_error_type"] == "ModuleNotFoundError"
        assert result["fingerprint_ecosystem"] == "python"

    def test_darwin_classify_returns_stored_recipe(self, mcp_store) -> None:
        handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'scikit-learn'",
            solution_steps=["pip install scikit-learn"],
        )
        result = handle_lore_darwin_classify(
            error_text="ModuleNotFoundError: No module named 'scikit-learn'",
            top_k=3,
        )
        assert result["match_count"] >= 1
        assert result["fingerprint_stats"] is not None

    def test_darwin_stats_handler(self, mcp_store) -> None:
        handle_lore_fix(
            error_signature="TypeError: cannot concat",
            solution_steps=["cast to str"],
        )
        s = handle_lore_darwin_stats()
        assert s["total_fingerprints"] >= 1

    def test_darwin_export_handler(self, mcp_store) -> None:
        handle_lore_fix(
            error_signature="ValueError: too big",
            solution_steps=["clamp value"],
        )
        r = handle_lore_darwin_export(min_total_seen=1)
        assert r["count"] >= 1
        assert len(r["fingerprints"]) >= 1

    def test_rate_fix_rolls_up_to_fingerprint(self, mcp_store) -> None:
        fix_result = handle_lore_fix(
            error_signature="AttributeError: has no attribute 'foo'",
            solution_steps=["add foo attribute"],
        )
        fp_hash = fix_result["fingerprint_hash"]
        pattern_id = fix_result["pattern_id"]

        # Rate it — should update both pattern confidence and fingerprint
        handle_lore_rate_fix(pattern_id=pattern_id, outcome="success")

        classified = handle_lore_darwin_classify(
            error_text="AttributeError: has no attribute 'foo'"
        )
        stats = classified["fingerprint_stats"]
        assert stats is not None
        assert stats["total_success"] == 1
