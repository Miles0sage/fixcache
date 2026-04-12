"""
ingest.py — Claude Code transcript ingestion for auto-capture of fix recipes.

Parses a Claude Code session JSONL file, finds error→fix windows, and
stores them as Darwin patterns via the same pipeline as `lore-memory fix`.

Usage:
    from lore_memory.ingest import ingest_transcript
    result = ingest_transcript(store, "/path/to/session.jsonl")

The extractor is intentionally simple: find tool_result blocks that look
like errors (is_error=True or matching patterns), then collect the
assistant's next few tool_use actions as the "fix", capped until the
next successful tool_result with no error signals. This yields noisy
but high-recall recipes that the Bayesian confidence loop can prune.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


# ── Error detection patterns ──────────────────────────────────────────────────

# Anchor patterns at line start (MULTILINE) so source-code substrings
# like `print("An error: ...")` or `except ValueError:` don't match.
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE),
    re.compile(r"^\w*(Error|Exception)\b.*:", re.MULTILINE),
    re.compile(r"^(FAILED|FAIL)\b", re.MULTILINE),
    re.compile(r"^ModuleNotFoundError", re.MULTILINE),
    re.compile(r"^ImportError", re.MULTILINE),
    re.compile(r"^SyntaxError", re.MULTILINE),
    re.compile(r"^TypeError", re.MULTILINE),
    re.compile(r"^AttributeError", re.MULTILINE),
    re.compile(r"^FileNotFoundError", re.MULTILINE),
    re.compile(r"^NameError", re.MULTILINE),
    re.compile(r"^ValueError", re.MULTILINE),
    re.compile(r"^KeyError", re.MULTILINE),
    re.compile(r"^PermissionError", re.MULTILINE),
    re.compile(r"^RuntimeError", re.MULTILINE),
    re.compile(r"^AssertionError", re.MULTILINE),
    re.compile(r"command not found"),
    re.compile(r"No such file or directory"),
    re.compile(r"ECONNREFUSED"),
    re.compile(r"ETIMEDOUT"),
    re.compile(r"\bexit code\s+[1-9]"),
    re.compile(r"^fatal:", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^error:", re.MULTILINE | re.IGNORECASE),
]

# Regex that matches a Read-tool-style line: "   123\tsome source code"
_LINE_NUMBERED_CODE = re.compile(r"^\s*\d+\t", re.MULTILINE)

_MAX_SIGNATURE_LEN = 240
_MAX_STEPS_PER_RECIPE = 8
_MIN_STEPS_TO_STORE = 1


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FixRecipe:
    """A (error_signature → solution_steps) recipe extracted from transcript."""

    error_signature: str
    solution_steps: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_file: str = ""
    transcript_line: int = 0


# ── Transcript parser ─────────────────────────────────────────────────────────

def iter_messages(path: str | Path) -> Iterator[dict[str, Any]]:
    """
    Yield parsed JSONL messages from a Claude Code session transcript.

    Skips malformed lines silently — transcripts sometimes have
    truncated or partial writes at the tail.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Transcript not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ── Extraction helpers ────────────────────────────────────────────────────────

def _tool_result_text(block: dict[str, Any]) -> str:
    """Extract plain text from a tool_result content block."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                parts.append(sub.get("text", ""))
            elif isinstance(sub, str):
                parts.append(sub)
        return "\n".join(parts)
    return ""


def _looks_like_error(text: str, is_error_flag: bool = False) -> bool:
    """
    Heuristic: does this tool_result text contain an error signature?
    Checks both the explicit is_error flag and text patterns.

    Rejects text that appears to be source-code listings (many lines
    prefixed with "<lineno>\t") — Read-tool output often contains
    words like "error" or "FAILED" as part of the code itself.
    """
    if is_error_flag:
        return True
    if not text:
        return False

    # Skip if the text looks like Read tool output (line-numbered code).
    # More than 5 line-numbered lines → treat as code listing, not error.
    if len(_LINE_NUMBERED_CODE.findall(text)) > 5:
        return False

    for pat in _ERROR_PATTERNS:
        if pat.search(text):
            return True
    return False


def _extract_error_signature(text: str) -> str:
    """
    Pick the most informative single line from an error blob and
    trim it to _MAX_SIGNATURE_LEN characters.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    # Prefer the last line that matches an error pattern (usually the actual error)
    best = ""
    for line in reversed(lines):
        for pat in _ERROR_PATTERNS:
            if pat.search(line):
                best = line
                break
        if best:
            break
    if not best:
        best = lines[-1]

    # Strip absolute paths for privacy/portability
    best = re.sub(r"/[^\s:]+/([^/\s]+)", r".../\1", best)
    # Collapse whitespace
    best = re.sub(r"\s+", " ", best).strip()
    return best[:_MAX_SIGNATURE_LEN]


