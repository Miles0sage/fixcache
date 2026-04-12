#!/usr/bin/env python3
"""
mcp/server.py — lore-memory MCP server (stdio JSON-RPC transport).

6 tools:
  lore_remember        — attested storage with provenance + trust scoring
  lore_recall          — verified BM25 retrieval with trust threshold
  lore_fix             — store error recipes in procedural memory
  lore_match_procedure — pattern-matched procedure retrieval
  lore_teach           — store conventions / rules as facts
  lore_stats           — memory system statistics

Transport: raw JSON-RPC 2.0 over stdin/stdout (no MCP SDK dependency).
All logging goes to stderr. stdout is exclusively JSON-RPC.

Entry point: lore-memory-mcp (see pyproject.toml)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from ..core.store import MemoryStore
from ..darwin import consolidate, evolve_patterns, log_outcome, update_confidence
from ..darwin_replay import (
    classify as darwin_classify,
    darwin_stats,
    export_sanitized,
    record_outcome as replay_record_outcome,
    upsert_fingerprint,
)
from ..fingerprint import compute_fingerprint
from ..layers.identity import IdentityLayer
# prefetch module moved to _graveyard/ in lore-memory-lite — stub to keep imports clean
def generate_briefing(*_a, **_kw): return {}
def record_access(*_a, **_kw): pass
from .tools import TOOL_SCHEMAS, TRUST_SCORES, TIME_WINDOWS

# ── Logging — always to stderr ────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
logger = logging.getLogger("lore_memory_mcp")

# ── Default DB path ───────────────────────────────────────────────────────────
_DEFAULT_DB = str(Path.home() / ".lore-memory" / "default.db")

# ── Supported MCP protocol versions ──────────────────────────────────────────
SUPPORTED_PROTOCOL_VERSIONS = [
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]

# ── Module-level store (lazy-initialised) ─────────────────────────────────────
_store: MemoryStore | None = None
_identity: IdentityLayer | None = None


def _get_store() -> MemoryStore:
    global _store, _identity
    if _store is None:
        db_path = os.environ.get("LORE_MEMORY_DB", _DEFAULT_DB)
        _store = MemoryStore(db_path)
        _identity = IdentityLayer(_store)
        logger.info("lore-memory store opened: %s", db_path)
    return _store


def _get_identity() -> IdentityLayer:
    global _identity
    store = _get_store()  # ensure initialised
    if _identity is None:
        _identity = IdentityLayer(store)
    return _identity


# ── Provenance hash ───────────────────────────────────────────────────────────

def _provenance_hash(content: str, timestamp: float) -> str:
    """SHA-256 of content + timestamp string."""
    raw = f"{content}{timestamp}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── Tool handlers ─────────────────────────────────────────────────────────────

def handle_lore_remember(
    content: str,
    source_type: str = "agent",
    memory_type: str = "fact",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Attested storage with provenance tracking and trust scoring."""
    if not content or not isinstance(content, str):
        return {"success": False, "error": "content must be a non-empty string"}
    store = _get_store()

    if source_type not in TRUST_SCORES:
        source_type = "agent"
    trust_score = TRUST_SCORES[source_type]

    now = time.time()
    prov_hash = _provenance_hash(content, now)

    metadata: dict[str, Any] = {
        "source_type": source_type,
        "trust_score": trust_score,
        "provenance_hash": prov_hash,
    }
    if tags:
        metadata["tags"] = tags

    mid = store.add(
        content=content,
        memory_type=memory_type,
        metadata=metadata,
    )
    # WAL already recorded by store.add — no extra call needed

    return {
        "success": True,
        "memory_id": mid,
        "trust_score": trust_score,
        "provenance_hash": prov_hash,
        "memory_type": memory_type,
        "source_type": source_type,
    }


