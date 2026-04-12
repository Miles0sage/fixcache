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

_ECOSYSTEM_CUES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(pytest|python|\.py\b|ModuleNotFoundError|ImportError|Traceback)"), "python"),
    (re.compile(r"\b(npm|yarn|pnpm|node|\.js\b|\.ts\b|\.tsx\b|package\.json)"), "node"),
    (re.compile(r"\b(cargo|rustc|\.rs\b|borrow checker)"), "rust"),
    (re.compile(r"\b(go build|go test|\.go\b|\bgo\s)"), "go"),
    (re.compile(r"\b(docker|docker-compose|Dockerfile)"), "docker"),
    (re.compile(r"\b(git\s|fatal:|merge conflict)"), "git"),
    (re.compile(r"\b(bash|zsh|sh:|/bin/)"), "shell"),
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

# Strip file paths: /any/abs/path/foo.py → foo.py
_ABS_PATH = re.compile(r"(?:/[^\s:'\"`]+/)+([^/\s:'\"`]+)")
# Strip line/column numbers: file.py:42:8 → file.py:<L>:<C>
_LINE_COL = re.compile(r"(\.[a-z]+):(\d+)(:(\d+))?")
# Strip hex-looking IDs (UUIDs, hashes, pointer addresses like 0xdeadbeef).
# Handles both 0x-prefixed addresses and bare hex strings of length 8+.
_HEX_ID = re.compile(r"(?:0x[0-9a-f]+|\b[0-9a-f]{8,}\b)", re.IGNORECASE)
# Strip specific numeric values that are usually noise
_NUMBER = re.compile(r"\b\d{3,}\b")
# Strip quoted literals (often contain secrets or user-specific values)
_QUOTED = re.compile(r"(['\"])([^'\"]{8,})\1")


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


def _detect_ecosystem(text: str) -> str:
    for pat, eco in _ECOSYSTEM_CUES:
        if pat.search(text):
            return eco
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
    """Strip paths, IDs, big numbers, and long quoted literals."""
    s = text
    s = _ABS_PATH.sub(r"<p>/\1", s)
    s = _LINE_COL.sub(r"\1:<L>", s)
    s = _HEX_ID.sub("<id>", s)
    s = _NUMBER.sub("<n>", s)
    s = _QUOTED.sub(r"\1<val>\1", s)
    return s


def _pick_final_line(text: str) -> str:
    """Pick the most informative single line from a multi-line error."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    # Prefer the last line matching an error pattern (that's usually the punchline)
    for line in reversed(lines):
        for pat, _ in _ERROR_TYPE_PATTERNS:
            if pat.search(line):
                return line
    return lines[-1]


# ── Public API ───────────────────────────────────────────────────────────────

def compute_fingerprint(error_text: str) -> Fingerprint:
    """
    Compute a normalized, privacy-preserving fingerprint from raw error text.

    The same fingerprint hash should fire for semantically-equivalent errors
    across repos, machines, and versions.
    """
    text = error_text or ""
    final_line = _pick_final_line(text)
    essence = _redact(final_line)[:200]

    error_type = _detect_error_type(final_line)
    ecosystem = _detect_ecosystem(text)
    tool = _detect_tool(text)
    top_frame = _extract_top_frame(text)

    canonical = "|".join(
        [
            error_type,
            ecosystem,
            tool,
            top_frame or "-",
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
