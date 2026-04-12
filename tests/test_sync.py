"""
tests/test_sync.py — Tests for sync.py and hooks.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.sync import (
    sync_claude_md,
    sync_cursorrules,
    sync_agents_md,
    sync_all,
    _MARKER_START,
    _MARKER_END,
    _CURSOR_MARKER_START,
    _CURSOR_MARKER_END,
)
from lore_memory.hooks import install_claude_hooks, generate_hook_script


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Store with some facts, meta, and a darwin_pattern."""
    store.add("we use pnpm not npm", memory_type="fact")
    store.add("prefer TypeScript over JavaScript", memory_type="fact")
    store.add("use postgres for all persistence", memory_type="meta")
    return store


# ── sync_claude_md ────────────────────────────────────────────────────────────

class TestSyncClaudeMd:
    def test_creates_file_with_markers(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        result = sync_claude_md(populated_store, out)
        assert out.exists()
        assert _MARKER_START in result
        assert _MARKER_END in result

    def test_contains_lore_header(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        result = sync_claude_md(populated_store, out)
        assert "Lore Memory" in result

    def test_contains_conventions(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        result = sync_claude_md(populated_store, out)
        assert "pnpm" in result
        assert "TypeScript" in result

    def test_contains_architecture(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        result = sync_claude_md(populated_store, out)
        assert "postgres" in result

    def test_preserves_content_outside_markers(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        # Write existing content with user section before and after
        out.write_text(
            "# My Project\n\nSome existing instructions.\n\n"
            f"{_MARKER_START}\nold lore content\n{_MARKER_END}\n\n"
            "## More user content\n\nKeep this.\n",
            encoding="utf-8",
        )
        result = sync_claude_md(populated_store, out)
        assert "# My Project" in result
        assert "Some existing instructions." in result
        assert "## More user content" in result
        assert "Keep this." in result
        # Old lore content replaced
        assert "old lore content" not in result
        # New lore content present
        assert "pnpm" in result

    def test_appends_markers_when_absent(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        out.write_text("# Existing content\n\nNo lore yet.\n", encoding="utf-8")
        result = sync_claude_md(populated_store, out)
        assert "# Existing content" in result
        assert _MARKER_START in result
        assert _MARKER_END in result

    def test_creates_parent_dirs(self, populated_store, tmp_path):
        out = tmp_path / "subdir" / "deep" / "CLAUDE.md"
        sync_claude_md(populated_store, out)
        assert out.exists()

    def test_idempotent(self, populated_store, tmp_path):
        out = tmp_path / "CLAUDE.md"
        result1 = sync_claude_md(populated_store, out)
        result2 = sync_claude_md(populated_store, out)
        assert result1 == result2

    def test_error_recipes_included(self, store, tmp_path):
        import time, uuid, json as _json
        now = time.time()
        pid = str(uuid.uuid4())
        store.conn.execute(
            "INSERT INTO darwin_patterns "
            "(id, pattern_type, description, rule, frequency, confidence, created_at, last_triggered) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, "error_recipe", "Fix for: ECONNREFUSED.*5432",
             _json.dumps(["docker compose up -d postgres", "pg_isready"]),
             1, 0.5, now, now),
        )
        store.conn.commit()

        out = tmp_path / "CLAUDE.md"
        result = sync_claude_md(store, out)
        assert "ECONNREFUSED" in result
        assert "docker compose up" in result


# ── sync_cursorrules ──────────────────────────────────────────────────────────

class TestSyncCursorrules:
    def test_creates_file_with_markers(self, populated_store, tmp_path):
        out = tmp_path / ".cursorrules"
        result = sync_cursorrules(populated_store, out)
        assert out.exists()
        assert _CURSOR_MARKER_START in result
        assert _CURSOR_MARKER_END in result

    def test_contains_lore_content(self, populated_store, tmp_path):
        out = tmp_path / ".cursorrules"
        result = sync_cursorrules(populated_store, out)
        assert "Lore Memory" in result
        assert "pnpm" in result

    def test_preserves_existing_content(self, populated_store, tmp_path):
        out = tmp_path / ".cursorrules"
        out.write_text(
            "# My cursor rules\n\nalways use spaces\n\n"
            f"{_CURSOR_MARKER_START}\nold\n{_CURSOR_MARKER_END}\n",
            encoding="utf-8",
        )
        result = sync_cursorrules(populated_store, out)
        assert "always use spaces" in result
        assert "old" not in result
        assert "pnpm" in result


# ── sync_agents_md ────────────────────────────────────────────────────────────

class TestSyncAgentsMd:
    def test_creates_file(self, populated_store, tmp_path):
        out = tmp_path / "AGENTS.md"
        result = sync_agents_md(populated_store, out)
        assert out.exists()
        assert "Lore Memory" in result

    def test_preserves_existing_content(self, populated_store, tmp_path):
        out = tmp_path / "AGENTS.md"
        out.write_text("# Codex agents\n\nexisting rules\n", encoding="utf-8")
        result = sync_agents_md(populated_store, out)
        assert "existing rules" in result
        assert "pnpm" in result


# ── sync_all ──────────────────────────────────────────────────────────────────

class TestSyncAll:
    def test_creates_all_four_files(self, populated_store, tmp_path):
        report = sync_all(populated_store, tmp_path)
        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / ".cursorrules").exists()
        assert (tmp_path / ".windsurfrules").exists()
        assert (tmp_path / "AGENTS.md").exists()
        assert set(report["created"]) == {
            "CLAUDE.md", ".cursorrules", ".windsurfrules", "AGENTS.md",
        }
        assert report["synced"] == []

    def test_detects_existing_files(self, populated_store, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# existing\n", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("# existing\n", encoding="utf-8")
        report = sync_all(populated_store, tmp_path)
        assert "CLAUDE.md" in report["synced"]
        assert "AGENTS.md" in report["synced"]
        assert ".cursorrules" in report["created"]

    def test_report_has_project_dir(self, populated_store, tmp_path):
        report = sync_all(populated_store, tmp_path)
        assert "project_dir" in report
        assert str(tmp_path) in report["project_dir"]

    def test_empty_store_still_creates_files(self, store, tmp_path):
        report = sync_all(store, tmp_path)
        assert (tmp_path / "CLAUDE.md").exists()


# ── hooks.py ──────────────────────────────────────────────────────────────────

class TestGenerateHookScript:
    def test_pre_compaction_script(self):
        script = generate_hook_script("pre-compaction")
        assert "lore-memory remember" in script
        assert "#!/usr/bin/env bash" in script

    def test_post_session_script(self):
        script = generate_hook_script("post-session")
        assert "lore-memory sync" in script
        assert "#!/usr/bin/env bash" in script

    def test_invalid_hook_type_raises(self):
        with pytest.raises(ValueError, match="Unknown hook_type"):
            generate_hook_script("nonexistent")


class TestInstallClaudeHooks:
    def test_creates_settings_file(self, tmp_path):
        result = install_claude_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.local.json"
        assert settings_path.exists()
        assert result["path"] == str(settings_path)

    def test_settings_contains_stop_hook(self, tmp_path):
        install_claude_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings_path.read_text())
        stop_hooks = data["hooks"]["Stop"]
        commands = [h["command"] for h in stop_hooks]
        assert any("lore-memory sync" in cmd for cmd in commands)

    def test_settings_contains_prompt_hook(self, tmp_path):
        install_claude_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings_path.read_text())
        prompt_hooks = data["hooks"]["UserPromptSubmit"]
        commands = [h["command"] for h in prompt_hooks]
        assert any("lore-memory recall" in cmd for cmd in commands)

    def test_idempotent_no_duplicate_hooks(self, tmp_path):
        install_claude_hooks(tmp_path)
        install_claude_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.local.json"
        data = json.loads(settings_path.read_text())
        stop_hooks = data["hooks"]["Stop"]
        sync_cmds = [h for h in stop_hooks if "lore-memory sync" in h["command"]]
        assert len(sync_cmds) == 1

    def test_preserves_existing_settings(self, tmp_path):
        settings_path = tmp_path / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"theme": "dark", "hooks": {"Stop": []}}),
            encoding="utf-8",
        )
        install_claude_hooks(tmp_path)
        data = json.loads(settings_path.read_text())
        assert data["theme"] == "dark"

    def test_hooks_added_reported(self, tmp_path):
        result = install_claude_hooks(tmp_path)
        assert len(result["hooks_added"]) >= 2

    def test_already_present_reported_on_second_install(self, tmp_path):
        install_claude_hooks(tmp_path)
        result = install_claude_hooks(tmp_path)
        assert len(result["already_present"]) >= 2
        assert result["hooks_added"] == []