def handle_lore_recall(
    query: str,
    top_k: int = 5,
    min_trust: float = 0.5,
    time_window: str | None = None,
    memory_type: str | None = None,
) -> dict[str, Any]:
    """Verified BM25 retrieval with trust threshold and time window."""
    if not query or not isinstance(query, str):
        return {"query": query, "results": [], "count": 0, "error": "query must be a non-empty string"}
    if not isinstance(top_k, int) or top_k < 1:
        return {"query": query, "results": [], "count": 0, "error": "top_k must be a positive integer"}
    if time_window is not None and time_window not in TIME_WINDOWS:
        return {"query": query, "results": [], "count": 0, "error": f"time_window must be one of {list(TIME_WINDOWS)}"}
    store = _get_store()

    since: float | None = None
    if time_window is not None:
        window_secs = TIME_WINDOWS.get(time_window)
        if window_secs is not None:
            since = time.time() - window_secs

    if since is not None:
        raw_results = store.search_temporal(
            query, since=since, top_k=top_k * 3
        )
    else:
        raw_results = store.search(
            query, top_k=top_k * 3, memory_type=memory_type
        )

    # Filter by trust score
    filtered: list[dict[str, Any]] = []
    for mem in raw_results:
        meta = mem.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        trust = meta.get("trust_score", 1.0)
        if trust >= min_trust:
            if memory_type and mem.get("memory_type") != memory_type:
                continue
            filtered.append(mem)
        if len(filtered) >= top_k:
            break

    # Touch accessed memories
    for mem in filtered:
        store.touch(mem["id"])

    # Record access pattern for Prefetcher (silent — never blocks recall)
    if filtered:
        recalled_ids = [m["id"] for m in filtered]
        try:
            record_access(store, recalled_ids, tool_used="lore_recall")
        except Exception:
            pass  # prefetch recording must never break recall

    # Annotate with layer attribution
    results_out: list[dict[str, Any]] = []
    for mem in filtered:
        meta = mem.get("metadata") or {}
        results_out.append({
            "id": mem["id"],
            "content": mem["content"],
            "memory_type": mem.get("memory_type"),
            "trust_score": meta.get("trust_score", 1.0) if isinstance(meta, dict) else 1.0,
            "source_type": meta.get("source_type", "unknown") if isinstance(meta, dict) else "unknown",
            "provenance_hash": meta.get("provenance_hash") if isinstance(meta, dict) else None,
            "layer": "L1",
            "created_at": mem.get("created_at"),
            "access_count": mem.get("access_count", 0),
        })

    return {
        "query": query,
        "results": results_out,
        "count": len(results_out),
        "min_trust_applied": min_trust,
        "time_window": time_window,
    }


