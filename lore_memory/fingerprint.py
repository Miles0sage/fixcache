"""
fingerprint.py — Normalized, privacy-preserving failure fingerprints.

A fingerprint is a stable, redacted canonical form of an error that
lets lore-memory match, aggregate, and score failures across repos,
languages, and machines — without leaking paths, secrets, or PII.

The fingerprint hash is what makes the Darwin dataset defensible:
competitors can clone the code but they can't clone your corpus of
normalized failures linked to measured fix efficacy.

Design:
    Fingerprint = {
        error_type: "ModuleNotFoundError"
        ecosystem:  "python" | "node" | "rust" | "shell" | "docker" | "unknown"
        tool:       "pytest" | "npm" | "cargo" | ...
        essence:    canonical redacted form of the final error line
        top_frame:  basename of the user code file, or None
    }
    hash = sha256 of `|`-joined canonical form → 16 hex chars

The hash is short enough to be stable/shareable and long enough
(2^64 namespace) to avoid collisions in any realistic corpus.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any


# ── Error type patterns ──────────────────────────────────────────────────────

_ERROR_TYPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(\w*(?:Error|Exception))\b"), "{0}"),
    (re.compile(r"^Traceback \(most recent call last\)"), "PythonTraceback"),
    (re.compile(r"^(FAIL(?:ED)?)\b"), "TestFailure"),
    (re.compile(r"ECONNREFUSED"), "ConnectionRefused"),
    (re.compile(r"ETIMEDOUT"), "ConnectionTimeout"),
    (re.compile(r"command not found"), "CommandNotFound"),
    (re.compile(r"No such file or directory"), "FileNotFound"),
    (re.compile(r"Permission denied"), "PermissionDenied"),
    (re.compile(r"fatal:", re.IGNORECASE), "GitFatal"),
    (re.compile(r"\bexit code\s+(\d+)", re.IGNORECASE), "NonZeroExit"),
]


# ── Ecosystem / tool detection ───────────────────────────────────────────────

# Secondary signal: infer ecosystem from error type name when text-level cues
# fail (e.g. a bare "SyntaxError: ..." signature typed by a user into `fix`
# has no `.py` path or `Traceback` marker, so _ECOSYSTEM_CUES miss it, but
# the full traceback that `watch` captures DOES contain those cues and returns
# "python" — producing a hash mismatch that blocks the primary lookup).
# NOTE: TypeError appears in both Python and Node, but bare "TypeError: ..."
# without a Node stack (`at ... (file.js:N:M)`) defaults to python here, which
# is the more common case. Real Node traces trigger the text-level node cue
# first and win via that path.
_ERROR_TYPE_TO_ECOSYSTEM: dict[str, str] = {
    "ModuleNotFoundError": "python",
    "ImportError": "python",
    "SyntaxError": "python",
    "IndentationError": "python",
    "TabError": "python",
    "NameError": "python",
    "UnboundLocalError": "python",
    "AttributeError": "python",
    "TypeError": "python",
    "ValueError": "python",
    "KeyError": "python",
    "IndexError": "python",
    "RuntimeError": "python",
    "RecursionError": "python",
    "FileNotFoundError": "python",
    "PermissionError": "python",
    "IsADirectoryError": "python",
    "NotADirectoryError": "python",
    "ZeroDivisionError": "python",
    "OverflowError": "python",
    "ArithmeticError": "python",
    "StopIteration": "python",
    "StopAsyncIteration": "python",
    "AssertionError": "python",
    "LookupError": "python",
    "UnicodeError": "python",
    "UnicodeDecodeError": "python",
    "UnicodeEncodeError": "python",
    "OSError": "python",
    "IOError": "python",
    "PythonTraceback": "python",
}

_ECOSYSTEM_CUES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(pytest|python|\.py\b|ModuleNotFoundError|ImportError|Traceback)"), "python"),
    (re.compile(r"\b(npm|yarn|pnpm|node|\.js\b|\.ts\b|\.tsx\b|package\.json)"), "node"),
    (re.compile(r"\b(cargo|rustc|\.rs\b|borrow checker)"), "rust"),
    (re.compile(r"\b(go build|go test|\.go\b|\bgo\s)"), "go"),
    # shell comes before docker so that `bash: docker: command not found`
    # routes to shell (the surrounding context) rather than docker (a word
    # that happens to appear in the error). Real docker errors don't usually
    # have a shell prefix, so this doesn't misroute them.
    (re.compile(r"\b(bash|zsh|sh:|/bin/)"), "shell"),
    (re.compile(r"\b(docker|docker-compose|Dockerfile)"), "docker"),
    (re.compile(r"\b(git\s|fatal:|merge conflict)"), "git"),
]

_TOOL_CUES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bpytest\b"), "pytest"),
    (re.compile(r"\bpip\b"), "pip"),
    (re.compile(r"\bnpm\b"), "npm"),
    (re.compile(r"\byarn\b"), "yarn"),
    (re.compile(r"\bcargo\b"), "cargo"),
    (re.compile(r"\bgo\s+(test|build|run)\b"), "go"),
    (re.compile(r"\bdocker\s+(compose|build|run)\b"), "docker"),
    (re.compile(r"\bgit\b"), "git"),
    (re.compile(r"\bmake\b"), "make"),
]


# ── Redaction patterns ───────────────────────────────────────────────────────

# Targeted patterns: canonicalize common error shapes so that specific
# identifiers (module names, attribute names, type names, filenames) collapse
# to generic placeholders. These run BEFORE the generic redactors so that
# class-specific structure survives but value-specific tokens do not.
#
# The ordering matters: more specific patterns come first.
_TARGETED_REDACTORS: list[tuple[re.Pattern[str], str]] = [
    # ── Python ──────────────────────────────────────────────────────────────
    (re.compile(r"No module named ['\"][^'\"]*['\"]"), "No module named '<mod>'"),
    (
        re.compile(r"cannot import name ['\"][^'\"]*['\"] from ['\"][^'\"]*['\"](\s*\([^)]*\))?"),
        "cannot import name '<name>' from '<mod>'",
    ),
    (re.compile(r"cannot import name ['\"][^'\"]*['\"]"), "cannot import name '<name>'"),
    (
        re.compile(r"['\"][^'\"]+['\"] object has no attribute ['\"][^'\"]*['\"]"),
        "'<type>' object has no attribute '<attr>'",
    ),
    (
        re.compile(r"['\"][^'\"]+['\"] object is not subscriptable"),
        "'<type>' object is not subscriptable",
    ),
    (re.compile(r"['\"][^'\"]+['\"] object is not iterable"), "'<type>' object is not iterable"),
    (re.compile(r"['\"][^'\"]+['\"] object is not callable"), "'<type>' object is not callable"),
    # ── Node / JS ───────────────────────────────────────────────────────────
    (re.compile(r"Cannot find module ['\"][^'\"]*['\"]"), "Cannot find module '<mod>'"),
    (re.compile(r"\b\w+ is not a function\b"), "<name> is not a function"),
    (re.compile(r"\b\w+ is not defined\b"), "<name> is not defined"),
    # ── Go ──────────────────────────────────────────────────────────────────
    (re.compile(r"undefined: \w+"), "undefined: <name>"),
    # ── Rust ────────────────────────────────────────────────────────────────
    (re.compile(r"unused import: `[^`]+`"), "unused import: `<name>`"),
    (re.compile(r"cannot find `\w+` in this scope"), "cannot find `<name>` in this scope"),
    # ── Shell ───────────────────────────────────────────────────────────────
    # NOTE: \S{1,200} is bounded — the bare `\S+` variant took 195ms on
    # a 10k-char adversarial input (catastrophic backtracking landed in
    # the security hardening suite). Release blocker if attacker-controlled.
    (re.compile(r"\S{1,200}: command not found"), "<cmd>: command not found"),
    # ── Filesystem ──────────────────────────────────────────────────────────
    (re.compile(r"\S{1,200}: No such file or directory"), "<path>: No such file or directory"),
    # ── Secrets & tokens ────────────────────────────────────────────────────
    # Redact common API-key shapes even when they appear unquoted in the
    # wild. Runs before the generic _QUOTED redactor so that bare tokens
    # like `token=ghp_...` or `Authorization: Bearer sk-...` never reach
    # the fingerprint essence or the Darwin export corpus.
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "<github_token>"),
    (re.compile(r"gho_[A-Za-z0-9]{20,}"), "<github_token>"),
    (re.compile(r"ghs_[A-Za-z0-9]{20,}"), "<github_token>"),
    (re.compile(r"ghu_[A-Za-z0-9]{20,}"), "<github_token>"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "<github_token>"),
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), "<google_api_key>"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<openai_key>"),
    (re.compile(r"xoxb-[A-Za-z0-9-]{20,}"), "<slack_bot_token>"),
    (re.compile(r"xoxp-[A-Za-z0-9-]{20,}"), "<slack_user_token>"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "<gitlab_token>"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "<aws_access_key>"),
    (re.compile(r"(?<![@\w])[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
    # ── Relative paths (./foo.go, ./src/main.rs, ./foo.go:12:9) ────────────
    (re.compile(r"\./[\w./-]+\.[a-z]+(?::\d+)?(?::\d+)?"), "./<file>"),
]

# Strip file paths: /any/abs/path/foo.py → foo.py
# Factored to avoid the catastrophic backtracking that the 22-second
# hardening test caught on a 50k-segment path: segments now exclude `/`,
# and the trailing `/` is matched once after the inner `+` group rather
# than as part of each iteration. This makes each iteration consume a
# unique `/<segment>` pair — linear time, no nested-quantifier hazard —
# while still collapsing `/abs/path/secret.py` to `<p>/secret.py`.
_ABS_PATH = re.compile(r"(?:/[^\s:/'\"`]+){1,20}/([^/\s:'\"`]+)")
# Strip line/column numbers: file.py:42:8 → file.py:<L>:<C>
_LINE_COL = re.compile(r"(\.[a-z]+):(\d+)(:(\d+))?")
# Strip hex-looking IDs (UUIDs, hashes, pointer addresses like 0xdeadbeef).
# Handles both 0x-prefixed addresses and bare hex strings of length 8+.
_HEX_ID = re.compile(r"(?:0x[0-9a-f]+|\b[0-9a-f]{8,}\b)", re.IGNORECASE)
# Strip specific numeric values that are usually noise
_NUMBER = re.compile(r"\b\d{3,}\b")
# Strip quoted literals (often contain secrets or user-specific values).
# Two separate patterns — one for single-quoted, one for double-quoted —
# because a single combined pattern of the form (['"])([^'"]{8,})\1 refuses
# to match a value that contains the *other* kind of quote. Real-world
# Python tracebacks commonly carry apostrophes inside strings (`can't`,
# `doesn't`, object repr strings), which made the combined pattern leak
# secrets like 'it\'s_a_secret'. Property-based fuzz tests surfaced this
# as a privacy regression; fix landed as part of v0.4.0 hardening.
# `<`/`>` are excluded from the content class so the generic quoted
# redactor does not eat placeholders emitted by the targeted redactors.
# Without that exclusion, a canonical essence like
#     'NoneType' object has no attribute 'split'
# → targeted → 'NoneType' → '<type>' and 'split' → '<attr>'
# → generic  → '<type>'<val>'<attr>'   (the whole thing collapses)
# Excluding `<>` leaves already-redacted tokens alone.
_QUOTED_SINGLE = re.compile(r"'([^'<>]{8,})'")
_QUOTED_DOUBLE = re.compile(r'"([^"<>]{8,})"')


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fingerprint:
    error_type: str
    ecosystem: str
    tool: str
    essence: str
    top_frame: str | None
    hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _detect_error_type(line: str) -> str:
    for pat, label in _ERROR_TYPE_PATTERNS:
        m = pat.search(line)
        if m:
            if "{0}" in label:
                return m.group(1)
            return label
    return "Unknown"


def _detect_ecosystem(text: str, error_type: str | None = None) -> str:
    for pat, eco in _ECOSYSTEM_CUES:
        if pat.search(text):
            return eco
    # Fallback: infer from the error type name when text-level cues all miss.
    # This fixes the bare-signature → hash mismatch: a user typing
    # "SyntaxError: ..." into `fix` produces no .py/Traceback cue, but the
    # full traceback that `watch` captures does — same error class, same hash.
    if error_type and error_type in _ERROR_TYPE_TO_ECOSYSTEM:
        return _ERROR_TYPE_TO_ECOSYSTEM[error_type]
    return "unknown"


def _detect_tool(text: str) -> str:
    for pat, tool in _TOOL_CUES:
        if pat.search(text):
            return tool
    return "unknown"


def _extract_top_frame(text: str) -> str | None:
    """
    Best-effort extraction of the user's top stack frame basename.
    Only returns a filename (not a path) for privacy.
    """
    # Python tracebacks: `File "/abs/path/foo.py", line 42`
    m = re.search(r'File\s+"([^"]+)"', text)
    if m:
        path = m.group(1)
        return path.rsplit("/", 1)[-1]
    # Node stack: `at fn (/abs/path/foo.js:10:5)`
    m = re.search(r"at\s+\S+\s*\(([^:)]+)", text)
    if m:
        path = m.group(1)
        return path.rsplit("/", 1)[-1]
    # Rust error: `--> src/main.rs:42:5`
    m = re.search(r"-->\s*([^:\s]+)", text)
    if m:
        path = m.group(1)
        return path.rsplit("/", 1)[-1]
    return None


def _redact(text: str) -> str:
    """Strip paths, IDs, big numbers, and long quoted literals.

    Applies targeted error-shape canonicalization first, then generic
    path/id/number/quoted redaction. Targeted patterns preserve the
    *shape* of common errors while collapsing specific identifiers,
    which is what lets sklearn/pandas/numpy all map to one fingerprint.
    """
    s = text
    for pat, replacement in _TARGETED_REDACTORS:
        s = pat.sub(replacement, s)
    s = _ABS_PATH.sub(r"<p>/\1", s)
    s = _LINE_COL.sub(r"\1:<L>", s)
    s = _HEX_ID.sub("<id>", s)
    s = _NUMBER.sub("<n>", s)
    s = _QUOTED_SINGLE.sub("'<val>'", s)
    s = _QUOTED_DOUBLE.sub('"<val>"', s)
    return s


def _pick_final_line(text: str) -> str:
    """Pick the most informative single line from a multi-line error.

    Preference order (highest first):
    1. Last line where an error pattern matches at the START of the line
       AND the match is not ``FAIL``/``FAILED`` (a pytest summary prefix).
       e.g. ``ModuleNotFoundError: No module named 'foo'`` from a traceback.
    2. Last line where an error pattern appears anywhere — but if the line is
       a pytest-style ``FAILED <path> - <ErrorType>: …`` line, strip the prefix
       and return only the ``<ErrorType>: …`` suffix so the fingerprint matches
       the bare error-type form that ``lore fix`` stores.
    3. The last non-empty line.

    The pytest-prefix stripping is the critical fix for the ``fix``→``watch``
    primary-lookup mismatch: without it, ``watch`` computes a different hash
    for ``FAILED tests/foo.py - ModuleNotFoundError: …`` than ``fix`` stores
    for ``ModuleNotFoundError: No module named 'foo'``, so the fingerprint-hash
    index never fires and the LIKE-fallback rescues only ~50% of cases.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    # Pass 1: last line where a non-FAILED error pattern anchors at position 0
    for line in reversed(lines):
        for pat, label in _ERROR_TYPE_PATTERNS:
            m = pat.search(line)
            if m and m.start() == 0 and label != "TestFailure":
                return line

    # Pass 2: last line where any error pattern matches — strip pytest prefix
    for line in reversed(lines):
        for pat, _ in _ERROR_TYPE_PATTERNS:
            if pat.search(line):
                # Pytest FAILED summary: "FAILED path/test.py::fn - ErrorType: msg"
                # Strip everything up to and including " - " to get the bare error.
                sep = " - "
                idx = line.find(sep)
                if idx != -1:
                    suffix = line[idx + len(sep):]
                    # Only use suffix if it itself looks like an error line
                    for p2, _ in _ERROR_TYPE_PATTERNS:
                        if p2.search(suffix):
                            return suffix
                return line

    return lines[-1]


