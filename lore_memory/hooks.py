"""
hooks.py — Claude Code hook integration for lore-memory.

Installs hooks into .claude/settings.local.json so lore-memory sync
runs automatically after each session, and context is pre-loaded on
each prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ── Shell script templates ────────────────────────────────────────────────────

_PRE_COMPACTION_SCRIPT = """\
#!/usr/bin/env bash
# lore-memory: save context before compaction
set -euo pipefail
if command -v lore-memory >/dev/null 2>&1; then
    lore-memory remember "Pre-compaction checkpoint: $(date -u +%Y-%m-%dT%H:%M:%SZ)" --type meta 2>/dev/null || true
fi
"""

_POST_SESSION_SCRIPT = """\
#!/usr/bin/env bash
# lore-memory: sync config files after session ends
set -euo pipefail
if command -v lore-memory >/dev/null 2>&1; then
    lore-memory sync 2>/dev/null || true
fi
"""


def generate_hook_script(hook_type: str) -> str:
    """
    Generate a shell script for the given hook type.

    Args:
        hook_type: One of 'pre-compaction' or 'post-session'.

    Returns:
        Shell script content as a string.

    Raises:
        ValueError: If hook_type is not recognised.
    """
    scripts = {
        "pre-compaction": _PRE_COMPACTION_SCRIPT,
        "post-session": _POST_SESSION_SCRIPT,
    }
    if hook_type not in scripts:
        raise ValueError(
            f"Unknown hook_type {hook_type!r}. "
            f"Valid values: {list(scripts.keys())}"
        )
    return scripts[hook_type]


# ── Settings.local.json management ───────────────────────────────────────────

def _load_settings(path: Path) -> dict[str, Any]:
    """Load JSON settings file, returning empty dict if absent or invalid."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(path: Path, settings: dict[str, Any]) -> None:
    """Write settings dict as formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _add_hook(
    hooks_list: list[dict[str, Any]],
    matcher: str,
    command: str,
) -> list[dict[str, Any]]:
    """
    Return a new hooks list with the given hook appended (if not already present).
    Immutable — does not modify the input list.
    """
    for existing in hooks_list:
        if existing.get("matcher") == matcher and existing.get("command") == command:
            return hooks_list  # already present
    return [*hooks_list, {"matcher": matcher, "command": command}]


def install_claude_hooks(project_dir: str | Path) -> dict[str, Any]:
    """
    Install lore-memory hooks into .claude/settings.local.json.

    Adds:
      - Stop hook: runs `lore-memory sync` after each session ends.
      - UserPromptSubmit hook: runs `lore-memory recall --context`
        for pre-loading relevant memory before each prompt.

    If the settings file already exists its content is preserved; hooks
    are only added if not already present.

    Args:
        project_dir: Path to the project root (where .claude/ lives).

    Returns:
        Report dict with keys: path, hooks_added, already_present.
    """
    base = Path(project_dir)
    settings_path = base / ".claude" / "settings.local.json"

    settings = _load_settings(settings_path)

    # Ensure hooks key exists
    if "hooks" not in settings:
        settings = {**settings, "hooks": {}}

    hooks: dict[str, Any] = dict(settings["hooks"])
    hooks_added: list[str] = []
    already_present: list[str] = []

    # Stop hook — sync after session
    stop_hooks: list[dict[str, Any]] = list(hooks.get("Stop", []))
    sync_cmd = "lore-memory sync"
    new_stop = _add_hook(stop_hooks, ".*", sync_cmd)
    if len(new_stop) > len(stop_hooks):
        hooks_added.append("Stop: lore-memory sync")
    else:
        already_present.append("Stop: lore-memory sync")
    hooks = {**hooks, "Stop": new_stop}

    # UserPromptSubmit hook — pre-load context
    prompt_hooks: list[dict[str, Any]] = list(hooks.get("UserPromptSubmit", []))
    recall_cmd = "lore-memory recall --context 2>/dev/null || true"
    new_prompt = _add_hook(prompt_hooks, ".*", recall_cmd)
    if len(new_prompt) > len(prompt_hooks):
        hooks_added.append("UserPromptSubmit: lore-memory recall --context")
    else:
        already_present.append("UserPromptSubmit: lore-memory recall --context")
    hooks = {**hooks, "UserPromptSubmit": new_prompt}

    settings = {**settings, "hooks": hooks}
    _save_settings(settings_path, settings)

    return {
        "path": str(settings_path),
        "hooks_added": hooks_added,
        "already_present": already_present,
    }