def handle_lore_fix(
    error_signature: str,
    solution_steps: list[str],
    tags: list[str] | None = None,
    outcome: str = "success",
) -> dict[str, Any]:
    """Store an error recipe in procedural memory via darwin_journal + darwin_patterns."""
    if not error_signature or not isinstance(error_signature, str):
        return {"success": False, "error": "error_signature must be a non-empty string"}
    if not solution_steps or not isinstance(solution_steps, list):
        return {"success": False, "error": "solution_steps must be a non-empty list"}

    store = _get_store()
    now = time.time()

    recipe_id = str(uuid.uuid4())
    steps_text = "\n".join(f"{i+1}. {s}" for i, s in enumerate(solution_steps))
    tags_str = ",".join(tags) if tags else ""

    # Validate outcome
    valid_outcomes = ("success", "failure", "partial", "corrected")
    if outcome not in valid_outcomes:
        outcome = "success"

    steps_json = json.dumps(solution_steps)

    # Compute normalized fingerprint for Darwin Replay efficacy tracking
    fp = compute_fingerprint(error_signature)
    meta_json = json.dumps(
        {
            "tags": tags or [],
            "recipe_id": recipe_id,
            "fingerprint_hash": fp.hash,
            "fingerprint_error_type": fp.error_type,
            "fingerprint_ecosystem": fp.ecosystem,
        }
    )

    pattern_id = str(uuid.uuid4())
    description = f"Fix for: {error_signature[:120]}"
    rule = steps_json  # rule stores the solution_steps JSON

    mem_content = f"ERROR FIX: {error_signature}\nSOLUTION:\n{steps_text}"
    if tags_str:
        mem_content += f"\nTAGS: {tags_str}"

    # Upsert the fingerprint first so darwin_patterns can link to it.
    # This is idempotent and cheap; safe to run outside the transaction.
    upsert_fingerprint(store, error_signature)

    # Wrap all inserts in an explicit transaction for atomicity
    store.conn.execute("BEGIN")
    try:
        # Store in darwin_journal
        store.wal.record(
            "INSERT", "darwin_journal", record_id=recipe_id,
            data={"error_signature": error_signature, "solution_steps": solution_steps, "tags": tags},
        )
        store.conn.execute(
            """
            INSERT INTO darwin_journal (id, query, result_ids, outcome, correction, timestamp, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (recipe_id, error_signature, recipe_id, outcome, steps_json, now, meta_json),
        )

        # Also store as a darwin_pattern for fast regex matching later
        store.wal.record(
            "INSERT", "darwin_patterns", record_id=pattern_id,
            data={"error_signature": error_signature, "tags": tags},
        )
        store.conn.execute(
            """
            INSERT INTO darwin_patterns
                (id, pattern_type, description, rule, frequency, confidence,
                 created_at, last_triggered, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pattern_id, "error_recipe", description, rule, 1, 0.5, now, now, meta_json),
        )

        # Also store in memories for FTS5 fallback
        # store.add() calls conn.commit() internally, so we call it after the
        # explicit inserts above to keep everything in one transaction.
        mem_meta_json = json.dumps({
            "recipe_id": recipe_id,
            "pattern_id": pattern_id,
            "error_signature": error_signature,
            "tags": tags or [],
            "trust_score": TRUST_SCORES["agent"],
            "source_type": "agent",
            "provenance_hash": _provenance_hash(mem_content, now),
        })
        mem_id = str(uuid.uuid4())
        store.wal.record("INSERT", "memories", record_id=mem_id, data={"content": mem_content})
        store.conn.execute(
            """
            INSERT INTO memories
                (id, content, memory_type, source_format, created_at,
                 drawer_id, chunk_index, parent_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (mem_id, mem_content, "experience", None, now, None, None, None, mem_meta_json),
        )

        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise

    return {
        "success": True,
        "recipe_id": recipe_id,
        "pattern_id": pattern_id,
        "memory_id": mem_id,
        "error_signature": error_signature,
        "steps_count": len(solution_steps),
        "confidence": 0.5,
        "outcome": outcome,
        "fingerprint_hash": fp.hash,
        "fingerprint_error_type": fp.error_type,
        "fingerprint_ecosystem": fp.ecosystem,
    }


def handle_lore_match_procedure(current_error: str) -> dict[str, Any]:
    """Find the best fix recipe via regex match on darwin_patterns, FTS5 fallback."""
    store = _get_store()

    # 1. Try regex match against darwin_patterns
    rows = store.conn.execute(
        """
        SELECT id, description, rule, frequency, confidence, last_triggered
        FROM darwin_patterns
        WHERE pattern_type = 'error_recipe'
        ORDER BY confidence DESC, frequency DESC
        """
    ).fetchall()

    best_match: dict[str, Any] | None = None
    best_confidence = -1.0

    for row in rows:
        pat_id, description, rule_json, frequency, confidence, last_triggered = row
        # Extract the error signature from description
        sig = description.replace("Fix for: ", "", 1)
        try:
            if re.search(sig, current_error, re.IGNORECASE):
                if confidence > best_confidence:
                    try:
                        solution_steps = json.loads(rule_json)
                    except (json.JSONDecodeError, TypeError):
                        solution_steps = [rule_json]
                    best_match = {
                        "match_method": "regex",
                        "pattern_id": pat_id,
                        "error_signature": sig,
                        "solution_steps": solution_steps,
                        "confidence": confidence,
                        "frequency": frequency,
                        "last_triggered": last_triggered,
                    }
                    best_confidence = confidence
        except re.error:
            # Invalid regex — try literal substring match
            if sig.lower() in current_error.lower():
                if confidence > best_confidence:
                    try:
                        solution_steps = json.loads(rule_json)
                    except (json.JSONDecodeError, TypeError):
                        solution_steps = [rule_json]
                    best_match = {
                        "match_method": "substring",
                        "pattern_id": pat_id,
                        "error_signature": sig,
                        "solution_steps": solution_steps,
                        "confidence": confidence,
                        "frequency": frequency,
                        "last_triggered": last_triggered,
                    }
                    best_confidence = confidence

    if best_match is not None:
        # Update last_triggered and increment frequency
        store.conn.execute(
            "UPDATE darwin_patterns SET last_triggered=?, frequency=frequency+1 WHERE id=?",
            (time.time(), best_match["pattern_id"]),
        )
        store.conn.commit()
        best_match["hint"] = (
            f"Call lore_report_outcome with pattern_id={best_match['pattern_id']} "
            "after applying this fix to close the Darwin feedback loop."
        )
        return {"found": True, **best_match}

    # 2. FTS5 fallback — search memories for error fix content
    fts_results = store.search(
        f"ERROR FIX {current_error[:100]}",
        top_k=3,
        memory_type="experience",
    )
    if fts_results:
        best = fts_results[0]
        meta = best.get("metadata") or {}
        pattern_id_hint = meta.get("pattern_id", "") if isinstance(meta, dict) else ""
        hint = (
            f"Call lore_report_outcome with pattern_id={pattern_id_hint} after applying this fix."
            if pattern_id_hint
            else "Use lore_report_outcome to report outcome once a pattern_id is known."
        )
        return {
            "found": True,
            "match_method": "fts5_fallback",
            "memory_id": best["id"],
            "content": best["content"],
            "confidence": 0.3,
            "message": "No exact regex match — returning closest FTS5 result.",
            "hint": hint,
        }

    return {
        "found": False,
        "message": "No matching procedure found.",
        "current_error": current_error[:200],
    }


def handle_lore_teach(
    convention: str,
    tags: list[str] | None = None,
    source_type: str = "user",
) -> dict[str, Any]:
    """Store a convention/rule/preference as a fact memory."""
    if not convention or not isinstance(convention, str):
        return {"success": False, "error": "convention must be a non-empty string"}
    store = _get_store()

    if source_type not in TRUST_SCORES:
        source_type = "user"
    trust_score = TRUST_SCORES[source_type]

    now = time.time()
    prov_hash = _provenance_hash(convention, now)

    metadata: dict[str, Any] = {
        "source_type": source_type,
        "trust_score": trust_score,
        "provenance_hash": prov_hash,
        "convention": True,
    }
    if tags:
        metadata["tags"] = tags

    mid = store.add(
        content=convention,
        memory_type="fact",
        metadata=metadata,
    )

    return {
        "success": True,
        "memory_id": mid,
        "trust_score": trust_score,
        "provenance_hash": prov_hash,
        "source_type": source_type,
        "tags": tags or [],
    }


def handle_lore_stats() -> dict[str, Any]:
    """Return full statistics about the memory system."""
    store = _get_store()
    identity = _get_identity()

    base_stats = store.stats()

    # Trust level breakdown (exclusive ranges)
    trust_breakdown = {}
    for label, query in [
        ("high", "json_extract(metadata, '$.trust_score') >= 0.9"),
        ("medium", "json_extract(metadata, '$.trust_score') >= 0.6 AND json_extract(metadata, '$.trust_score') < 0.9"),
        ("low", "json_extract(metadata, '$.trust_score') < 0.6 OR json_extract(metadata, '$.trust_score') IS NULL"),
    ]:
        row = store.conn.execute(f"SELECT COUNT(*) FROM memories WHERE {query}").fetchone()
        trust_breakdown[label] = row[0] if row else 0

    # Darwin patterns count
    darwin_count = store.conn.execute(
        "SELECT COUNT(*) FROM darwin_patterns"
    ).fetchone()
    darwin_count = darwin_count[0] if darwin_count else 0

    # Darwin journal count
    journal_count = store.conn.execute(
        "SELECT COUNT(*) FROM darwin_journal"
    ).fetchone()
    journal_count = journal_count[0] if journal_count else 0

    # Identity summary
    id_data = identity.get()
    identity_summary = {
        "configured": identity.exists(),
        "keys": list(id_data.keys()) if id_data else [],
        "token_estimate": identity.token_count(),
    }

    return {
        "total_memories": base_stats["total"],
        "by_type": base_stats["by_type"],
        "by_trust_level": trust_breakdown,
        "darwin_patterns": darwin_count,
        "darwin_journal_entries": journal_count,
        "wal_entries": base_stats["wal_entries"],
        "decay": {
            "avg": base_stats["decay_avg"],
            "min": base_stats["decay_min"],
            "max": base_stats["decay_max"],
        },
        "identity": identity_summary,
    }


def handle_lore_list(
    limit: int = 20,
    offset: int = 0,
    memory_type: str | None = None,
) -> dict[str, Any]:
    """List all memories with pagination and optional type filter."""
    if not isinstance(limit, int) or limit < 1:
        return {"success": False, "error": "limit must be a positive integer"}
    if not isinstance(offset, int) or offset < 0:
        return {"success": False, "error": "offset must be a non-negative integer"}

    store = _get_store()
    rows = store.list_all(memory_type=memory_type, limit=limit, offset=offset)
    total = store.count(memory_type=memory_type)

    items = []
    for mem in rows:
        meta = mem.get("metadata") or {}
        items.append({
            "id": mem["id"],
            "preview": (mem["content"] or "")[:100],
            "memory_type": mem.get("memory_type"),
            "trust_score": meta.get("trust_score", 1.0) if isinstance(meta, dict) else 1.0,
            "created_at": mem.get("created_at"),
        })

    return {
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
        "memory_type": memory_type,
    }


def handle_lore_forget(memory_id: str) -> dict[str, Any]:
    """Soft-delete a memory by setting decay_score to 0.0 (preserves audit trail)."""
    if not memory_id or not isinstance(memory_id, str):
        return {"success": False, "error": "memory_id must be a non-empty string"}

    store = _get_store()
    mem = store.get(memory_id)
    if mem is None:
        return {"success": False, "error": f"Memory not found: {memory_id}"}

    store.wal.record(
        "UPDATE", "memories", record_id=memory_id,
        data={"decay_score": 0.0, "reason": "lore_forget"},
    )
    store.conn.execute(
        "UPDATE memories SET decay_score=0.0 WHERE id=?", (memory_id,)
    )
    store.conn.commit()

    return {
        "success": True,
        "memory_id": memory_id,
        "forgotten_content_preview": (mem["content"] or "")[:100],
        "memory_type": mem.get("memory_type"),
    }


def handle_lore_rate_fix(pattern_id: str, outcome: str) -> dict[str, Any]:
    """
    Bayesian confidence update for a darwin pattern.
    Delegates to darwin.update_confidence for Beta-distribution tracking.
    Also logs to darwin_journal for audit.
    """
    if not pattern_id or not isinstance(pattern_id, str):
        return {"success": False, "error": "pattern_id must be a non-empty string"}
    if outcome not in ("success", "failure"):
        return {"success": False, "error": "outcome must be 'success' or 'failure'"}

    store = _get_store()
    result = update_confidence(store, pattern_id, outcome)
    if not result.get("success"):
        return result

    # Roll up outcome into fingerprint aggregates (Darwin Replay efficacy).
    # Reads the pattern's stored fingerprint_hash from metadata if present.
    pat_row = store.conn.execute(
        "SELECT metadata FROM darwin_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    if pat_row and pat_row[0]:
        try:
            meta = json.loads(pat_row[0])
            fp_hash = meta.get("fingerprint_hash")
            if fp_hash:
                replay_record_outcome(store, fp_hash, outcome)
        except (json.JSONDecodeError, TypeError):
            pass

    # Log to darwin_journal for audit trail
    journal_id = str(uuid.uuid4())
    now = time.time()
    store.conn.execute(
        """
        INSERT INTO darwin_journal (id, query, result_ids, outcome, correction, timestamp, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            journal_id,
            f"rate_fix:{pattern_id}",
            pattern_id,
            outcome,
            None,
            now,
            json.dumps({
                "pattern_id": pattern_id,
                "old_confidence": result["old_confidence"],
                "new_confidence": result["new_confidence"],
            }),
        ),
    )
    store.conn.commit()

    return {
        "success": True,
        "pattern_id": pattern_id,
        "outcome": outcome,
        "old_confidence": result["old_confidence"],
        "new_confidence": result["new_confidence"],
        "frequency": result["frequency"],
    }