# ── CLI integration ───────────────────────────────────────────────────────────

class TestCLITeach:
    def test_teach_stores_convention(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "test.db")
        rc = main(["--db", db, "teach", "we use pnpm not npm"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Stored convention" in out

    def test_teach_with_tags(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "test.db")
        rc = main(["--db", db, "teach", "use black for formatting", "--tags", "python", "style"])
        assert rc == 0


class TestCLIFix:
    def test_fix_stores_recipe(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "test.db")
        rc = main([
            "--db", db, "fix", "ECONNREFUSED.*5432",
            "--steps", "docker compose up -d postgres", "pg_isready",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "recipe_id" in out

    def test_fix_with_outcome(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "test.db")
        rc = main([
            "--db", db, "fix", "ImportError: no module",
            "--steps", "pip install missing-pkg",
            "--outcome", "success",
        ])
        assert rc == 0


class TestCLISync:
    def test_sync_creates_files(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "project.db")
        main(["--db", db, "remember", "we use pnpm not npm"])
        capsys.readouterr()
        rc = main(["--db", db, "sync", "--dir", str(tmp_path)])
        assert rc == 0
        assert (tmp_path / "CLAUDE.md").exists()

    def test_sync_format_claude(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "project.db")
        rc = main(["--db", db, "sync", "--dir", str(tmp_path), "--format", "claude"])
        assert rc == 0
        assert (tmp_path / "CLAUDE.md").exists()
        assert not (tmp_path / "AGENTS.md").exists()

    def test_sync_format_codex(self, tmp_path, capsys):
        from lore_memory.cli import main
        db = str(tmp_path / "project.db")
        rc = main(["--db", db, "sync", "--dir", str(tmp_path), "--format", "codex"])
        assert rc == 0
        assert (tmp_path / "AGENTS.md").exists()


class TestCLIHookInstall:
    def test_hook_install_creates_settings(self, tmp_path, capsys):
        from lore_memory.cli import main
        rc = main(["hook", "install", "--dir", str(tmp_path)])
        assert rc == 0
        assert (tmp_path / ".claude" / "settings.local.json").exists()
        out = capsys.readouterr().out
        assert "Settings" in out

    def test_hook_install_reports_added(self, tmp_path, capsys):
        from lore_memory.cli import main
        main(["hook", "install", "--dir", str(tmp_path)])
        out = capsys.readouterr().out
        assert "Added" in out
