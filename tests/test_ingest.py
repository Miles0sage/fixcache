"""
test_ingest.py — Tests for Claude Code transcript ingestion.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.ingest import (
    FixRecipe,
    _describe_tool_use,
    _extract_error_signature,
    _looks_like_error,
    _tool_result_text,
    extract_fix_recipes,
    find_latest_transcript,
    ingest_transcript,
    iter_messages,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    s = MemoryStore(str(tmp_path / "ingest.db"))
    yield s
    s.close()


def _write_jsonl(path: Path, messages: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")


def _user_tool_result(
    text: str, is_error: bool = False, tool_use_id: str = "t1"
) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "content": text,
                }
            ],
        },
    }


def _user_prompt(text: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
    }


def _assistant_with_tool(tool_name: str, input_obj: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tu_{tool_name}",
                    "name": tool_name,
                    "input": input_obj,
                }
            ],
        },
    }


# ── Parser unit tests ─────────────────────────────────────────────────────────


class TestTranscriptParser:
    def test_iter_messages_skips_malformed(self, tmp_path: Path) -> None:
        p = tmp_path / "session.jsonl"
        p.write_text('{"type":"user"}\nnot-json\n{"type":"assistant"}\n')
        messages = list(iter_messages(p))
        assert len(messages) == 2
        assert messages[0]["type"] == "user"
        assert messages[1]["type"] == "assistant"

    def test_iter_messages_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            list(iter_messages("/nonexistent/path.jsonl"))

    def test_tool_result_text_str(self) -> None:
        block = {"type": "tool_result", "content": "simple string output"}
        assert _tool_result_text(block) == "simple string output"

    def test_tool_result_text_list(self) -> None:
        block = {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "line 1"},
                {"type": "text", "text": "line 2"},
            ],
        }
        assert _tool_result_text(block) == "line 1\nline 2"


# ── Error detection ──────────────────────────────────────────────────────────


class TestErrorDetection:
    def test_is_error_flag_wins(self) -> None:
        assert _looks_like_error("", is_error_flag=True) is True

    def test_traceback_detected(self) -> None:
        text = 'Traceback (most recent call last):\n  File "x.py"'
        assert _looks_like_error(text) is True

    def test_module_not_found(self) -> None:
        assert _looks_like_error("ModuleNotFoundError: No module named 'foo'") is True

    def test_ecconrefused(self) -> None:
        assert _looks_like_error("ECONNREFUSED 127.0.0.1:5432") is True

    def test_exit_code_nonzero(self) -> None:
        assert _looks_like_error("command failed with exit code 2") is True

    def test_clean_output_not_error(self) -> None:
        assert _looks_like_error("test passed in 1.2s") is False
        assert _looks_like_error("Hello, world") is False


class TestSignatureExtraction:
    def test_picks_last_matching_line(self) -> None:
        text = "Traceback (most recent call last):\n  File 'x.py'\nModuleNotFoundError: No module named 'foo'"
        sig = _extract_error_signature(text)
        assert "ModuleNotFoundError" in sig
        assert "foo" in sig

    def test_empty_input(self) -> None:
        assert _extract_error_signature("") == ""
        assert _extract_error_signature("\n\n") == ""

    def test_absolute_paths_redacted(self) -> None:
        text = "FileNotFoundError: /home/user/secret/project/file.py"
        sig = _extract_error_signature(text)
        assert "/home/user/secret/project" not in sig

    def test_signature_truncated(self) -> None:
        long = "Error: " + ("x" * 1000)
        sig = _extract_error_signature(long)
        assert len(sig) <= 240


# ── Tool use description ─────────────────────────────────────────────────────


class TestToolUseDescription:
    def test_bash_includes_command(self) -> None:
        block = {"name": "Bash", "input": {"command": "pip install foo"}}
        assert _describe_tool_use(block) == "Run: pip install foo"

    def test_edit_includes_file(self) -> None:
        block = {"name": "Edit", "input": {"file_path": "/repo/src/main.py"}}
        desc = _describe_tool_use(block)
        assert desc.startswith("Edit")
        assert "main.py" in desc

    def test_unknown_tool_fallback(self) -> None:
        block = {"name": "MysteryTool", "input": {}}
        assert _describe_tool_use(block) == "Used MysteryTool"


# ── Extraction end-to-end ────────────────────────────────────────────────────


class TestExtraction:
    def test_simple_error_fix_window(self) -> None:
        messages = [
            _user_prompt("run the tests"),
            _assistant_with_tool("Bash", {"command": "pytest"}),
            _user_tool_result(
                "ModuleNotFoundError: No module named 'scikit-learn'",
                is_error=True,
            ),
            _assistant_with_tool("Bash", {"command": "pip install scikit-learn"}),
            _assistant_with_tool("Bash", {"command": "pytest"}),
            _user_tool_result("all tests passed", is_error=False),
        ]
        recipes = extract_fix_recipes(messages, source_file="test.jsonl")
        assert len(recipes) == 1
        assert "ModuleNotFoundError" in recipes[0].error_signature
        assert any(
            "pip install scikit-learn" in s for s in recipes[0].solution_steps
        )
        assert "claude-code" in recipes[0].tags

    def test_no_error_no_recipe(self) -> None:
        messages = [
            _user_prompt("hello"),
            _assistant_with_tool("Bash", {"command": "ls"}),
            _user_tool_result("file1\nfile2", is_error=False),
        ]
        assert extract_fix_recipes(messages) == []

    def test_multiple_errors_multiple_recipes(self) -> None:
        messages = [
            _user_prompt("start"),
            _assistant_with_tool("Bash", {"command": "python app.py"}),
            _user_tool_result("ImportError: cannot import name 'Foo'", is_error=True),
            _assistant_with_tool("Edit", {"file_path": "/a/app.py"}),
            _user_tool_result("ok", is_error=False),
            _user_prompt("deploy it"),
            _assistant_with_tool("Bash", {"command": "docker compose up"}),
            _user_tool_result(
                "ECONNREFUSED 127.0.0.1:5432", is_error=True
            ),
            _assistant_with_tool("Bash", {"command": "docker compose up -d postgres"}),
            _user_tool_result("started", is_error=False),
        ]
        recipes = extract_fix_recipes(messages)
        assert len(recipes) == 2
        sigs = [r.error_signature for r in recipes]
        assert any("ImportError" in s for s in sigs)
        assert any("ECONNREFUSED" in s for s in sigs)

    def test_max_steps_cap(self) -> None:
        messages = [_user_tool_result("Error: xyz failed", is_error=True)]
        # Add 20 assistant bash blocks — should cap at 8
        for i in range(20):
            messages.append(_assistant_with_tool("Bash", {"command": f"echo step{i}"}))
        recipes = extract_fix_recipes(messages)
        assert len(recipes) == 1
        assert len(recipes[0].solution_steps) <= 8


# ── Storage pipeline ─────────────────────────────────────────────────────────


class TestStorage:
    def test_ingest_transcript_stores_recipes(
        self, store: MemoryStore, tmp_path: Path
    ) -> None:
        messages = [
            _user_prompt("run tests"),
            _assistant_with_tool("Bash", {"command": "pytest"}),
            _user_tool_result(
                "FileNotFoundError: .../conftest.py", is_error=True
            ),
            _assistant_with_tool("Write", {"file_path": "/repo/conftest.py"}),
            _user_tool_result("file created", is_error=False),
        ]
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, messages)

        report = ingest_transcript(store, p)
        assert report["recipes_extracted"] == 1
        assert report["recipes_stored"] == 1
        assert len(report["pattern_ids"]) == 1

        # Verify it landed in darwin_patterns + darwin_journal + memories
        pat_count = store.conn.execute(
            "SELECT COUNT(*) FROM darwin_patterns"
        ).fetchone()[0]
        assert pat_count == 1

        journal_count = store.conn.execute(
            "SELECT COUNT(*) FROM darwin_journal"
        ).fetchone()[0]
        assert journal_count == 1

        mem_count = store.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_type='experience'"
        ).fetchone()[0]
        assert mem_count == 1

    def test_dry_run_does_not_store(
        self, store: MemoryStore, tmp_path: Path
    ) -> None:
        messages = [
            _user_tool_result("TypeError: bad", is_error=True),
            _assistant_with_tool("Edit", {"file_path": "/a/b.py"}),
            _user_tool_result("ok", is_error=False),
        ]
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, messages)

        report = ingest_transcript(store, p, dry_run=True)
        assert report["recipes_extracted"] == 1
        assert report["recipes_stored"] == 0
        assert report["pattern_ids"] == []
        pat_count = store.conn.execute(
            "SELECT COUNT(*) FROM darwin_patterns"
        ).fetchone()[0]
        assert pat_count == 0

    def test_ingested_recipes_are_matchable(
        self, store: MemoryStore, tmp_path: Path
    ) -> None:
        """End-to-end: ingest → match_procedure finds the recipe."""
        messages = [
            _user_tool_result(
                "ModuleNotFoundError: No module named 'scikit-learn'",
                is_error=True,
            ),
            _assistant_with_tool("Bash", {"command": "pip install scikit-learn"}),
            _user_tool_result("Successfully installed", is_error=False),
        ]
        p = tmp_path / "session.jsonl"
        _write_jsonl(p, messages)
        ingest_transcript(store, p)

        # Verify the pattern is retrievable via regex match
        rows = store.conn.execute(
            "SELECT description, rule FROM darwin_patterns WHERE pattern_type='error_recipe'"
        ).fetchall()
        assert len(rows) == 1
        description, rule = rows[0]
        assert "scikit-learn" in description
        assert "pip install scikit-learn" in rule


# ── find_latest_transcript ────────────────────────────────────────────────────


class TestFindLatest:
    def test_returns_none_when_no_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ~/.claude/projects doesn't exist, returns None."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        # Path.home() uses HOME on POSIX
        assert find_latest_transcript() is None

    def test_finds_latest_jsonl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "fakehome"
        proj_dir = fake_home / ".claude" / "projects" / "testproj"
        proj_dir.mkdir(parents=True)
        old = proj_dir / "old.jsonl"
        new = proj_dir / "new.jsonl"
        old.write_text('{"type":"user"}\n')
        new.write_text('{"type":"user"}\n')

        # Touch new to make it more recent
        import os
        import time
        os.utime(old, (time.time() - 1000, time.time() - 1000))

        monkeypatch.setenv("HOME", str(fake_home))
        latest = find_latest_transcript()
        assert latest is not None
        assert latest.name == "new.jsonl"
