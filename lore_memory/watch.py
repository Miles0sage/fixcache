"""
watch.py — `lore-memory watch <cmd>`: the activation loop.

Wraps a shell command, streams stderr through the Darwin Replay classifier
in real time, and surfaces matching fix recipes the moment an error is
detected. This is the "it learned" moment — the missing piece all prior
AI reviews flagged as existential.

Design:
  - Run the target command via subprocess.Popen, line-buffered
  - Tee stderr to the terminal AND accumulate for classification
  - When the child process exits non-zero, classify the accumulated
    error text and print the top recipes with efficacy stats
  - Optional --suggest-only: never interrupt, just print suggestions
  - Optional --json: emit machine-readable output for hook integration

Usage:
    lore-memory watch pytest tests/
    lore-memory watch python manage.py migrate
    lore-memory watch npm test
    lore-memory watch -- docker compose up
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from .core.store import MemoryStore
from .darwin_replay import classify, upsert_fingerprint


@dataclass
class WatchResult:
    exit_code: int
    stderr_tail: str
    fingerprint_hash: str | None
    suggestions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stderr_tail": self.stderr_tail,
            "fingerprint_hash": self.fingerprint_hash,
            "suggestions": self.suggestions,
        }


# ── Stderr buffer management ──────────────────────────────────────────────────

_MAX_STDERR_CAPTURE = 16 * 1024  # 16 KB ring buffer; enough for any stack trace


def _tail(text: str, max_bytes: int = _MAX_STDERR_CAPTURE) -> str:
    """Return the last `max_bytes` of `text` (simple ring-buffer tail)."""
    if len(text) <= max_bytes:
        return text
    return text[-max_bytes:]


# ── Core execution ────────────────────────────────────────────────────────────

def run_command(
    command: list[str],
    tee: bool = True,
) -> tuple[int, str]:
    """
    Run a command as a subprocess, streaming stderr to the terminal
    while accumulating it for classification.

    Args:
        command: List of command tokens (passed to Popen).
        tee: If True (default), stream stderr live to the real stderr.

    Returns:
        (exit_code, captured_stderr).
    """
    proc = subprocess.Popen(
        command,
        stdout=None,          # inherit parent stdout — user sees it live
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,            # line-buffered so errors surface immediately
    )
    buf: list[str] = []

    assert proc.stderr is not None
    try:
        for line in proc.stderr:
            if tee:
                sys.stderr.write(line)
                sys.stderr.flush()
            buf.append(line)
    except KeyboardInterrupt:
        proc.terminate()
        raise

    proc.wait()
    captured = _tail("".join(buf))
    return proc.returncode, captured


# ── Classification + suggestion formatting ────────────────────────────────────

def classify_and_format(
    store: MemoryStore,
    stderr_text: str,
    top_k: int = 3,
) -> WatchResult:
    """
    Classify captured stderr and build a WatchResult with human-readable
    suggestions. Also upserts the fingerprint so `darwin stats` reflects
    the observed failure.
    """
    if not stderr_text.strip():
        return WatchResult(
            exit_code=0,
            stderr_tail="",
            fingerprint_hash=None,
        )

    # Upsert ensures the failure is counted even if no recipe exists yet
    upsert_fingerprint(store, stderr_text)
    result = classify(store, stderr_text, top_k=top_k)

    fp = result["fingerprint"]
    suggestions: list[dict[str, Any]] = []
    for c in result["candidates"]:
        suggestions.append(
            {
                "pattern_id": c["pattern_id"],
                "confidence": c["confidence"],
                "frequency": c["frequency"],
                "description": c["description"],
                "solution_steps": c["solution_steps"],
            }
        )

    return WatchResult(
        exit_code=0,
        stderr_tail=_tail(stderr_text, 2048),
        fingerprint_hash=fp["hash"],
        suggestions=suggestions,
    )


def format_suggestions(
    result: WatchResult,
    stats: dict[str, Any] | None = None,
) -> str:
    """Human-readable suggestions for terminal output."""
    if not result.suggestions:
        if result.fingerprint_hash:
            return (
                f"\n💡 lore-memory: new failure fingerprint {result.fingerprint_hash}. "
                f"No recipes yet — use `lore-memory fix` to teach me the fix.\n"
            )
        return ""

    lines = [""]
    lines.append(
        f"💡 lore-memory: matched fingerprint {result.fingerprint_hash}"
    )
    if stats:
        total = stats.get("total_seen", 0)
        efficacy = stats.get("efficacy")
        if efficacy is not None:
            lines.append(
                f"   seen {total}x — efficacy {efficacy:.0%} "
                f"({stats.get('total_success', 0)} pass / "
                f"{stats.get('total_failure', 0)} fail)"
            )
        else:
            lines.append(f"   seen {total}x — unrated so far")

    for i, s in enumerate(result.suggestions, 1):
        lines.append("")
        lines.append(
            f"  [{i}] {s['description'][:100]}  "
            f"(conf={s['confidence']}, freq={s['frequency']})"
        )
        for step in s["solution_steps"][:5]:
            lines.append(f"      → {step}")
        if len(s["solution_steps"]) > 5:
            lines.append(f"      → (+{len(s['solution_steps']) - 5} more)")

    lines.append("")
    top = result.suggestions[0]
    lines.append(
        f"   Apply? Run: lore-memory darwin report {top['pattern_id']} success  "
        f"# or 'failure'"
    )
    lines.append("")
    return "\n".join(lines)


# ── Hook-friendly entry point ─────────────────────────────────────────────────

def activate(
    store: MemoryStore,
    error_text: str,
    top_k: int = 3,
) -> dict[str, Any]:
    """
    Stateless activation entry point for hook integrations (e.g. Claude
    Code PreToolUse matcher). Takes error text, returns the top recipes
    as a dict suitable for JSON output.

    This is the function a hook script calls when it sees a matching
    error pattern in stderr or a tool_result block.
    """
    result = classify_and_format(store, error_text, top_k=top_k)
    # Look up full fingerprint stats so hooks can display efficacy
    stats: dict[str, Any] | None = None
    if result.fingerprint_hash:
        stats_result = classify(store, error_text, top_k=top_k)
        stats = stats_result.get("fingerprint_stats")
    return {
        "fingerprint_hash": result.fingerprint_hash,
        "fingerprint_stats": stats,
        "suggestions": result.suggestions,
        "human_output": format_suggestions(result, stats=stats),
    }


# ── CLI helper used by cli.py ────────────────────────────────────────────────

def watch_command(
    store: MemoryStore,
    command: list[str],
    suggest_only: bool = False,
    json_output: bool = False,
) -> int:
    """
    Run the `lore-memory watch <cmd>` flow: execute, classify on failure,
    print suggestions. Returns the child's exit code (so shell piping works).
    """
    import json as _json

    if not command:
        sys.stderr.write("lore-memory watch: missing command to run\n")
        return 2

    try:
        exit_code, captured = run_command(command, tee=not json_output)
    except FileNotFoundError:
        sys.stderr.write(
            f"lore-memory watch: command not found: {command[0]}\n"
        )
        return 127
    except KeyboardInterrupt:
        return 130

    if exit_code == 0:
        # Success — don't burn recipes on clean runs
        if json_output:
            print(
                _json.dumps(
                    {"exit_code": 0, "message": "clean run"}
                )
            )
        return 0

    # Failure path — classify and surface recipes
    result = classify_and_format(store, captured, top_k=3)
    result.exit_code = exit_code  # propagate the real child exit code

    # Attach stats for the output formatter
    stats: dict[str, Any] | None = None
    if result.fingerprint_hash:
        cls = classify(store, captured, top_k=1)
        stats = cls.get("fingerprint_stats")

    if json_output:
        payload = result.to_dict()
        payload["fingerprint_stats"] = stats
        print(_json.dumps(payload, indent=2))
    else:
        msg = format_suggestions(result, stats=stats)
        if msg:
            sys.stderr.write(msg)
            sys.stderr.flush()

    if suggest_only:
        return exit_code
    return exit_code
