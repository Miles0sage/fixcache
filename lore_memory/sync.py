"""
sync.py — Export lore-memory to agent config formats.

Generates CLAUDE.md sections, .cursorrules, and AGENTS.md from stored memories.
Uses <!-- LORE:START --> / <!-- LORE:END --> markers so user content is preserved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core.store import MemoryStore

_MARKER_START = "<!-- LORE:START -->"
_MARKER_END = "<!-- LORE:END -->"

_CURSOR_MARKER_START = "# LORE:START"
_CURSOR_MARKER_END = "# LORE:END"

_AGENTS_MARKER_START = "# LORE:START"
_AGENTS_MARKER_END = "# LORE:END"


# ── Section builders ──────────────────────────────────────────────────────────

def _build_conventions_section(store: MemoryStore) -> str:
    """Build the Conventions section from fact-type memories."""
    facts = store.list_all(memory_type="fact", limit=50)
    if not facts:
        return ""
    lines = ["## Conventions", ""]
    for mem in facts:
        content = mem.get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    lines.append("")
    return "\n".join(lines)


def _build_error_recipes_section(store: MemoryStore) -> str:
    """Build the Error Recipes section from darwin_patterns."""
    rows = store.conn.execute(
        """
        SELECT description, rule
        FROM darwin_patterns
        WHERE pattern_type = 'error_recipe'
        ORDER BY confidence DESC, frequency DESC
        LIMIT 20
        """
    ).fetchall()
    if not rows:
        return ""
    lines = ["## Error Recipes", ""]
    for description, rule_json in rows:
        sig = description.replace("Fix for: ", "", 1)
        lines.append(f"### `{sig}`")
        try:
            steps = json.loads(rule_json)
            for i, step in enumerate(steps, 1):
                lines.append(f"{i}. {step}")
        except (json.JSONDecodeError, TypeError):
            lines.append(f"- {rule_json}")
        lines.append("")
    return "\n".join(lines)


def _build_architecture_section(store: MemoryStore) -> str:
    """Build the Architecture Decisions section from meta memories."""
    metas = store.list_all(memory_type="meta", limit=20)
    if not metas:
        return ""
    lines = ["## Architecture Decisions", ""]
    for mem in metas:
        content = mem.get("content", "").strip()
        if content:
            lines.append(f"- {content}")
    lines.append("")
    return "\n".join(lines)


def _build_identity_section(store: MemoryStore) -> str:
    """Build the Identity section from the identity table."""
    row = store.conn.execute(
        "SELECT value FROM identity WHERE key = 'default'"
    ).fetchone()
    if row is None:
        return ""
    lines = ["## Identity", ""]
    # value is YAML — include verbatim
    lines.append("```yaml")
    lines.append(row[0].strip())
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _build_lore_block(store: MemoryStore) -> str:
    """Build the full lore block content (without markers)."""
    sections: list[str] = ["# Lore Memory — Auto-generated conventions", ""]

    identity = _build_identity_section(store)
    if identity:
        sections.append(identity)

    conventions = _build_conventions_section(store)
    if conventions:
        sections.append(conventions)

    recipes = _build_error_recipes_section(store)
    if recipes:
        sections.append(recipes)

    arch = _build_architecture_section(store)
    if arch:
        sections.append(arch)

    return "\n".join(sections)


# ── Marker-based upsert ───────────────────────────────────────────────────────

def _upsert_markers(
    existing: str,
    new_content: str,
    start_marker: str,
    end_marker: str,
) -> str:
    """
    Replace content between start/end markers, preserving text outside.
    If markers are absent, append the marked block to the end.
    """
    if start_marker in existing and end_marker in existing:
        before = existing[: existing.index(start_marker)]
        after = existing[existing.index(end_marker) + len(end_marker):]
        return f"{before}{start_marker}\n{new_content}\n{end_marker}{after}"

    # Markers absent — append
    sep = "\n\n" if existing and not existing.endswith("\n\n") else ""
    return f"{existing}{sep}{start_marker}\n{new_content}\n{end_marker}\n"


# ── Public syncers ────────────────────────────────────────────────────────────

def sync_claude_md(store: MemoryStore, output_path: str | Path) -> str:
    """
    Generate or update a CLAUDE.md with a lore-memory section.

    If the file exists, only the content between <!-- LORE:START --> and
    <!-- LORE:END --> is replaced; everything outside is preserved.

    Returns the final file content.
    """
    path = Path(output_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _build_lore_block(store)
    updated = _upsert_markers(existing, block, _MARKER_START, _MARKER_END)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return updated


def sync_cursorrules(store: MemoryStore, output_path: str | Path) -> str:
    """
    Generate or update a .cursorrules file with a lore-memory section.

    Uses # LORE:START / # LORE:END comment markers.

    Returns the final file content.
    """
    path = Path(output_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # Build content in Cursor rules format (plain text, not markdown HTML comments)
    block = _build_lore_block(store)
    updated = _upsert_markers(existing, block, _CURSOR_MARKER_START, _CURSOR_MARKER_END)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return updated


def sync_agents_md(store: MemoryStore, output_path: str | Path) -> str:
    """
    Generate or update an AGENTS.md file with a lore-memory section.

    Uses # LORE:START / # LORE:END markers (Codex CLI format).

    Returns the final file content.
    """
    path = Path(output_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = _build_lore_block(store)
    updated = _upsert_markers(existing, block, _AGENTS_MARKER_START, _AGENTS_MARKER_END)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return updated


def sync_all(store: MemoryStore, project_dir: str | Path) -> dict[str, Any]:
    """
    Run all syncers for the given project directory.

    Auto-detects which config files already exist; creates any that are missing.

    Returns a report dict:
        {
            "synced": ["CLAUDE.md", ...],
            "created": ["AGENTS.md", ...],
            "skipped": [],
        }
    """
    base = Path(project_dir)
    synced: list[str] = []
    created: list[str] = []

    targets = [
        ("CLAUDE.md", sync_claude_md),
        (".cursorrules", sync_cursorrules),
        ("AGENTS.md", sync_agents_md),
    ]

    for filename, syncer in targets:
        target_path = base / filename
        existed = target_path.exists()
        syncer(store, target_path)
        if existed:
            synced.append(filename)
        else:
            created.append(filename)

    return {
        "synced": synced,
        "created": created,
        "skipped": [],
        "project_dir": str(base),
    }