def _assistant_tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the tool_use content blocks from an assistant message."""
    msg = message.get("message", {})
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def _describe_tool_use(block: dict[str, Any]) -> str:
    """One-line human description of a tool_use block."""
    name = block.get("name", "unknown_tool")
    inp = block.get("input") or {}

    # Bash commands — include the command
    if name == "Bash":
        cmd = inp.get("command", "")
        if cmd:
            cmd = cmd.replace("\n", " ")[:160]
            return f"Run: {cmd}"

    # Edit / Write — include the file path
    if name in ("Edit", "Write"):
        path = inp.get("file_path", "")
        if path:
            # strip absolute root
            short = re.sub(r"^/[^/]+/", "", path)
            return f"{name} {short}"

    # Read — include the file path
    if name == "Read":
        path = inp.get("file_path", "")
        if path:
            short = re.sub(r"^/[^/]+/", "", path)
            return f"Read {short}"

    # Generic fallback
    return f"Used {name}"


# ── Core extractor ────────────────────────────────────────────────────────────

def extract_fix_recipes(
    messages: list[dict[str, Any]],
    source_file: str = "",
) -> list[FixRecipe]:
    """
    Walk the message stream and emit FixRecipe for each error → fix window.

    Algorithm:
      1. Find tool_result blocks flagged as errors (is_error=True or matching patterns)
      2. For each error, collect subsequent assistant tool_use actions
      3. Stop when we hit the next successful (non-error) tool_result
         OR we've collected _MAX_STEPS_PER_RECIPE steps
         OR we hit a new user message
      4. Emit a FixRecipe if we have at least _MIN_STEPS_TO_STORE steps.
    """
    recipes: list[FixRecipe] = []
    n = len(messages)
    i = 0
    while i < n:
        msg = messages[i]
        msg_type = msg.get("type")

        # Look for tool_result blocks in user messages (they carry results)
        if msg_type == "user":
            content = msg.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    is_err = bool(block.get("is_error", False))
                    text = _tool_result_text(block)
                    if not _looks_like_error(text, is_error_flag=is_err):
                        continue

                    signature = _extract_error_signature(text)
                    if not signature:
                        continue

                    # Walk forward collecting assistant tool_use actions
                    steps: list[str] = []
                    j = i + 1
                    while j < n and len(steps) < _MAX_STEPS_PER_RECIPE:
                        nxt = messages[j]
                        nxt_type = nxt.get("type")

                        if nxt_type == "user":
                            # New user prompt — stop unless it's a tool_result
                            nxt_content = nxt.get("message", {}).get("content")
                            has_tool_result = (
                                isinstance(nxt_content, list)
                                and any(
                                    isinstance(b, dict) and b.get("type") == "tool_result"
                                    for b in nxt_content
                                )
                            )
                            if not has_tool_result:
                                break
                            # Check: did the next tool_result also error?
                            all_ok = True
                            for b in nxt_content:
                                if (
                                    isinstance(b, dict)
                                    and b.get("type") == "tool_result"
                                    and _looks_like_error(
                                        _tool_result_text(b),
                                        is_error_flag=bool(b.get("is_error", False)),
                                    )
                                ):
                                    all_ok = False
                                    break
                            if all_ok and steps:
                                # Error resolved — stop collecting
                                break
                        elif nxt_type == "assistant":
                            for tu in _assistant_tool_uses(nxt):
                                desc = _describe_tool_use(tu)
                                if desc and desc not in steps:
                                    steps.append(desc)
                                    if len(steps) >= _MAX_STEPS_PER_RECIPE:
                                        break
                        j += 1

                    if len(steps) >= _MIN_STEPS_TO_STORE:
                        recipes.append(
                            FixRecipe(
                                error_signature=signature,
                                solution_steps=steps,
                                tags=["claude-code", "auto-ingest"],
                                source_file=source_file,
                                transcript_line=i,
                            )
                        )
        i += 1
    return recipes


# ── Storage bridge ────────────────────────────────────────────────────────────

def _store_recipe(store: Any, recipe: FixRecipe) -> str:
    """
    Insert a FixRecipe into darwin_journal + darwin_patterns + memories.
    Mirrors handle_lore_fix transaction semantics.
    """
    now = time.time()
    recipe_id = str(uuid.uuid4())
    pattern_id = str(uuid.uuid4())
    steps_json = json.dumps(recipe.solution_steps)
    meta_json = json.dumps(
        {
            "tags": recipe.tags,
            "recipe_id": recipe_id,
            "source": "claude-code-ingest",
            "source_file": recipe.source_file,
            "transcript_line": recipe.transcript_line,
        }
    )
    description = f"Fix for: {recipe.error_signature[:120]}"
    steps_text = "\n".join(
        f"{i+1}. {s}" for i, s in enumerate(recipe.solution_steps)
    )
    mem_content = (
        f"ERROR FIX (auto-ingested): {recipe.error_signature}\n"
        f"SOLUTION:\n{steps_text}"
    )

    store.conn.execute("BEGIN")
    try:
        store.wal.record(
            "INSERT",
            "darwin_journal",
            record_id=recipe_id,
            data={
                "error_signature": recipe.error_signature,
                "solution_steps": recipe.solution_steps,
                "source": "claude-code-ingest",
            },
        )
        store.conn.execute(
            """
            INSERT INTO darwin_journal
                (id, query, result_ids, outcome, correction, timestamp, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_id,
                recipe.error_signature,
                recipe_id,
                "success",
                steps_json,
                now,
                meta_json,
            ),
        )
        store.wal.record(
            "INSERT",
            "darwin_patterns",
            record_id=pattern_id,
            data={"error_signature": recipe.error_signature},
        )
        store.conn.execute(
            """
            INSERT INTO darwin_patterns
                (id, pattern_type, description, rule, frequency, confidence,
                 created_at, last_triggered)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pattern_id,
                "error_recipe",
                description,
                steps_json,
                1,
                0.4,  # slightly lower than manual — these are auto-ingested
                now,
                now,
            ),
        )
        mem_id = str(uuid.uuid4())
        mem_meta = json.dumps(
            {
                "recipe_id": recipe_id,
                "pattern_id": pattern_id,
                "error_signature": recipe.error_signature,
                "tags": recipe.tags,
                "trust_score": 0.6,  # mined from agent transcript
                "source_type": "mined",
                "source": "claude-code-ingest",
            }
        )
        store.wal.record(
            "INSERT",
            "memories",
            record_id=mem_id,
            data={"content": mem_content},
        )
        store.conn.execute(
            """
            INSERT INTO memories
                (id, content, memory_type, source_format, created_at,
                 drawer_id, chunk_index, parent_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mem_id,
                mem_content,
                "experience",
                "claude-code-transcript",
                now,
                None,
                None,
                None,
                mem_meta,
            ),
        )
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    return pattern_id


