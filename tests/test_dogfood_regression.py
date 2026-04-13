"""
test_dogfood_regression.py — Regression tests for the two v0.4.0 release blockers.

Bug A: fix→watch fingerprint mismatch
    `lore fix "ModuleNotFoundError: ..."` stored a hash computed from the single-line
    error signature.  `lore watch --cmd pytest` captured a pytest FAILED summary line
    whose fingerprint hash differed, so the primary lookup never fired.

Bug B: `darwin report` subcommand missing
    watch output printed "Apply? Run: lore-memory darwin report <id> success" but
    that subcommand did not exist, so users could not close the Darwin feedback loop.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import lore_memory.mcp.server as server_mod
from lore_memory.cli import main as cli_main
from lore_memory.core.store import MemoryStore
from lore_memory.darwin_replay import classify, record_outcome, upsert_fingerprint
from lore_memory.fingerprint import compute_fingerprint
from lore_memory.mcp.server import handle_lore_fix
from lore_memory.watch import classify_and_format, run_command


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _fresh_store(tmp_path: Path, name: str = "regression.db") -> MemoryStore:
    return MemoryStore(str(tmp_path / name))


def _mount_mcp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> MemoryStore:
    db_path = str(tmp_path / name)
    monkeypatch.setenv("LORE_MEMORY_DB", db_path)
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)
    return server_mod._get_store()


def _teardown_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    if server_mod._store is not None:
        server_mod._store.close()
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)


# ── Bug A: fingerprint canonical form ────────────────────────────────────────


class TestBugAFingerprintCanonical:
    """
    Bug A root cause: _pick_final_line returned the full pytest FAILED summary
    line ("FAILED tests/foo.py - ModuleNotFoundError: ...") instead of stripping
    the prefix to yield the bare "ModuleNotFoundError: ..." form that `fix` stores.
    Both forms must now hash identically.
    """

    def test_fix_stores_canonical_fingerprint_hash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """fix must store the same hash that compute_fingerprint would produce."""
        _mount_mcp_store(tmp_path, monkeypatch, "canonical.db")
        result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
        )
        assert result["success"] is True
        stored_hash = result["fingerprint_hash"]
        # Must equal what compute_fingerprint produces for the same text
        expected = compute_fingerprint("ModuleNotFoundError: No module named 'fake_xyz'").hash
        assert stored_hash == expected, (
            f"fix stored hash {stored_hash!r} but compute_fingerprint gives {expected!r}"
        )
        _teardown_mcp(monkeypatch)

    def test_fix_then_watch_primary_lookup_fires_on_single_line_signature(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Teach a fix with a single-line error_signature.
        Classify a single-line error text.
        Primary fingerprint-hash lookup must surface the recipe (not just LIKE fallback).
        """
        store = _mount_mcp_store(tmp_path, monkeypatch, "single.db")
        handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
        )

        result = classify(store, "ModuleNotFoundError: No module named 'fake_xyz'", top_k=3)
        assert result["match_count"] >= 1, (
            "Primary fingerprint lookup must return >=1 candidate for single-line classify"
        )
        steps = result["candidates"][0]["solution_steps"]
        assert "pip install fake_xyz" in steps
        _teardown_mcp(monkeypatch)

    def test_fix_then_watch_primary_lookup_fires_on_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Teach a fix with a single-line signature.
        Classify a real multi-line Python traceback containing the same error.
        Fingerprint hash must match → primary lookup fires.
        """
        store = _mount_mcp_store(tmp_path, monkeypatch, "traceback.db")
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
        )
        stored_hash = fix_result["fingerprint_hash"]

        traceback_text = (
            "Traceback (most recent call last):\n"
            "  File \"<string>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'fake_xyz'"
        )
        fp = compute_fingerprint(traceback_text)
        assert fp.hash == stored_hash, (
            f"Traceback fingerprint {fp.hash} must match stored fix hash {stored_hash}"
        )

        result = classify(store, traceback_text, top_k=3)
        assert result["match_count"] >= 1, (
            f"Primary lookup must fire for traceback. candidates={result['candidates']}"
        )
        steps = result["candidates"][0]["solution_steps"]
        assert "pip install fake_xyz" in steps
        _teardown_mcp(monkeypatch)

    def test_pytest_summary_line_hashes_same_as_single_line(self) -> None:
        """
        The core fingerprint invariant: a pytest FAILED summary line must hash
        identically to the bare error-type line it embeds.

        This is the exact scenario the dogfood agent hit:
          watch captures: "FAILED tests/foo.py::fn - ModuleNotFoundError: No module named 'x'"
          fix stored:     "ModuleNotFoundError: No module named 'x'"
        """
        single = compute_fingerprint("ModuleNotFoundError: No module named 'fake_xyz'")
        pytest_summary = compute_fingerprint(
            "============================= test session starts ==============================\n"
            "FAILED tests/test_foo.py::test_fn - ModuleNotFoundError: No module named 'fake_xyz'\n"
            "============================== 1 failed in 0.12s =============================="
        )
        assert single.hash == pytest_summary.hash, (
            f"Pytest summary hash {pytest_summary.hash!r} != single-line hash {single.hash!r}. "
            "Bug A not fixed: fix→watch primary lookup will still miss on pytest output."
        )

    def test_full_pytest_output_with_traceback_hashes_same_as_single_line(self) -> None:
        """Full pytest output (traceback + summary) must also hash to the single-line form."""
        single = compute_fingerprint("ModuleNotFoundError: No module named 'fake_xyz'")
        full_pytest = compute_fingerprint(
            "============================= test session starts ==============================\n"
            "collected 1 item\n"
            "\n"
            "tests/test_foo.py F\n"
            "\n"
            "=================================== FAILURES ===================================\n"
            "\n"
            "    def test_fn():\n"
            ">       import fake_xyz\n"
            "E       ModuleNotFoundError: No module named 'fake_xyz'\n"
            "\n"
            "tests/test_foo.py:2: ModuleNotFoundError\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_foo.py::test_fn - ModuleNotFoundError: No module named 'fake_xyz'\n"
            "============================== 1 failed in 0.12s =============================="
        )
        assert single.hash == full_pytest.hash, (
            f"Full pytest output hash {full_pytest.hash!r} != single-line hash {single.hash!r}"
        )


# ── Bug B: darwin report CLI subcommand ───────────────────────────────────────


class TestBugBDarwinReport:
    """Bug B: `lore-memory darwin report <pattern_id> success|failure` must exist."""

    def test_darwin_report_success_increments_successes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _mount_mcp_store(tmp_path, monkeypatch, "report_success.db")
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
        )
        fp_hash = fix_result["fingerprint_hash"]
        pattern_id = fix_result["pattern_id"]

        # Verify fp row exists
        row_before = store.conn.execute(
            "SELECT total_success FROM fingerprints WHERE hash=?", (fp_hash,)
        ).fetchone()
        assert row_before is not None
        before = row_before[0]

        # Run darwin report via CLI
        db_path = str(tmp_path / "report_success.db")
        rc = cli_main(["--db", db_path, "darwin", "report", pattern_id, "success"])
        assert rc == 0, f"darwin report returned non-zero exit code: {rc}"

        row_after = store.conn.execute(
            "SELECT total_success FROM fingerprints WHERE hash=?", (fp_hash,)
        ).fetchone()
        assert row_after[0] == before + 1, (
            f"total_success should increment from {before} to {before+1}, got {row_after[0]}"
        )
        _teardown_mcp(monkeypatch)

    def test_darwin_report_failure_increments_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _mount_mcp_store(tmp_path, monkeypatch, "report_failure.db")
        fix_result = handle_lore_fix(
            error_signature="AttributeError: 'NoneType' object has no attribute 'split'",
            solution_steps=["check for None before calling .split()"],
        )
        fp_hash = fix_result["fingerprint_hash"]
        pattern_id = fix_result["pattern_id"]

        row_before = store.conn.execute(
            "SELECT total_failure FROM fingerprints WHERE hash=?", (fp_hash,)
        ).fetchone()
        before = row_before[0]

        db_path = str(tmp_path / "report_failure.db")
        rc = cli_main(["--db", db_path, "darwin", "report", pattern_id, "failure"])
        assert rc == 0

        row_after = store.conn.execute(
            "SELECT total_failure FROM fingerprints WHERE hash=?", (fp_hash,)
        ).fetchone()
        assert row_after[0] == before + 1, (
            f"total_failure should go from {before} to {before+1}, got {row_after[0]}"
        )
        _teardown_mcp(monkeypatch)

    def test_darwin_report_nonexistent_pattern_returns_error_not_crash(
        self, tmp_path: Path
    ) -> None:
        """darwin report with unknown pattern_id must return exit code 1, not crash."""
        db_path = str(tmp_path / "empty.db")
        # Create the DB
        store = MemoryStore(db_path)
        store.close()

        rc = cli_main(["--db", db_path, "darwin", "report", "nonexistent-uuid-1234", "success"])
        assert rc == 1, (
            f"Unknown pattern_id should return exit code 1, got {rc}"
        )

    def test_watch_output_advertises_real_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The text printed by watch must advertise a subcommand that actually exists.
        Specifically: 'lore-memory darwin report <id> success' must parse without error.
        """
        store = _mount_mcp_store(tmp_path, monkeypatch, "advertise.db")
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
        )
        pattern_id = fix_result["pattern_id"]

        from lore_memory.watch import format_suggestions, classify_and_format
        stderr = "ModuleNotFoundError: No module named 'fake_xyz'"
        watch_result = classify_and_format(store, stderr)
        output = format_suggestions(watch_result)

        # The advertised command must mention darwin report
        assert "darwin report" in output, (
            f"watch output must advertise 'darwin report'. Got:\n{output}"
        )
        # The pattern_id advertised must be parseable by the CLI
        db_path = str(tmp_path / "advertise.db")
        rc = cli_main(["--db", db_path, "darwin", "report", pattern_id, "success"])
        assert rc == 0, (
            f"CLI returned {rc} for 'darwin report {pattern_id} success'. "
            "watch advertises a command that doesn't work!"
        )
        _teardown_mcp(monkeypatch)


