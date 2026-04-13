"""
hooks.py — Claude Code hook integration for fixcache.

Installs a PostToolUse hook into .claude/settings.local.json so that
whenever a Bash command fails inside Claude Code, the stderr/output is
piped to `fixcache activate` and matching fix recipes are surfaced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# ── Hook command ──────────────────────────────────────────────────────────────

# Reads the PostToolUse JSON payload from stdin, extracts output/stderr,
# and pipes it to fixcache activate. Fails silently so it never blocks work.
_POSTUSE_COMMAND = (
    "python3 -c \""
    "import sys, json; "
    "d=json.load(sys.stdin); "
    "out=d.get('output','') or d.get('stderr',''); "
    "sys.exit(0) if not out else None"
    "\" 2>/dev/null && true || "
    "python3 -c \""
    "import sys, json, subprocess; "
    "d=json.load(sys.stdin) if not sys.stdin.isatty() else {}; "
    "out=(d.get('output','') or d.get('stderr','') or '').strip(); "
    "subprocess.run(['fixcache','activate'],input=out,text=True) if out else None"
    "\" 2>/dev/null || true"
)

# Simpler, robust version: just pipe full stdin to fixcache activate
# fixcache activate reads from stdin and handles non-error / empty input gracefully
_POSTUSE_COMMAND_SIMPLE = (
    "jq -r '.output // .stderr // empty' 2>/dev/null | fixcache activate 2>/dev/null || true"
)


# ── Settings helpers ──────────────────────────────────────────────────────────

def _load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def _add_hook(
    hooks_list: list[dict[str, Any]],
    matcher: str,
    command: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (new_list, was_added)."""
    for existing in hooks_list:
        if existing.get("command") == command:
            return hooks_list, False
    return [*hooks_list, {"matcher": matcher, "command": command}], True


# ── Public API ────────────────────────────────────────────────────────────────

def install_claude_hooks(project_dir: str | Path) -> dict[str, Any]:
    """
    Install fixcache PostToolUse hook into .claude/settings.local.json.

    Adds a PostToolUse hook matching Bash tool calls: on failure the output
    is piped to `fixcache activate` which surfaces matching fix recipes.

    Returns:
        Report dict with keys: path, hooks_added, already_present.
    """
    base = Path(project_dir)
    settings_path = base / ".claude" / "settings.local.json"

    settings = _load_settings(settings_path)
    if "hooks" not in settings:
        settings = {**settings, "hooks": {}}

    hooks: dict[str, Any] = dict(settings["hooks"])
    hooks_added: list[str] = []
    already_present: list[str] = []

    # PostToolUse — intercept Bash errors and surface fix recipes
    post_hooks: list[dict[str, Any]] = list(hooks.get("PostToolUse", []))
    new_post, added = _add_hook(post_hooks, "Bash", _POSTUSE_COMMAND_SIMPLE)
    if added:
        hooks_added.append("PostToolUse: fixcache activate (Bash errors)")
    else:
        already_present.append("PostToolUse: fixcache activate (Bash errors)")
    hooks = {**hooks, "PostToolUse": new_post}

    settings = {**settings, "hooks": hooks}
    _save_settings(settings_path, settings)

    return {
        "path": str(settings_path),
        "hooks_added": hooks_added,
        "already_present": already_present,
    }
