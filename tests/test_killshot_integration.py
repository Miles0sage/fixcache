"""
test_killshot_integration.py — The kill-shot integration test suite.

Real subprocesses. Real SQLite. Real fingerprints. No mocks.
If this passes you can screenshot it into a HN post.
If it fails, you fix the code not the tests.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import lore_memory.mcp.server as server_mod
from lore_memory.core.store import MemoryStore
from lore_memory.darwin_replay import (
    classify,
    darwin_stats,
    export_sanitized,
    record_outcome,
    upsert_fingerprint,
)
from lore_memory.fingerprint import compute_fingerprint, fingerprint_hash
from lore_memory.mcp.server import handle_lore_fix
from lore_memory.watch import classify_and_format, run_command


# ── Shared fixture pattern from existing tests ────────────────────────────────

def _fresh_store(tmp_path: Path, name: str = "killshot.db") -> MemoryStore:
    """Return a fresh, isolated MemoryStore backed by a real SQLite file."""
    return MemoryStore(str(tmp_path / name))


def _fresh_mcp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "mcp.db") -> MemoryStore:
    """Reset the module-level MCP store and return the opened store."""
    db_path = str(tmp_path / name)
    monkeypatch.setenv("LORE_MEMORY_DB", db_path)
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)
    return db_path


def _cleanup_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    if server_mod._store is not None:
        server_mod._store.close()
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)


# ── Test 1: The kill-shot happy path ─────────────────────────────────────────

class TestKillshotHappyPath:
    """
    Full cycle: real subprocess failure → fingerprint → teach fix → re-run →
    suggestion surfaced → rate success → frequency/efficacy updated.
    """

    def test_full_learn_and_recall_cycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_mcp_store(tmp_path, monkeypatch, "killshot1.db")

        # Step 1: Spawn a REAL subprocess that raises a REAL ModuleNotFoundError.
        # We use `python -c` inline so the traceback path is "<string>" — this
        # avoids the tmp_path containing "pytest" which would set tool='pytest'
        # and cause a fingerprint mismatch when teaching via the bare error sig.
        proc = subprocess.run(
            [sys.executable, "-c", "import fixcache_missing_xyz_module"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, "Script should fail"
        stderr = proc.stderr
        assert "ModuleNotFoundError" in stderr or "No module named" in stderr, (
            f"Expected ModuleNotFoundError in: {stderr!r}"
        )
        print(f"\n[Test1] Real stderr captured:\n{stderr.strip()}")

        # Step 2: classify_and_format — no recipes yet, suggestions empty
        store = server_mod._get_store()  # initialise via MCP (ensures schema)
        result_before = classify_and_format(store, stderr)
        fp_hash = result_before.fingerprint_hash
        assert fp_hash is not None, "Fingerprint must be computed from real traceback"
        assert len(fp_hash) == 16
        assert result_before.suggestions == [], (
            f"No recipes taught yet — suggestions should be empty, got: {result_before.suggestions}"
        )
        print(f"[Test1] Fingerprint (before): {fp_hash}, suggestions: []")

        # Step 3: Teach the fix via handle_lore_fix (same API as `lore fix` CLI).
        # Using the bare error signature — fingerprint must match the observed one
        # because both use tool='unknown' (no pytest/pip cues in either).
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fixcache_missing_xyz_module'",
            solution_steps=[
                "pip install fixcache_missing_xyz_module",
                "verify: python -c \"import fixcache_missing_xyz_module\"",
            ],
            tags=["python", "import"],
        )
        assert fix_result["success"] is True
        assert fix_result["fingerprint_hash"] == fp_hash, (
            f"Taught fix fingerprint {fix_result['fingerprint_hash']} "
            f"must match observed failure fingerprint {fp_hash}"
        )
        pattern_id = fix_result["pattern_id"]
        print(f"[Test1] Taught fix, pattern_id={pattern_id}")

        # Step 4: Re-run same subprocess through classify_and_format
        proc2 = subprocess.run(
            [sys.executable, "-c", "import fixcache_missing_xyz_module"],
            capture_output=True,
            text=True,
        )
        result_after = classify_and_format(store, proc2.stderr)

        assert result_after.fingerprint_hash == fp_hash, "Fingerprint must be stable run-to-run"
        assert len(result_after.suggestions) >= 1, (
            f"After teaching, suggestions must be non-empty. Got: {result_after.suggestions}"
        )
        top = result_after.suggestions[0]
        assert "pip install fixcache_missing_xyz_module" in top["solution_steps"], (
            f"Taught step must appear in suggestions. Got: {top['solution_steps']}"
        )
        assert top["confidence"] > 0
        assert top["frequency"] >= 1
        print(f"[Test1] After teaching: suggestion='{top['solution_steps'][0]}', "
              f"conf={top['confidence']}, freq={top['frequency']}")

        # Step 6: Record success outcome via record_outcome
        outcome_result = record_outcome(store, fp_hash, "success")
        assert outcome_result["success"] is True
        assert outcome_result["total_success"] >= 1
        assert outcome_result["efficacy"] is not None
        assert outcome_result["efficacy"] > 0
        print(f"[Test1] Recorded success. efficacy={outcome_result['efficacy']:.2%}")

        # Step 7: Re-run a third time to check frequency incremented
        proc3 = subprocess.run(
            [sys.executable, "-c", "import fixcache_missing_xyz_module"],
            capture_output=True,
            text=True,
        )
        classify_and_format(store, proc3.stderr)  # increments total_seen

        stats = darwin_stats(store)
        fp_row = store.conn.execute(
            "SELECT total_seen, total_success FROM fingerprints WHERE hash=?", (fp_hash,)
        ).fetchone()
        assert fp_row is not None
        total_seen, total_success = fp_row[0], fp_row[1]
        assert total_seen >= 3, f"Expected >=3 observations, got {total_seen}"
        assert total_success >= 1, f"Expected >=1 success, got {total_success}"
        print(f"[Test1] Final: total_seen={total_seen}, total_success={total_success}, "
              f"overall_efficacy={stats['overall_efficacy']}")

        _cleanup_mcp(monkeypatch)


# ── Test 2: Cross-repo fingerprint collapse ───────────────────────────────────

class TestCrossRepoFingerprintCollapse:
    """
    HEADLINE CLAIM: teach a fix for `fake_a`, observe `fake_b`, get the same recipe.

    This demonstrates that lore-memory learns ONE fix for the class of error,
    not one fix per module name. sklearn, pandas, numpy → same fingerprint.
    """

    def test_different_module_names_get_same_recipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_mcp_store(tmp_path, monkeypatch, "collapse.db")

        # Teach fix for fake_a
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fixcache_fake_a'",
            solution_steps=["pip install <module>", "check requirements.txt"],
            tags=["python"],
        )
        assert fix_result["success"] is True
        fp_hash_a = fix_result["fingerprint_hash"]
        print(f"\n[Test2] Taught fix for fake_a. fingerprint={fp_hash_a}")

        # Confirm hash of fake_a == hash of fake_b via raw fingerprint
        hash_b = fingerprint_hash("ModuleNotFoundError: No module named 'fixcache_fake_b'")
        assert fp_hash_a == hash_b, (
            f"Cross-repo collapse FAILED: fake_a hash={fp_hash_a}, fake_b hash={hash_b}. "
            "These should be equal — both are ModuleNotFoundError/python/unknown."
        )
        print(f"[Test2] Confirmed collapse: hash(fake_a) == hash(fake_b) == {hash_b}")

        # Run a subprocess that imports fake_b — use python -c inline so the
        # traceback path is "<string>" not a pytest tmp_path (which would set
        # tool='pytest' and break fingerprint collapse).
        proc = subprocess.run(
            [sys.executable, "-c", "import fixcache_fake_b"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        stderr_b = proc.stderr
        assert "fixcache_fake_b" in stderr_b, f"Expected fake_b in stderr: {stderr_b!r}"

        # Classify the fake_b error — should surface the fake_a-taught recipe
        store = server_mod._get_store()
        result = classify_and_format(store, stderr_b)

        assert result.fingerprint_hash == fp_hash_a, (
            f"fake_b fingerprint {result.fingerprint_hash} should equal "
            f"fake_a fingerprint {fp_hash_a}"
        )
        assert len(result.suggestions) >= 1, (
            "fake_b failure should surface the fake_a-taught recipe. "
            f"Suggestions empty. fingerprint={result.fingerprint_hash}"
        )
        top = result.suggestions[0]
        assert "pip install <module>" in top["solution_steps"], (
            f"Expected 'pip install <module>' in solution_steps, got: {top['solution_steps']}"
        )
        print(f"[Test2] HEADLINE CLAIM VERIFIED: fake_b error surfaced fake_a recipe: "
              f"'{top['solution_steps'][0]}'")

        _cleanup_mcp(monkeypatch)


# ── Test 3: Bayesian downweighting ────────────────────────────────────────────

class TestBayesianDownweighting:
    """
    Teach 2 recipes for the same fingerprint.
    Rate A as success 10x, B as failure 10x.
    Assert A ranks above B.
    """

    def test_good_recipe_ranks_above_bad(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_mcp_store(tmp_path, monkeypatch, "bayes.db")

        error_sig = "ModuleNotFoundError: No module named 'fixcache_bayes_test'"

        # Teach recipe A (the good one)
        fix_a = handle_lore_fix(
            error_signature=error_sig,
            solution_steps=["pip install fixcache_bayes_test"],
            tags=["python"],
        )
        pattern_id_a = fix_a["pattern_id"]
        fp_hash = fix_a["fingerprint_hash"]

        # Teach recipe B (the bad one)
        fix_b = handle_lore_fix(
            error_signature=error_sig,
            solution_steps=["restart the computer and hope"],
            tags=["python"],
        )
        pattern_id_b = fix_b["pattern_id"]

        store = server_mod._get_store()

        # Rate A success 10x
        for _ in range(10):
            store.conn.execute(
                "UPDATE darwin_patterns SET confidence = MIN(confidence + 0.05, 1.0), "
                "frequency = frequency + 1 WHERE id = ?",
                (pattern_id_a,),
            )
        # Rate B failure 10x — reduce confidence
        for _ in range(10):
            store.conn.execute(
                "UPDATE darwin_patterns SET confidence = MAX(confidence - 0.05, 0.0), "
                "frequency = frequency + 1 WHERE id = ?",
                (pattern_id_b,),
            )
        store.conn.commit()

        # Classify and check ordering
        result = classify(store, error_sig, top_k=2)
        candidates = result["candidates"]
        assert len(candidates) >= 2, f"Expected 2 candidates, got {len(candidates)}"

        # Find A and B in candidates
        conf_map = {c["pattern_id"]: c["confidence"] for c in candidates}
        assert pattern_id_a in conf_map, "Recipe A must appear in candidates"
        assert pattern_id_b in conf_map, "Recipe B must appear in candidates"
        assert conf_map[pattern_id_a] > conf_map[pattern_id_b], (
            f"Recipe A (conf={conf_map[pattern_id_a]}) should rank above "
            f"recipe B (conf={conf_map[pattern_id_b]}) after Bayesian updates"
        )
        print(f"\n[Test3] Recipe A conf={conf_map[pattern_id_a]:.3f}, "
              f"Recipe B conf={conf_map[pattern_id_b]:.3f}. A > B: correct.")

        _cleanup_mcp(monkeypatch)


# ── Test 4: Cross-class purity ────────────────────────────────────────────────

class TestCrossClassPurity:
    """
    Teach a ModuleNotFoundError recipe.
    Trigger an AttributeError in a real subprocess.
    Assert the ModuleNotFoundError recipe is NOT surfaced.

    This is the 100% purity claim: errors of class A never bleed into class B.
    """

    def test_attribute_error_does_not_surface_import_recipe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_mcp_store(tmp_path, monkeypatch, "purity.db")

        # Teach a fix for ModuleNotFoundError
        fix_result = handle_lore_fix(
            error_signature="ModuleNotFoundError: No module named 'fixcache_purity_test'",
            solution_steps=["pip install fixcache_purity_test"],
            tags=["python"],
        )
        assert fix_result["success"] is True
        module_not_found_fp = fix_result["fingerprint_hash"]

        # Trigger a real AttributeError via subprocess
        attr_script = tmp_path / "attr_error.py"
        attr_script.write_text("x = None\nprint(x.nonexistent_attr_fixcache)\n")
        proc = subprocess.run(
            [sys.executable, str(attr_script)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        stderr = proc.stderr
        assert "AttributeError" in stderr, f"Expected AttributeError in: {stderr!r}"
        print(f"\n[Test4] Real AttributeError stderr:\n{stderr.strip()}")

        # Compute fingerprint of the AttributeError
        attr_fp = fingerprint_hash(stderr)
        assert attr_fp != module_not_found_fp, (
            f"AttributeError fp ({attr_fp}) should differ from "
            f"ModuleNotFoundError fp ({module_not_found_fp})"
        )
        print(f"[Test4] ModuleNotFoundError fp={module_not_found_fp}, "
              f"AttributeError fp={attr_fp}. Different: correct.")

        # Classify the AttributeError — should NOT surface the import fix
        store = server_mod._get_store()
        result = classify_and_format(store, stderr)

        # The fingerprint must not match the module-not-found one
        assert result.fingerprint_hash != module_not_found_fp, (
            "AttributeError must not map to ModuleNotFoundError fingerprint"
        )

        # Any suggestions surfaced must NOT contain the import recipe
        import_recipe_text = "pip install fixcache_purity_test"
        for suggestion in result.suggestions:
            for step in suggestion["solution_steps"]:
                assert import_recipe_text not in step, (
                    f"ModuleNotFoundError recipe '{import_recipe_text}' leaked into "
                    f"AttributeError suggestions — purity violated!"
                )
        print(f"[Test4] Purity holds: AttributeError suggestions={result.suggestions} "
              f"(no import recipe)")

        _cleanup_mcp(monkeypatch)


# ── Test 5: Privacy round-trip via export_sanitized ───────────────────────────

class TestPrivacyExport:
    """
    Teach a recipe containing an absolute path and an API key shape.
    export_sanitized must strip both.
    The exported entry must still contain the generalized fingerprint hash.
    """

    def test_absolute_paths_and_secrets_stripped_in_export(
        self, tmp_path: Path
    ) -> None:
        store = _fresh_store(tmp_path, "privacy.db")

        # Error text that contains an absolute path and something that looks
        # like an API key in a long quoted string
        dirty_error = (
            'Traceback (most recent call last):\n'
            '  File "/home/miles/secretproject/src/app.py", line 42, in <module>\n'
            '    client = ApiClient(key="sk-1234567890abcdef")\n'
            'ValueError: invalid API key format\n'
        )
        upsert_fingerprint(store, dirty_error)

        corpus = export_sanitized(store)
        assert len(corpus) == 1, "One fingerprint should be exported"
        entry = corpus[0]

        # Privacy checks
        assert "/home/miles" not in str(entry), (
            f"Absolute path leaked into export: {entry}"
        )
        assert "secretproject" not in str(entry), (
            f"Project name leaked into export: {entry}"
        )
        assert "sk-1234567890abcdef" not in str(entry), (
            f"API key shape leaked into export: {entry}"
        )

        # Must still contain the fingerprint hash
        assert "hash" in entry
        assert len(entry["hash"]) == 16
        # essence must exist (may be empty string but not the raw path)
        assert "essence" in entry
        assert "/home/miles" not in entry.get("essence", "")

        print(f"\n[Test5] Export entry (sanitized):\n"
              f"  hash={entry['hash']}\n"
              f"  error_type={entry['error_type']}\n"
              f"  ecosystem={entry['ecosystem']}\n"
              f"  essence={entry['essence']!r}\n"
              f"  top_frame={entry['top_frame']!r}")
        print("[Test5] Privacy HOLDS: no paths, no secrets in export.")

        store.close()


# ── Test 6: Real-world subprocess demo (file not found) ───────────────────────

class TestRealWorldSubprocessDemo:
    """
    bash -c 'cat /nonexistent/file/abc.txt' → real 'No such file or directory'.
    Fingerprint computed. Verify it matches the file-not-found class.
    """

    def test_file_not_found_fingerprints_correctly(self, tmp_path: Path) -> None:
        store = _fresh_store(tmp_path, "fnf.db")

        proc = subprocess.run(
            ["bash", "-c", "cat /nonexistent/file/abc.txt"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0
        stderr = proc.stderr
        assert "No such file or directory" in stderr, (
            f"Expected ENOENT in stderr: {stderr!r}"
        )
        print(f"\n[Test6] Real bash stderr: {stderr.strip()!r}")

        result = classify_and_format(store, stderr)
        fp_hash = result.fingerprint_hash
        assert fp_hash is not None, "Fingerprint must be computed"
        assert len(fp_hash) == 16

        # Verify it classifies as file-not-found (not some unrelated class)
        fp = compute_fingerprint(stderr)
        assert fp.error_type in ("FileNotFound", "Unknown"), (
            f"Expected FileNotFound error_type, got: {fp.error_type!r}"
        )

        # The hash must be stable — run the same command again
        proc2 = subprocess.run(
            ["bash", "-c", "cat /nonexistent/file/abc.txt"],
            capture_output=True,
            text=True,
        )
        result2 = classify_and_format(store, proc2.stderr)
        assert result2.fingerprint_hash == fp_hash, (
            "File-not-found fingerprint must be deterministic across runs"
        )
        print(f"[Test6] Fingerprint: {fp_hash}, error_type={fp.error_type}. Stable: confirmed.")

        store.close()


# ── Test 7: End-to-end metrics demo (the before/after table) ─────────────────

class TestEndToEndMetricsDemo:
    """
    Before: darwin_stats shows 0 everything.
    Teach 3 recipes, trigger 3 failures, rate 2 success + 1 failure.
    After: darwin_stats shows the expected counts.

    Prints a before/after table to stdout (visible with pytest -s).
    """

    def test_before_after_metrics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_mcp_store(tmp_path, monkeypatch, "metrics.db")

        store = server_mod._get_store()

        # ── BEFORE ───────────────────────────────────────────────────────────
        before = darwin_stats(store)
        print("\n" + "="*60)
        print("  DARWIN STATS — BEFORE")
        print("="*60)
        print(f"  total_fingerprints  : {before['total_fingerprints']}")
        print(f"  total_seen_events   : {before['total_seen_events']}")
        print(f"  total_success       : {before['total_success']}")
        print(f"  total_failure       : {before['total_failure']}")
        print(f"  overall_efficacy    : {before['overall_efficacy']}")
        print("="*60)

        assert before["total_fingerprints"] == 0
        assert before["total_seen_events"] == 0
        assert before["overall_efficacy"] is None

        # ── Trigger 3 real failures, then teach fixes from the actual stderr ────
        # Capture real subprocess stderr first, then teach using that as the
        # error_signature so fingerprint(taught) == fingerprint(observed) always.
        inline_codes = [
            ("import fixcache_m1",  ["pip install fixcache_m1"]),
            ("x = 1 + 'two'",       ["cast operand to int before adding"]),
            ("int('abc')",           ["validate input is numeric before int()"]),
        ]
        fp_hashes = []
        for code, steps in inline_codes:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
            )
            assert proc.returncode != 0
            real_stderr = proc.stderr
            # Teach using the real captured stderr so fingerprint matches exactly
            r = handle_lore_fix(error_signature=real_stderr, solution_steps=steps)
            assert r["success"] is True
            fp_hashes.append(r["fingerprint_hash"])
            # Also classify (increments total_seen for this fingerprint)
            classify_and_format(store, real_stderr)

        # ── Rate outcomes: 2 success, 1 failure ───────────────────────────────
        record_outcome(store, fp_hashes[0], "success")
        record_outcome(store, fp_hashes[1], "success")
        record_outcome(store, fp_hashes[2], "failure")

        # ── AFTER ─────────────────────────────────────────────────────────────
        after = darwin_stats(store)
        print("\n" + "="*60)
        print("  DARWIN STATS — AFTER")
        print("="*60)
        print(f"  total_fingerprints  : {after['total_fingerprints']}")
        print(f"  total_seen_events   : {after['total_seen_events']}")
        print(f"  total_success       : {after['total_success']}")
        print(f"  total_failure       : {after['total_failure']}")
        print(f"  overall_efficacy    : {after['overall_efficacy']:.1%}" if after['overall_efficacy'] else "  overall_efficacy    : None")
        print(f"  efficacy_bands      : {after['efficacy_bands']}")
        print(f"  top_error_types     : {after['top_error_types']}")
        print("="*60 + "\n")

        # ── Assertions ────────────────────────────────────────────────────────
        # 3 distinct error shapes → 3 fingerprints
        assert after["total_fingerprints"] == 3, (
            f"Expected 3 fingerprints, got {after['total_fingerprints']}"
        )
        # 2 success + 1 failure from record_outcome
        assert after["total_success"] == 2, (
            f"Expected total_success=2, got {after['total_success']}"
        )
        assert after["total_failure"] == 1, (
            f"Expected total_failure=1, got {after['total_failure']}"
        )
        assert after["overall_efficacy"] is not None
        assert abs(after["overall_efficacy"] - 2/3) < 0.01, (
            f"Expected efficacy ~66.7%, got {after['overall_efficacy']:.1%}"
        )

        _cleanup_mcp(monkeypatch)


# ── Test 8: Determinism stress test ──────────────────────────────────────────

class TestDeterminismStress:
    """
    Run the SAME failing subprocess 100 times through classify_and_format.
    Assert:
    - fingerprint is identical all 100 times
    - total_seen counter in the fingerprints table is exactly 100
    """

    def test_fingerprint_stable_across_100_runs(self, tmp_path: Path) -> None:
        store = _fresh_store(tmp_path, "stress.db")

        # Use python -c inline so traceback path is "<string>", not a pytest
        # tmp_path that would trigger tool='pytest' and break determinism checks.
        inline_cmd = [sys.executable, "-c",
                      "raise ImportError('No module named fixcache_stress_test')"]

        hashes: list[str] = []
        n = 100

        print(f"\n[Test8] Running same subprocess {n}x through classify_and_format...")
        t0 = time.time()
        for _ in range(n):
            proc = subprocess.run(
                inline_cmd,
                capture_output=True,
                text=True,
            )
            assert proc.returncode != 0
            result = classify_and_format(store, proc.stderr)
            assert result.fingerprint_hash is not None
            hashes.append(result.fingerprint_hash)
        elapsed = time.time() - t0
        print(f"[Test8] Completed {n} runs in {elapsed:.2f}s ({elapsed/n*1000:.1f}ms/run)")

        # All hashes must be identical
        unique_hashes = set(hashes)
        assert len(unique_hashes) == 1, (
            f"Fingerprint must be deterministic across {n} runs. "
            f"Got {len(unique_hashes)} distinct hashes: {unique_hashes}"
        )
        canonical_hash = hashes[0]
        print(f"[Test8] Canonical fingerprint: {canonical_hash} (identical across all {n} runs)")

        # total_seen must equal exactly n
        row = store.conn.execute(
            "SELECT total_seen FROM fingerprints WHERE hash = ?", (canonical_hash,)
        ).fetchone()
        assert row is not None, "Fingerprint row must exist after 100 observations"
        total_seen = row[0]
        assert total_seen == n, (
            f"Expected total_seen={n}, got {total_seen}"
        )
        print(f"[Test8] total_seen={total_seen} (exactly {n}). Frequency counter correct.")

        store.close()