# ── End-to-end kill-shot redux ────────────────────────────────────────────────


class TestFixWatchEndToEndRealSubprocess:
    """
    THE KILLSHOT REDUX: real fix → real watch → recipe surfaces.

    This test is the definitive regression guard for Bug A. It:
    1. Types `fix "ModuleNotFoundError: No module named 'fake_xyz'" --steps "pip install fake_xyz"`
    2. Runs a REAL subprocess: python3 -c "import fake_xyz"
    3. Classifies the real stderr via classify_and_format
    4. Asserts the recipe surfaces (primary hash lookup, not LIKE fallback)
    """

    def test_fix_watch_end_to_end_real_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _mount_mcp_store(tmp_path, monkeypatch, "e2e.db")

        # Step 1: teach fix via handle_lore_fix (same as `lore fix` CLI)
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fake_xyz'",
            solution_steps=["pip install fake_xyz"],
            tags=["python", "import"],
        )
        assert fix_result["success"] is True, f"fix failed: {fix_result}"
        stored_hash = fix_result["fingerprint_hash"]
        pattern_id = fix_result["pattern_id"]
        print(f"\n[KillshotRedux] Taught fix. hash={stored_hash}, pattern_id={pattern_id}")

        # Step 2: run REAL subprocess that fails with ModuleNotFoundError
        proc = subprocess.run(
            [sys.executable, "-c", "import fake_xyz"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "subprocess must fail"
        real_stderr = proc.stderr
        assert "ModuleNotFoundError" in real_stderr or "No module named" in real_stderr, (
            f"Expected ModuleNotFoundError in stderr: {real_stderr!r}"
        )
        print(f"[KillshotRedux] Real stderr:\n{real_stderr.strip()}")

        # Step 3: verify fingerprint of real stderr matches stored fix hash
        watch_fp = compute_fingerprint(real_stderr).hash
        assert watch_fp == stored_hash, (
            f"BUG A STILL PRESENT: watch fingerprint {watch_fp!r} != "
            f"stored fix hash {stored_hash!r}.\n"
            f"Real stderr: {real_stderr!r}"
        )
        print(f"[KillshotRedux] Fingerprint match: {watch_fp} == {stored_hash}")

        # Step 4: classify_and_format must surface the recipe
        result = classify_and_format(store, real_stderr)
        assert result.fingerprint_hash == stored_hash, (
            f"classify_and_format returned hash {result.fingerprint_hash!r} "
            f"but stored hash is {stored_hash!r}"
        )
        assert len(result.suggestions) >= 1, (
            f"Recipe must surface after fix→watch. suggestions={result.suggestions}\n"
            f"Real stderr: {real_stderr!r}\n"
            f"watch fp: {result.fingerprint_hash!r}, stored: {stored_hash!r}"
        )
        top = result.suggestions[0]
        assert "pip install fake_xyz" in top["solution_steps"], (
            f"Taught step must appear. Got: {top['solution_steps']}"
        )
        print(f"[KillshotRedux] Recipe surfaced: '{top['solution_steps'][0]}'")

        # Step 5: darwin report via CLI closes the loop
        db_path = str(tmp_path / "e2e.db")
        rc = cli_main(["--db", db_path, "darwin", "report", pattern_id, "success"])
        assert rc == 0, f"darwin report returned {rc}"

        # Step 6: verify efficacy recorded
        row = store.conn.execute(
            "SELECT total_success FROM fingerprints WHERE hash=?", (stored_hash,)
        ).fetchone()
        assert row is not None
        assert row[0] >= 1, f"Expected >=1 success after darwin report, got {row[0]}"
        print(f"[KillshotRedux] Efficacy loop closed. total_success={row[0]}")

        _teardown_mcp(monkeypatch)


# ── Ecosystem-mismatch fix (httpx dogfood re-verification) ───────────────────


class TestEcosystemMismatchFix:
    """Regression tests for the httpx dogfood finding: bare fix signatures
    must produce the same ecosystem (and thus same hash) as the full traceback
    watch sees for the same error class."""

    def test_bare_syntax_error_infers_python_ecosystem(self) -> None:
        fp = compute_fingerprint("SyntaxError: '(' was never closed")
        assert fp.ecosystem == "python"

    def test_bare_module_not_found_infers_python(self) -> None:
        fp = compute_fingerprint("ModuleNotFoundError: No module named 'requests'")
        assert fp.ecosystem == "python"

    def test_bare_attribute_error_infers_python(self) -> None:
        fp = compute_fingerprint("AttributeError: 'NoneType' object has no attribute 'split'")
        assert fp.ecosystem == "python"

    def test_bare_type_error_infers_python(self) -> None:
        fp = compute_fingerprint("TypeError: unsupported operand type(s) for +: 'int' and 'str'")
        assert fp.ecosystem == "python"

    def test_bare_signature_and_traceback_produce_same_hash_syntax_error(self) -> None:
        bare = "SyntaxError: '(' was never closed"
        traceback = (
            "Traceback (most recent call last):\n"
            "  File \"/path/to/file.py\", line 10\n"
            "    def foo(\n"
            "           ^\n"
            "SyntaxError: '(' was never closed"
        )
        assert compute_fingerprint(bare).hash == compute_fingerprint(traceback).hash

    def test_bare_signature_and_traceback_produce_same_hash_module_not_found(self) -> None:
        bare = "ModuleNotFoundError: No module named 'foo'"
        traceback = (
            "Traceback (most recent call last):\n"
            "  File \"/path/to/file.py\", line 1, in <module>\n"
            "    import foo\n"
            "ModuleNotFoundError: No module named 'foo'"
        )
        assert compute_fingerprint(bare).hash == compute_fingerprint(traceback).hash

    def test_bare_attribute_error_and_traceback_collapse(self) -> None:
        bare = "AttributeError: 'NoneType' object has no attribute 'split'"
        traceback = (
            "Traceback (most recent call last):\n"
            "  File \"/app/x.py\", line 5, in run\n"
            "    val.split(',')\n"
            "AttributeError: 'NoneType' object has no attribute 'split'"
        )
        assert compute_fingerprint(bare).hash == compute_fingerprint(traceback).hash

    def test_bare_non_python_error_stays_unknown(self) -> None:
        """A truly unknown error type (no entry in _ERROR_TYPE_TO_ECOSYSTEM)
        should still return 'unknown' ecosystem, not force-infer."""
        fp = compute_fingerprint("CustomDomainError: widget fizzled")
        assert fp.ecosystem == "unknown"

    def test_node_cannot_find_module_still_routes_to_node(self) -> None:
        """The text-level cue must still win over error-type inference.
        Node's 'Cannot find module' error includes an `at ... (file.js:N:M)`
        stack frame that triggers the node text cue before any type inference."""
        fp = compute_fingerprint(
            "Error: Cannot find module 'express'\n"
            "    at Function.Module._resolveFilename (node:internal/modules/cjs/loader:1142:15)"
        )
        assert fp.ecosystem == "node"