def handle_lore_report_outcome(
    pattern_id: str,
    outcome: str,
    context: str | None = None,
) -> dict[str, Any]:
    """
    Close the Darwin feedback loop: log outcome + update Bayesian confidence.
    Called by agents after applying a fix returned by lore_match_procedure.
    """
    if not pattern_id or not isinstance(pattern_id, str):
        return {"success": False, "error": "pattern_id must be a non-empty string"}
    valid_outcomes = ("success", "failure", "partial")
    if outcome not in valid_outcomes:
        return {"success": False, "error": f"outcome must be one of {valid_outcomes}"}

    store = _get_store()

    # Verify pattern exists
    row = store.conn.execute(
        "SELECT id FROM darwin_patterns WHERE id=?", (pattern_id,)
    ).fetchone()
    if row is None:
        return {"success": False, "error": f"Pattern not found: {pattern_id}"}

    journal_id = log_outcome(store, pattern_id, outcome, context)
    confidence_result = update_confidence(store, pattern_id, outcome)

    # Roll up outcome into fingerprint aggregates (Darwin Replay efficacy)
    pat_row = store.conn.execute(
        "SELECT metadata FROM darwin_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    if pat_row and pat_row[0]:
        try:
            meta = json.loads(pat_row[0])
            fp_hash = meta.get("fingerprint_hash")
            if fp_hash:
                replay_record_outcome(store, fp_hash, outcome)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "success": True,
        "pattern_id": pattern_id,
        "outcome": outcome,
        "journal_id": journal_id,
        "old_confidence": confidence_result.get("old_confidence"),
        "new_confidence": confidence_result.get("new_confidence"),
        "alpha": confidence_result.get("alpha"),
        "beta": confidence_result.get("beta"),
        "frequency": confidence_result.get("frequency"),
    }


