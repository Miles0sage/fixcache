#!/usr/bin/env python3
"""
ci_classify.py — Reads stderr/error text from stdin, runs fixcache activate,
and writes a GitHub Actions step summary with fix recipes.

Never exits non-zero. Designed to be safe to run in CI even when fixcache
has no match or encounters an error.

Usage:
    echo 'ModuleNotFoundError: No module named torch' | python scripts/ci_classify.py
    cat pytest-output.txt | python scripts/ci_classify.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def write_summary(content: str) -> None:
    """Append markdown content to $GITHUB_STEP_SUMMARY if set, else print."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(content + "\n")
    else:
        print(content)


def classify(error_text: str) -> dict | None:
    """Run fixcache activate and return parsed JSON, or None on failure."""
    try:
        result = subprocess.run(
            ["fixcache", "activate", "-", "--json"],
            input=error_text,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return None


def format_summary(data: dict, min_confidence: float) -> str:
    """Format fixcache output as GitHub Actions step summary markdown."""
    lines: list[str] = []
    lines.append("## fixcache — Error Memory Report")
    lines.append("")

    fp = data.get("fingerprint_stats", {})
    fp_hash = data.get("fingerprint_hash", "unknown")
    error_type = fp.get("error_type", "unknown")
    ecosystem = fp.get("ecosystem", "unknown")
    seen = fp.get("total_seen", 0)
    efficacy = fp.get("efficacy")

    lines.append(f"**Fingerprint:** `{fp_hash}`")
    lines.append(f"**Error type:** `{error_type}` | **Ecosystem:** `{ecosystem}`")
    lines.append(f"**Seen:** {seen}x | **Efficacy:** {f'{efficacy:.0%}' if efficacy is not None else 'unrated'}")
    lines.append("")

    suggestions = data.get("suggestions", [])
    above_threshold = [s for s in suggestions if s.get("confidence", 0) >= min_confidence]

    if not above_threshold:
        lines.append("> No fix recipes matched above confidence threshold.")
        lines.append("")
        lines.append(f"_Threshold: {min_confidence:.0%}. Lower `min-confidence` or add a recipe with `fixcache fix`._")
        return "\n".join(lines)

    lines.append("### Fix Recipes")
    lines.append("")

    for i, suggestion in enumerate(above_threshold, 1):
        confidence = suggestion.get("confidence", 0)
        description = suggestion.get("description", "Unnamed recipe")
        steps = suggestion.get("solution_steps", [])
        pattern_id = suggestion.get("pattern_id", "")
        frequency = suggestion.get("frequency", 0)

        lines.append(f"#### {i}. {description}")
        lines.append(f"**Confidence:** {confidence:.0%} | **Frequency:** {frequency}x")
        lines.append("")

        if steps:
            lines.append("**Steps:**")
            for step in steps:
                lines.append(f"```bash")
                lines.append(step)
                lines.append("```")

        if pattern_id:
            lines.append("")
            lines.append("**Darwin feedback loop — close it after applying:**")
            lines.append(f"```bash")
            lines.append(f"fixcache darwin report {pattern_id} success  # or 'failure'")
            lines.append(f"```")

        lines.append("")

    lines.append("---")
    lines.append("_Powered by [fixcache](https://github.com/Miles0sage/fixcache) — AI Agent Error Memory_")

    return "\n".join(lines)


def main() -> None:
    min_confidence = float(os.environ.get("FIXCACHE_MIN_CONFIDENCE", "0.5"))
    fail_on_no_match = os.environ.get("FIXCACHE_FAIL_ON_NO_MATCH", "false").lower() == "true"

    # Read error text from stdin
    if sys.stdin.isatty():
        write_summary("## fixcache\n\n> No input provided (stdin is a tty). Pipe error text to this script.")
        return

    error_text = sys.stdin.read().strip()
    if not error_text:
        write_summary("## fixcache\n\n> Empty input — nothing to classify.")
        return

    data = classify(error_text)

    if data is None:
        write_summary(
            "## fixcache — Error Memory Report\n\n"
            "> fixcache could not classify this error. "
            "Install with `pip install fixcache` and ensure it is on PATH."
        )
        return

    summary = format_summary(data, min_confidence)
    write_summary(summary)

    # Honour fail-on-no-match but never raise for other reasons
    if fail_on_no_match:
        suggestions = data.get("suggestions", [])
        above_threshold = [s for s in suggestions if s.get("confidence", 0) >= min_confidence]
        if not above_threshold:
            print(
                f"fixcache: no recipes matched above confidence {min_confidence:.0%}. "
                "Set fail-on-no-match: 'false' to suppress this.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # Never exit non-zero from an unexpected error
        write_summary(f"## fixcache\n\n> Internal error: `{exc}`")