# ── Public API ───────────────────────────────────────────────────────────────

#: Hard cap on the input text length. Anything bigger is truncated to the
#: last ``_MAX_INPUT_BYTES`` — the tail is where the actual error almost
#: always lives (tracebacks are bottom-heavy). Guards against DoS via
#: 100 MB payloads that took ~24 seconds to redact in the pre-hardening
#: fingerprinter. Chosen to fit every realistic agent stderr blob.
_MAX_INPUT_BYTES = 65_536


def compute_fingerprint(error_text: str) -> Fingerprint:
    """
    Compute a normalized, privacy-preserving fingerprint from raw error text.

    The same fingerprint hash should fire for semantically-equivalent errors
    across repos, machines, and versions.
    """
    text = error_text or ""
    if len(text) > _MAX_INPUT_BYTES:
        # Keep the tail — errors live at the bottom of stack traces
        text = text[-_MAX_INPUT_BYTES:]
    final_line = _pick_final_line(text)
    essence = _redact(final_line)[:200]

    error_type = _detect_error_type(final_line)
    ecosystem = _detect_ecosystem(text, error_type=error_type)
    tool = _detect_tool(text)
    top_frame = _extract_top_frame(text)

    # top_frame is intentionally excluded from the canonical hash:
    # including it made different filenames produce different fingerprints,
    # which defeated cross-repo collapse. It's still returned on the
    # Fingerprint dataclass for display and debugging.
    canonical = "|".join(
        [
            error_type,
            ecosystem,
            tool,
            essence,
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return Fingerprint(
        error_type=error_type,
        ecosystem=ecosystem,
        tool=tool,
        essence=essence,
        top_frame=top_frame,
        hash=digest,
    )


def fingerprint_hash(error_text: str) -> str:
    """Convenience: return only the fingerprint hash."""
    return compute_fingerprint(error_text).hash