def handle_lore_evolve(
    min_failures: int = 3,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """
    Run evolve_patterns + consolidate and return the combined report.
    """
    if not isinstance(min_failures, int) or min_failures < 1:
        min_failures = 3
    if not isinstance(max_age_days, int) or max_age_days < 1:
        max_age_days = 30

    store = _get_store()
    evolution = evolve_patterns(store, min_failures=min_failures)
    consolidation = consolidate(store, max_age_days=max_age_days)

    return {
        "evolution": evolution,
        "consolidation": consolidation,
        "summary": {
            "patterns_demoted": len(evolution["demoted"]),
            "patterns_promoted": len(evolution["promoted"]),
            "errors_needing_recipe": len(evolution["needs_recipe"]),
            "memories_decayed": consolidation["decayed"],
            "memories_deduped": consolidation["deduped"],
            "patterns_deprecated": consolidation["deprecated"],
        },
    }


# ── Prefetcher tool handler ───────────────────────────────────────────────────

def handle_lore_darwin_classify(
    error_text: str,
    top_k: int = 3,
) -> dict[str, Any]:
    """
    Darwin Replay: given an error, return the normalized fingerprint +
    ranked fix-recipes with measured success rates.
    """
    if not error_text or not isinstance(error_text, str):
        return {"success": False, "error": "error_text must be a non-empty string"}
    if not isinstance(top_k, int) or top_k < 1:
        return {"success": False, "error": "top_k must be a positive integer"}
    store = _get_store()
    return darwin_classify(store, error_text, top_k=top_k)


def handle_lore_darwin_stats() -> dict[str, Any]:
    """Corpus-wide Darwin fingerprint stats (the dashboard of the moat)."""
    store = _get_store()
    return darwin_stats(store)


def handle_lore_darwin_export(min_total_seen: int = 1) -> dict[str, Any]:
    """
    Export the fingerprint corpus in a sanitized, shareable form.
    Safe to publish: already-redacted fingerprints + aggregates only.
    """
    if not isinstance(min_total_seen, int) or min_total_seen < 1:
        min_total_seen = 1
    store = _get_store()
    corpus = export_sanitized(store, min_total_seen=min_total_seen)
    return {
        "count": len(corpus),
        "min_total_seen": min_total_seen,
        "fingerprints": corpus,
    }


def handle_lore_briefing(
    entity: str | None = None,
    tool_used: str | None = None,
) -> dict[str, Any]:
    """Generate a session-start briefing with predicted context."""
    store = _get_store()
    return generate_briefing(store, entity=entity, tool_used=tool_used)


# ── Cognition tool wrapper ────────────────────────────────────────────────────

def _handle_lore_knowledge(query: str, top_k: int = 5) -> dict[str, Any]:
    """Thin wrapper around cognition.handle_lore_knowledge using the module store."""
    from ..cognition import query_knowledge
    store = _get_store()
    results = query_knowledge(store, query, top_k=top_k)
    return {
        "query": query,
        "results": results,
        "count": len(results),
    }


# ── Tool registry ─────────────────────────────────────────────────────────────

_HANDLERS = {
    "lore_remember": handle_lore_remember,
    "lore_recall": handle_lore_recall,
    "lore_fix": handle_lore_fix,
    "lore_match_procedure": handle_lore_match_procedure,
    "lore_teach": handle_lore_teach,
    "lore_stats": handle_lore_stats,
    "lore_list": handle_lore_list,
    "lore_forget": handle_lore_forget,
    "lore_rate_fix": handle_lore_rate_fix,
    "lore_report_outcome": handle_lore_report_outcome,
    "lore_evolve": handle_lore_evolve,
    "lore_knowledge": _handle_lore_knowledge,
    "lore_briefing": handle_lore_briefing,
    "lore_darwin_classify": handle_lore_darwin_classify,
    "lore_darwin_stats": handle_lore_darwin_stats,
    "lore_darwin_export": handle_lore_darwin_export,
}

TOOLS: dict[str, dict] = {
    name: {**TOOL_SCHEMAS[name], "handler": _HANDLERS[name]}
    for name in TOOL_SCHEMAS
}


# ── JSON-RPC request handler ──────────────────────────────────────────────────

def handle_request(request: dict) -> dict | None:
    method = request.get("method", "")
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_version = params.get("protocolVersion", SUPPORTED_PROTOCOL_VERSIONS[-1])
        negotiated = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "lore-memory", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        tool_list = [
            {
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            }
            for name, spec in TOOLS.items()
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tool_list},
        }

    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}

        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

        # Coerce types from schema
        schema_props = TOOLS[tool_name]["inputSchema"].get("properties", {})
        for key, value in list(tool_args.items()):
            prop = schema_props.get(key, {})
            declared = prop.get("type")
            if declared == "integer" and not isinstance(value, int):
                try:
                    tool_args[key] = int(value)
                except (TypeError, ValueError):
                    pass
            elif declared == "number" and not isinstance(value, (int, float)):
                try:
                    tool_args[key] = float(value)
                except (TypeError, ValueError):
                    pass

        try:
            result = TOOLS[tool_name]["handler"](**tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                },
            }
        except TypeError as exc:
            logger.exception("Tool argument error in %s: %s", tool_name, exc)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"Invalid arguments: {exc}"},
            }
        except Exception:
            logger.exception("Tool error in %s", tool_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "Internal tool error"},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("lore-memory MCP server starting...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except json.JSONDecodeError as exc:
            logger.error("JSON decode error: %s", exc)
            sys.stdout.write(
                json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }) + "\n"
            )
            sys.stdout.flush()
        except Exception as exc:
            logger.error("Server error: %s", exc)


if __name__ == "__main__":
    main()
