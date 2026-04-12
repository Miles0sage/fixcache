"""
darwin_replay.py — Darwin Replay + Fingerprints (the moat).

Builds on lore_memory.fingerprint to turn raw error recipes into a
measurable, cross-repo, shareable failure corpus. Every error gets
a canonical fingerprint hash; every fix outcome updates the
fingerprint's aggregated efficacy counts.

Public surface:
  upsert_fingerprint(store, error_text) -> dict
  record_outcome(store, fp_hash, outcome) -> dict
  classify(store, error_text, top_k=3) -> dict
      # returns fingerprint + ranked patterns with success rates
  darwin_stats(store) -> dict
      # corpus-wide stats: total fingerprints, top ecosystems, efficacy bands
  export_sanitized(store) -> list[dict]
      # privacy-preserving corpus suitable for sharing/packs

Why this is the moat:
  - Anyone can store errors. Nobody else normalizes them into a
    shareable fingerprint linked to measured fix efficacy.
  - The hash is stable, the aggregates compound with every outcome,
    and the export is safe to publish because fingerprints are
    already redacted by construction.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .core.store import MemoryStore
from .fingerprint import Fingerprint, compute_fingerprint


# ── Upsert ────────────────────────────────────────────────────────────────────

def upsert_fingerprint(store: MemoryStore, error_text: str) -> dict[str, Any]:
    """
    Compute a fingerprint for the given error text and upsert it into
    the `fingerprints` table. Increments total_seen and last_seen on
    every call so the table is an authoritative "how often does this
    class of failure happen" counter.

    Returns the stored fingerprint row as a dict.
    """
    fp = compute_fingerprint(error_text)
    now = time.time()

    row = store.conn.execute(
        "SELECT hash FROM fingerprints WHERE hash = ?", (fp.hash,)
    ).fetchone()

    if row is None:
        store.conn.execute(
            """
            INSERT INTO fingerprints
                (hash, error_type, ecosystem, tool, essence, top_frame,
                 total_seen, total_success, total_failure,
                 first_seen, last_seen, best_pattern_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, ?, ?, NULL, NULL)
            """,
            (
                fp.hash,
                fp.error_type,
                fp.ecosystem,
                fp.tool,
                fp.essence,
                fp.top_frame,
                now,
                now,
            ),
        )
    else:
        store.conn.execute(
            """
            UPDATE fingerprints
               SET total_seen = total_seen + 1,
                   last_seen = ?
             WHERE hash = ?
            """,
            (now, fp.hash),
        )

    store.conn.commit()
    return {
        **fp.as_dict(),
        "total_seen": _get_counter(store, fp.hash, "total_seen"),
    }


def _get_counter(store: MemoryStore, fp_hash: str, column: str) -> int:
    row = store.conn.execute(
        f"SELECT {column} FROM fingerprints WHERE hash = ?", (fp_hash,)
    ).fetchone()
    return row[0] if row else 0


# ── Record outcome ────────────────────────────────────────────────────────────

def record_outcome(
    store: MemoryStore,
    fp_hash: str,
    outcome: str,
) -> dict[str, Any]:
    """
    Update aggregated success/failure counts on a fingerprint.

    Args:
        fp_hash: 16-char hash from compute_fingerprint().
        outcome: 'success' or 'failure' (other values are treated as neutral).
    """
    row = store.conn.execute(
        "SELECT hash, total_success, total_failure FROM fingerprints WHERE hash = ?",
        (fp_hash,),
    ).fetchone()
    if row is None:
        return {"success": False, "error": f"Unknown fingerprint: {fp_hash}"}

    _, success_before, failure_before = row

    if outcome == "success":
        store.conn.execute(
            "UPDATE fingerprints SET total_success = total_success + 1 WHERE hash = ?",
            (fp_hash,),
        )
        success_after = success_before + 1
        failure_after = failure_before
    elif outcome == "failure":
        store.conn.execute(
            "UPDATE fingerprints SET total_failure = total_failure + 1 WHERE hash = ?",
            (fp_hash,),
        )
        success_after = success_before
        failure_after = failure_before + 1
    else:
        success_after = success_before
        failure_after = failure_before

    store.conn.commit()
    total = success_after + failure_after
    efficacy = (success_after / total) if total > 0 else None
    return {
        "success": True,
        "hash": fp_hash,
        "total_success": success_after,
        "total_failure": failure_after,
        "efficacy": efficacy,
    }


# ── Classify ─────────────────────────────────────────────────────────────────

def classify(
    store: MemoryStore,
    error_text: str,
    top_k: int = 3,
) -> dict[str, Any]:
    """
    Given raw error text, return the fingerprint + the top ranked
    fix-recipes for this failure class with their success rates.

    This is the public "Darwin Replay" query: "I'm seeing this error
    again — what have I learned works for it?"
    """
    fp = compute_fingerprint(error_text)

    fp_row = store.conn.execute(
        """
        SELECT hash, error_type, ecosystem, tool, essence, top_frame,
               total_seen, total_success, total_failure, first_seen, last_seen
          FROM fingerprints
         WHERE hash = ?
        """,
        (fp.hash,),
    ).fetchone()

    fingerprint_stats: dict[str, Any] | None = None
    if fp_row is not None:
        total_s = fp_row[7]
        total_f = fp_row[8]
        total = total_s + total_f
        efficacy = (total_s / total) if total > 0 else None
        fingerprint_stats = {
            "hash": fp_row[0],
            "error_type": fp_row[1],
            "ecosystem": fp_row[2],
            "tool": fp_row[3],
            "essence": fp_row[4],
            "top_frame": fp_row[5],
            "total_seen": fp_row[6],
            "total_success": total_s,
            "total_failure": total_f,
            "efficacy": efficacy,
            "first_seen": fp_row[9],
            "last_seen": fp_row[10],
        }

    # Pull candidate patterns scoped to this fingerprint first; fall back
    # to whole-corpus regex matching if the fingerprint is new.
    pattern_rows = store.conn.execute(
        """
        SELECT id, description, rule, frequency, confidence, last_triggered, metadata
          FROM darwin_patterns
         WHERE pattern_type = 'error_recipe'
           AND (metadata IS NOT NULL AND instr(metadata, ?) > 0)
         ORDER BY confidence DESC, frequency DESC
         LIMIT ?
        """,
        (fp.hash, top_k),
    ).fetchall()

    if not pattern_rows:
        # Fall back to any error_recipe patterns with the same error_type
        pattern_rows = store.conn.execute(
            """
            SELECT id, description, rule, frequency, confidence, last_triggered, metadata
              FROM darwin_patterns
             WHERE pattern_type = 'error_recipe'
               AND description LIKE ?
             ORDER BY confidence DESC, frequency DESC
             LIMIT ?
            """,
            (f"%{fp.error_type}%", top_k),
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in pattern_rows:
        pat_id, description, rule_json, frequency, confidence, last_triggered, meta_json = row
        try:
            steps = json.loads(rule_json)
            if not isinstance(steps, list):
                steps = [str(steps)]
        except (json.JSONDecodeError, TypeError):
            steps = [rule_json]
        candidates.append(
            {
                "pattern_id": pat_id,
                "description": description,
                "solution_steps": steps,
                "frequency": frequency,
                "confidence": round(confidence, 4),
                "last_triggered": last_triggered,
            }
        )

    return {
        "fingerprint": fp.as_dict(),
        "fingerprint_stats": fingerprint_stats,
        "candidates": candidates,
        "match_count": len(candidates),
    }


# ── Stats ────────────────────────────────────────────────────────────────────

def darwin_stats(store: MemoryStore) -> dict[str, Any]:
    """
    Corpus-wide stats: total fingerprints, top ecosystems, efficacy bands.
    This is the dashboard number for the moat.
    """
    total_row = store.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_seen), 0), "
        "COALESCE(SUM(total_success), 0), COALESCE(SUM(total_failure), 0) "
        "FROM fingerprints"
    ).fetchone()
    total_fps, total_seen, total_s, total_f = total_row or (0, 0, 0, 0)

    # Top ecosystems
    eco_rows = store.conn.execute(
        "SELECT ecosystem, COUNT(*) FROM fingerprints GROUP BY ecosystem "
        "ORDER BY COUNT(*) DESC LIMIT 10"
    ).fetchall()
    ecosystems = {row[0]: row[1] for row in eco_rows}

    # Top error_types
    type_rows = store.conn.execute(
        "SELECT error_type, COUNT(*) FROM fingerprints GROUP BY error_type "
        "ORDER BY COUNT(*) DESC LIMIT 10"
    ).fetchall()
    error_types = {row[0]: row[1] for row in type_rows}

    # Efficacy bands
    bands = {"unrated": 0, "low": 0, "medium": 0, "high": 0}
    band_rows = store.conn.execute(
        "SELECT total_success, total_failure FROM fingerprints"
    ).fetchall()
    for s, f in band_rows:
        total = s + f
        if total == 0:
            bands["unrated"] += 1
            continue
        rate = s / total
        if rate >= 0.75:
            bands["high"] += 1
        elif rate >= 0.4:
            bands["medium"] += 1
        else:
            bands["low"] += 1

    overall_efficacy: float | None = None
    total_outcomes = total_s + total_f
    if total_outcomes > 0:
        overall_efficacy = total_s / total_outcomes

    return {
        "total_fingerprints": total_fps,
        "total_seen_events": total_seen,
        "total_success": total_s,
        "total_failure": total_f,
        "overall_efficacy": overall_efficacy,
        "top_ecosystems": ecosystems,
        "top_error_types": error_types,
        "efficacy_bands": bands,
    }


# ── Sanitized export ─────────────────────────────────────────────────────────

def export_sanitized(store: MemoryStore, min_total_seen: int = 1) -> list[dict[str, Any]]:
    """
    Return the fingerprints table in a form safe to share/publish.

    Fingerprints are already redacted by construction — this function
    additionally:
      - Drops rows with total_seen < min_total_seen (noise floor)
      - Strips internal DB IDs / best_pattern_id
      - Keeps aggregate counts only
    """
    rows = store.conn.execute(
        """
        SELECT hash, error_type, ecosystem, tool, essence, top_frame,
               total_seen, total_success, total_failure
          FROM fingerprints
         WHERE total_seen >= ?
         ORDER BY total_seen DESC, last_seen DESC
        """,
        (min_total_seen,),
    ).fetchall()

    corpus: list[dict[str, Any]] = []
    for row in rows:
        (
            h,
            error_type,
            ecosystem,
            tool,
            essence,
            top_frame,
            total_seen,
            total_s,
            total_f,
        ) = row
        total = total_s + total_f
        corpus.append(
            {
                "hash": h,
                "error_type": error_type,
                "ecosystem": ecosystem,
                "tool": tool,
                "essence": essence,
                "top_frame": top_frame,
                "total_seen": total_seen,
                "total_success": total_s,
                "total_failure": total_f,
                "efficacy": (total_s / total) if total > 0 else None,
            }
        )
    return corpus