# ── Public entry point ────────────────────────────────────────────────────────

def ingest_transcript(
    store: Any,
    path: str | Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Parse a Claude Code transcript and store extracted fix recipes.

    Args:
        store: A MemoryStore (the same one used by MCP/CLI).
        path: Absolute path to the transcript JSONL file.
        dry_run: If True, extract recipes but do not store them.

    Returns:
        Report dict with counts and the extracted recipes.
    """
    messages = list(iter_messages(path))
    recipes = extract_fix_recipes(messages, source_file=str(path))

    stored_ids: list[str] = []
    if not dry_run:
        for r in recipes:
            pid = _store_recipe(store, r)
            stored_ids.append(pid)

    return {
        "path": str(path),
        "total_messages": len(messages),
        "recipes_extracted": len(recipes),
        "recipes_stored": len(stored_ids),
        "dry_run": dry_run,
        "pattern_ids": stored_ids,
        "recipes": [
            {
                "error_signature": r.error_signature,
                "solution_steps": r.solution_steps,
                "tags": r.tags,
            }
            for r in recipes
        ],
    }


def find_latest_transcript() -> Path | None:
    """
    Locate the most recently-modified Claude Code transcript under
    ~/.claude/projects/**/sessions/*.jsonl (or the project dir directly).
    Returns None if no transcripts are found.
    """
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return None
    candidates = list(base.rglob("*.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)
