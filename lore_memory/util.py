"""
util.py — Shared utilities for lore_memory.
"""

from __future__ import annotations

import re
import threading


def safe_regex_search(pattern: str, text: str, timeout: float = 0.1) -> bool:
    """Run re.search(pattern, text, IGNORECASE) with a wall-clock timeout.

    Prevents ReDoS: a catastrophically backtracking stored pattern is
    aborted after `timeout` seconds and degrades to a plain case-insensitive
    substring check.  Legitimate patterns (ImportError.*module, etc.) never
    come close to 100 ms even on 16 KB inputs.

    Note: the background thread continues running after timeout until the
    GIL is released naturally — this is a best-effort mitigation, not a
    hard kill.  For higher assurance, use the `regex` package which supports
    a true `timeout` parameter.
    """
    result: list[bool] = [False]

    def _run() -> None:
        try:
            result[0] = bool(re.search(pattern, text, re.IGNORECASE))
        except re.error:
            result[0] = pattern.lower() in text.lower()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return pattern.lower() in text.lower()
    return result[0]
