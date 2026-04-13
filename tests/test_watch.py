"""
test_watch.py — Tests for the activation loop (`lore-memory watch`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.watch import (
    WatchResult,
    _tail,
    activate,
    classify_and_format,
    format_suggestions,
    run_command,
    watch_command,
)
from lore_memory.mcp.server import handle_lore_fix
import lore_memory.mcp.server as server_mod


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(str(tmp_path / "watch.db"))
    yield s
    s.close()


@pytest.fixture
def seeded_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    """Fresh MemoryStore with a seeded fix recipe via the MCP handler."""
    db_path = str(tmp_path / "watch_seeded.db")
    monkeypatch.setenv("LORE_MEMORY_DB", db_path)
    monkeypatch.setattr(server_mod, "_store", None)
    monkeypatch.setattr(server_mod, "_identity", None)
    handle_lore_fix(
        error_signature="ModuleNotFoundError: No module named 'scikit-learn'",
        solution_steps=["pip install scikit-learn", "restart kernel"],
        tags=["python", "import"],
    )
    # Now reopen as a plain MemoryStore (the MCP store is closed elsewhere)
    assert server_mod._store is not None
    yield server_mod._store
    if server_mod._store is not None:
        server_mod._store.close()
    monkeypatch.setattr(server_mod, "_store", None)


# ── Helper unit tests ────────────────────────────────────────────────────────


class TestTail:
    def test_short_input_returned_whole(self) -> None:
        assert _tail("hello") == "hello"

    def test_long_input_truncated_from_start(self) -> None:
        text = "a" * 100 + "tail"
        result = _tail(text, max_bytes=4)
        assert result == "tail"

    def test_boundary(self) -> None:
        text = "x" * 16 * 1024
        assert _tail(text) == text


# ── run_command tests ────────────────────────────────────────────────────────


class TestRunCommand:
    def test_clean_exit(self, capfd: pytest.CaptureFixture) -> None:
        code, captured = run_command(["true"], tee=False)
        assert code == 0
        assert captured == ""

    def test_nonzero_exit_captures_stderr(
        self, capfd: pytest.CaptureFixture
    ) -> None:
        # Use python to emit a controlled error to stderr
        code, captured = run_command(
            [
                "python3",
                "-c",
                "import sys; sys.stderr.write('ModuleNotFoundError: foo\\n'); sys.exit(1)",
            ],
            tee=False,
        )
        assert code == 1
        assert "ModuleNotFoundError" in captured

    def test_unknown_command_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            run_command(["definitely_not_a_real_binary_xyz"], tee=False)


# ── classify_and_format ──────────────────────────────────────────────────────


class TestClassifyAndFormat:
    def test_empty_stderr(self, store: MemoryStore) -> None:
        result = classify_and_format(store, "")
        assert result.fingerprint_hash is None
        assert result.suggestions == []

    def test_new_error_creates_fingerprint(self, store: MemoryStore) -> None:
        result = classify_and_format(store, "TypeError: bad")
        assert result.fingerprint_hash is not None
        assert len(result.fingerprint_hash) == 16
        # No recipes in store yet
        assert result.suggestions == []

    def test_matching_recipe_returned(self, seeded_store: MemoryStore) -> None:
        result = classify_and_format(
            seeded_store,
            "ModuleNotFoundError: No module named 'scikit-learn'",
        )
        assert result.fingerprint_hash is not None
        assert len(result.suggestions) >= 1
        top = result.suggestions[0]
        assert "pip install scikit-learn" in top["solution_steps"]


# ── format_suggestions ──────────────────────────────────────────────────────


class TestFormatSuggestions:
    def test_no_suggestions_no_fingerprint(self) -> None:
        r = WatchResult(exit_code=0, stderr_tail="", fingerprint_hash=None)
        assert format_suggestions(r) == ""

    def test_no_recipe_but_fingerprint(self) -> None:
        r = WatchResult(exit_code=1, stderr_tail="err", fingerprint_hash="abc123")
        out = format_suggestions(r)
        assert "abc123" in out
        assert "fixcache fix" in out

    def test_with_suggestions(self) -> None:
        r = WatchResult(
            exit_code=1,
            stderr_tail="err",
            fingerprint_hash="deadbeef12345678",
            suggestions=[
                {
                    "pattern_id": "pat-1",
                    "confidence": 0.75,
                    "frequency": 3,
                    "description": "Fix for: TypeError",
                    "solution_steps": ["step one", "step two"],
                }
            ],
        )
        out = format_suggestions(r)
        assert "step one" in out
        assert "step two" in out
        assert "pat-1" in out
        assert "conf=0.75" in out

    def test_with_stats_renders_efficacy(self) -> None:
        r = WatchResult(
            exit_code=1,
            stderr_tail="err",
            fingerprint_hash="abcdef",
            suggestions=[
                {
                    "pattern_id": "pat-1",
                    "confidence": 0.5,
                    "frequency": 1,
                    "description": "Fix for: X",
                    "solution_steps": ["do the thing"],
                }
            ],
        )
        stats = {"total_seen": 10, "total_success": 7, "total_failure": 3, "efficacy": 0.7}
        out = format_suggestions(r, stats=stats)
        assert "70%" in out
        assert "7 pass" in out


# ── activate (hook entry point) ───────────────────────────────────────────────


class TestActivate:
    def test_activate_with_known_recipe(self, seeded_store: MemoryStore) -> None:
        result = activate(
            seeded_store,
            "ModuleNotFoundError: No module named 'scikit-learn'",
        )
        assert result["fingerprint_hash"] is not None
        assert len(result["suggestions"]) >= 1
        assert "pip install scikit-learn" in result["human_output"]

    def test_activate_with_unknown_error(self, store: MemoryStore) -> None:
        result = activate(store, "A completely novel error never seen before")
        assert result["fingerprint_hash"] is not None
        assert result["suggestions"] == []

    def test_activate_empty_input(self, store: MemoryStore) -> None:
        result = activate(store, "")
        assert result["fingerprint_hash"] is None
        assert result["suggestions"] == []


# ── watch_command full flow ───────────────────────────────────────────────────


class TestWatchCommand:
    def test_clean_command_returns_zero(
        self, store: MemoryStore, capsys: pytest.CaptureFixture
    ) -> None:
        code = watch_command(store, ["true"], json_output=False)
        assert code == 0

    def test_failing_command_classifies_and_prints(
        self, store: MemoryStore, capsys: pytest.CaptureFixture
    ) -> None:
        code = watch_command(
            store,
            [
                "python3",
                "-c",
                "import sys; sys.stderr.write('ModuleNotFoundError: foo\\n'); sys.exit(1)",
            ],
            json_output=False,
        )
        assert code == 1
        captured = capsys.readouterr()
        # Either the suggestion format or the "new fingerprint" hint should appear
        assert "lore-memory" in captured.err or "ModuleNotFoundError" in captured.err

    def test_json_output(self, store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = watch_command(
            store,
            [
                "python3",
                "-c",
                "import sys; sys.stderr.write('TypeError: nope\\n'); sys.exit(2)",
            ],
            json_output=True,
        )
        assert code == 2
        out = capsys.readouterr().out
        import json as _json
        payload = _json.loads(out)
        assert payload["exit_code"] == 2
        assert payload["fingerprint_hash"] is not None

    def test_missing_command(self, store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = watch_command(store, [], json_output=False)
        assert code == 2

    def test_unknown_binary(self, store: MemoryStore, capsys: pytest.CaptureFixture) -> None:
        code = watch_command(
            store,
            ["definitely_not_a_real_binary_xyz"],
            json_output=False,
        )
        assert code == 127
