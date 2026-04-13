"""
darwin.py — Darwin Evolution Engine for lore-memory.

Implements the feedback loop that makes error recipes learn and improve over time.
Four core functions:
  log_outcome        — record what happened when a pattern was applied
  update_confidence  — Bayesian Beta-distribution confidence update
  evolve_patterns    — scan journal for recurring failures; promote/demote/flag
  consolidate        — strategic forgetting: decay, dedup, deprecate
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .core.store import MemoryStore
from .core.wal import WAL
from .util import safe_regex_search


# ── Confidence thresholds ──────────────────────────────────────────────────────

CONFIDENCE_DEMOTION_THRESHOLD = 0.2   # below this → demoted
CONFIDENCE_DEPRECATED_THRESHOLD = 0.1  # below this + no accesses → deprecated
MIN_FAILURES_FOR_DEMOTION = 3


# ── log_outcome ───────────────────────────────────────────────────────────────

def log_outcome(
    store: MemoryStore,
    pattern_id: str,
    outcome: str,
    context: str | None = None,
) -> str:
    """
    Record the outcome of applying a fix pattern to darwin_journal.

    Args:
        store:      MemoryStore instance.
        pattern_id: ID of the darwin_pattern that was applied.
        outcome:    One of 'success', 'failure', 'partial'.
        context:    Optional free-text context (error text, env info, etc.).

    Returns:
        The generated journal entry ID.
    """
    valid_outcomes = ("success", "failure", "partial", "corrected")
    if outcome not in valid_outcomes:
        outcome = "partial"

    journal_id = str(uuid.uuid4())
    now = time.time()
    meta: dict[str, Any] = {"pattern_id": pattern_id, "source": "log_outcome"}
    if context:
        meta["context"] = context[:2000]  # cap context length

    store.conn.execute(
        """
        INSERT INTO darwin_journal (id, query, result_ids, outcome, correction, timestamp, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            journal_id,
            f"outcome:{pattern_id}",
            pattern_id,
            outcome,
            context[:2000] if context else None,
            now,
            json.dumps(meta),
        ),
    )
    store.conn.commit()
    return journal_id


# ── update_confidence ─────────────────────────────────────────────────────────

def update_confidence(
    store: MemoryStore,
    pattern_id: str,
    outcome: str,
) -> dict[str, Any]:
    """
    Bayesian Beta-distribution confidence update for a darwin pattern.

    Tracks alpha (successes) and beta (failures) in pattern metadata.
    confidence = alpha / (alpha + beta)

    Args:
        store:      MemoryStore instance.
        pattern_id: ID of the darwin_pattern to update.
        outcome:    'success' increases alpha; 'failure' increases beta;
                    'partial' increases both by 0.5 (neutral).

    Returns:
        Dict with old_confidence, new_confidence, alpha, beta, frequency.
    """
    row = store.conn.execute(
        "SELECT confidence, frequency, metadata FROM darwin_patterns WHERE id=?",
        (pattern_id,),
    ).fetchone()
    if row is None:
        return {"success": False, "error": f"Pattern not found: {pattern_id}"}

    old_confidence = row[0]
    frequency = row[1]
    raw_meta = row[2]

    meta: dict[str, Any] = {}
    if raw_meta:
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}

    # Initialise Beta parameters from metadata.
    # If not yet stored, use an uninformative prior (1, 1) — the flat Beta.
    # Do NOT derive from frequency: frequency counts total applications, not
    # outcomes, so using it as a pseudo-count invents phantom training data and
    # makes the learning rate collapse on heavily-seen but un-rated patterns.
    alpha = float(meta.get("beta_alpha", 1.0))
    beta_val = float(meta.get("beta_beta", 1.0))

    # Ensure minimum non-zero values
    alpha = max(alpha, 0.5)
    beta_val = max(beta_val, 0.5)

    if outcome == "success":
        alpha += 1.0
    elif outcome == "failure":
        beta_val += 1.0
    elif outcome in ("partial", "corrected"):
        alpha += 0.5
        beta_val += 0.5

    new_confidence = alpha / (alpha + beta_val)
    new_frequency = frequency + 1
    now = time.time()

    meta["beta_alpha"] = alpha
    meta["beta_beta"] = beta_val

    store.conn.execute(
        """
        UPDATE darwin_patterns
        SET confidence=?, frequency=?, last_triggered=?, metadata=?
        WHERE id=?
        """,
        (new_confidence, new_frequency, now, json.dumps(meta), pattern_id),
    )
    store.conn.commit()

    return {
        "success": True,
        "pattern_id": pattern_id,
        "outcome": outcome,
        "old_confidence": round(old_confidence, 4),
        "new_confidence": round(new_confidence, 4),
        "alpha": round(alpha, 2),
        "beta": round(beta_val, 2),
        "frequency": new_frequency,
    }


# ── evolve_patterns ───────────────────────────────────────────────────────────

def evolve_patterns(
    store: MemoryStore,
    min_failures: int = 3,
) -> dict[str, Any]:
    """
    Scan darwin_journal for recurring failures and evolve patterns accordingly.

    Logic:
    - If a pattern_id appears in min_failures+ failure entries: demote its
      confidence below CONFIDENCE_DEMOTION_THRESHOLD (0.2).
    - If multiple patterns exist for the same error signature: promote the one
      with the highest success rate, demote the rest.
    - If an error signature has NO matching pattern but appears min_failures+
      times in the journal: flag as 'needs_recipe'.

    Returns:
        {"demoted": [...], "promoted": [...], "needs_recipe": [...]}
    """
    now = time.time()
    demoted: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []
    needs_recipe: list[str] = []

    # ── 1. Find patterns with min_failures+ failure journal entries ────────────
    failure_rows = store.conn.execute(
        """
        SELECT result_ids AS pattern_id, COUNT(*) AS fail_count
        FROM darwin_journal
        WHERE outcome = 'failure'
          AND result_ids IS NOT NULL
        GROUP BY result_ids
        HAVING COUNT(*) >= ?
        """,
        (min_failures,),
    ).fetchall()

    for row in failure_rows:
        pid, fail_count = row[0], row[1]
        pat = store.conn.execute(
            "SELECT id, confidence, description FROM darwin_patterns WHERE id=?",
            (pid,),
        ).fetchone()
        if pat is None:
            continue

        pat_id, current_conf, description = pat
        if current_conf > CONFIDENCE_DEMOTION_THRESHOLD:
            new_conf = CONFIDENCE_DEMOTION_THRESHOLD * 0.5  # push well below threshold
            store.conn.execute(
                "UPDATE darwin_patterns SET confidence=?, last_triggered=? WHERE id=?",
                (new_conf, now, pat_id),
            )
            demoted.append({
                "pattern_id": pat_id,
                "description": description,
                "old_confidence": round(current_conf, 4),
                "new_confidence": round(new_conf, 4),
                "failure_count": fail_count,
            })

    # ── 2. For each error signature with multiple patterns, promote the best ───
    # Group patterns by their error signature (description = "Fix for: <sig>")
    all_patterns = store.conn.execute(
        """
        SELECT id, description, confidence, frequency
        FROM darwin_patterns
        WHERE pattern_type = 'error_recipe'
        ORDER BY description, confidence DESC
        """
    ).fetchall()

    # Group by signature
    sig_groups: dict[str, list[tuple]] = {}
    for row in all_patterns:
        pid, desc, conf, freq = row
        sig = desc.replace("Fix for: ", "", 1)
        sig_groups.setdefault(sig, []).append((pid, conf, freq))

    for sig, patterns in sig_groups.items():
        if len(patterns) < 2:
            continue
        # Best is first (sorted by confidence DESC)
        best_pid, best_conf, best_freq = patterns[0]

        # Count successes for this pattern
        success_count = store.conn.execute(
            "SELECT COUNT(*) FROM darwin_journal WHERE result_ids=? AND outcome='success'",
            (best_pid,),
        ).fetchone()[0]

        if success_count > 0 or best_conf >= 0.6:
            # Promote the best
            promoted_conf = min(best_conf * 1.1, 0.99)
            store.conn.execute(
                "UPDATE darwin_patterns SET confidence=?, last_triggered=? WHERE id=?",
                (promoted_conf, now, best_pid),
            )
            promoted.append({
                "pattern_id": best_pid,
                "error_signature": sig,
                "old_confidence": round(best_conf, 4),
                "new_confidence": round(promoted_conf, 4),
                "competitors_demoted": len(patterns) - 1,
            })

            # Demote the rest (if they haven't already been demoted above)
            for pid, conf, _freq in patterns[1:]:
                if conf > CONFIDENCE_DEMOTION_THRESHOLD:
                    store.conn.execute(
                        "UPDATE darwin_patterns SET confidence=?, last_triggered=? WHERE id=?",
                        (CONFIDENCE_DEMOTION_THRESHOLD * 0.5, now, pid),
                    )
                    # Only add to demoted if not already there
                    if not any(d["pattern_id"] == pid for d in demoted):
                        demoted.append({
                            "pattern_id": pid,
                            "description": f"Fix for: {sig}",
                            "old_confidence": round(conf, 4),
                            "new_confidence": round(CONFIDENCE_DEMOTION_THRESHOLD * 0.5, 4),
                            "failure_count": 0,
                            "reason": "outcompeted",
                        })

    # ── 3. Flag error signatures with no matching pattern but 3+ appearances ───
    # Scan journal for queries that look like error text (not rate_fix or outcome:)
    unmatched_rows = store.conn.execute(
        """
        SELECT query, COUNT(*) AS appearances
        FROM darwin_journal
        WHERE query NOT LIKE 'rate_fix:%'
          AND query NOT LIKE 'outcome:%'
          AND outcome = 'failure'
        GROUP BY query
        HAVING COUNT(*) >= ?
        """,
        (min_failures,),
    ).fetchall()

    for row in unmatched_rows:
        query_text, appearances = row[0], row[1]
        # Check if any pattern covers this query
        has_pattern = False
        pattern_rows = store.conn.execute(
            "SELECT description FROM darwin_patterns WHERE pattern_type='error_recipe'"
        ).fetchall()
        for prow in pattern_rows:
            sig = prow[0].replace("Fix for: ", "", 1)
            try:
                if safe_regex_search(sig, query_text):
                    has_pattern = True
                    break
            except re.error:
                if sig.lower() in query_text.lower():
                    has_pattern = True
                    break

        if not has_pattern and query_text not in needs_recipe:
            needs_recipe.append(query_text[:200])

    store.conn.commit()

    return {
        "demoted": demoted,
        "promoted": promoted,
        "needs_recipe": needs_recipe,
    }


# ── consolidate ───────────────────────────────────────────────────────────────

def consolidate(
    store: MemoryStore,
    max_age_days: int = 30,
) -> dict[str, Any]:
    """
    Strategic forgetting — prune low-value memories to keep the store sharp.

    Three passes:
    1. Decay: memories with access_count=0 and age > max_age_days get
       decay_score halved (Lottery Ticket principle — keep what gets used).
    2. Dedup: memories with duplicate content hash — keep highest trust,
       tombstone (decay_score=0) the others.
    3. Deprecate: darwin_patterns with confidence < 0.1 and no accesses
       in max_age_days days — mark as deprecated in metadata.

    Returns:
        {"decayed": N, "deduped": N, "deprecated": N}
    """
    now = time.time()
    cutoff = now - (max_age_days * 86400)
    decayed = 0
    deduped = 0
    deprecated = 0

    # ── Pass 1: Decay unused old memories ─────────────────────────────────────
    old_unused = store.conn.execute(
        """
        SELECT id, decay_score
        FROM memories
        WHERE access_count = 0
          AND created_at < ?
          AND decay_score > 0.01
        """,
        (cutoff,),
    ).fetchall()

    for row in old_unused:
        mem_id, current_decay = row[0], row[1]
        new_decay = current_decay * 0.5
        store.conn.execute(
            "UPDATE memories SET decay_score=? WHERE id=?",
            (new_decay, mem_id),
        )
        decayed += 1

    # ── Pass 2: Dedup by content hash ─────────────────────────────────────────
    # Compute SHA-256 of content for each memory, group duplicates
    all_mems = store.conn.execute(
        "SELECT id, content, metadata, decay_score FROM memories WHERE decay_score > 0"
    ).fetchall()

    content_groups: dict[str, list[tuple]] = {}
    for row in all_mems:
        mem_id, content, raw_meta, decay = row
        if not content:
            continue
        content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        content_groups.setdefault(content_hash, []).append((mem_id, raw_meta, decay))

    for content_hash, group in content_groups.items():
        if len(group) < 2:
            continue

        # Parse trust scores and find the best
        scored: list[tuple[str, float]] = []
        for mem_id, raw_meta, decay in group:
            trust = 0.5
            if raw_meta:
                try:
                    meta = json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
                    trust = float(meta.get("trust_score", 0.5))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            scored.append((mem_id, trust))

        # Keep the highest-trust memory; tombstone the rest
        scored.sort(key=lambda x: x[1], reverse=True)
        keeper_id = scored[0][0]
        for mem_id, _trust in scored[1:]:
            store.conn.execute(
                "UPDATE memories SET decay_score=0.0 WHERE id=?",
                (mem_id,),
            )
            deduped += 1

    # ── Pass 3: Deprecate low-confidence patterns that are never accessed ──────
    stale_patterns = store.conn.execute(
        """
        SELECT id, metadata
        FROM darwin_patterns
        WHERE confidence < ?
          AND (last_triggered IS NULL OR last_triggered < ?)
        """,
        (CONFIDENCE_DEPRECATED_THRESHOLD, cutoff),
    ).fetchall()

    for row in stale_patterns:
        pat_id, raw_meta = row[0], row[1]
        meta: dict[str, Any] = {}
        if raw_meta:
            try:
                meta = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}

        if meta.get("deprecated"):
            continue  # already marked

        meta["deprecated"] = True
        meta["deprecated_at"] = now
        store.conn.execute(
            "UPDATE darwin_patterns SET metadata=? WHERE id=?",
            (json.dumps(meta), pat_id),
        )
        deprecated += 1

    # Prune WAL entries older than 7 days to prevent unbounded growth
    WAL(store.conn).prune(time.time() - 7 * 86400)

    store.conn.commit()

    return {
        "decayed": decayed,
        "deduped": deduped,
        "deprecated": deprecated,
    }


# ── auto_report_outcome ───────────────────────────────────────────────────────

def auto_report_outcome(
    pattern_id: str,
    store: MemoryStore,
    apply_window: int = 60,
    success_window: int = 30,
    repeat_window: int = 60,
) -> dict[str, Any]:
    """
    Watch behavior, not ask for feedback.

    Infers whether a pattern application succeeded or failed by observing:
    - Was the pattern triggered recently (within apply_window seconds)?
    - Did any error entries appear in darwin_journal in the next success_window seconds?
    - Did the same fingerprint (result_ids) appear again within repeat_window seconds?

    Calls update_confidence() based on the inferred outcome.

    Args:
        pattern_id:     ID of the darwin_pattern to check.
        store:          MemoryStore instance.
        apply_window:   Seconds to look back for a recent trigger. Default 60.
        success_window: Seconds after trigger with no errors → infer SUCCESS. Default 30.
        repeat_window:  Seconds after trigger; repeat same fingerprint → infer FAILURE. Default 60.

    Returns:
        Dict with inferred_outcome, confidence_update result, or reason if no action taken.
    """
    now = time.time()

    # ── 1. Check if pattern was triggered recently ────────────────────────────
    row = store.conn.execute(
        "SELECT last_triggered, confidence FROM darwin_patterns WHERE id=?",
        (pattern_id,),
    ).fetchone()

    if row is None:
        return {"success": False, "error": f"Pattern not found: {pattern_id}"}

    last_triggered, confidence = row[0], row[1]

    if last_triggered is None or (now - last_triggered) > apply_window:
        return {
            "success": False,
            "reason": "pattern_not_recently_triggered",
            "last_triggered": last_triggered,
            "apply_window_s": apply_window,
        }

    trigger_time = last_triggered

    # ── 2. Check for repeat fingerprint within repeat_window → FAILURE ────────
    repeat_row = store.conn.execute(
        """
        SELECT COUNT(*) FROM darwin_journal
        WHERE result_ids = ?
          AND timestamp > ?
          AND timestamp <= ?
          AND outcome = 'failure'
        """,
        (pattern_id, trigger_time, trigger_time + repeat_window),
    ).fetchone()

    if repeat_row and repeat_row[0] > 0:
        inferred = "failure"
        result = update_confidence(store, pattern_id, inferred)
        log_outcome(store, pattern_id, inferred, context="auto_report: repeat fingerprint detected")
        return {
            "success": True,
            "inferred_outcome": inferred,
            "reason": "repeat_fingerprint_in_window",
            "confidence_update": result,
        }

    # ── 3. Check for error entries in journal after trigger → FAILURE ─────────
    error_row = store.conn.execute(
        """
        SELECT COUNT(*) FROM darwin_journal
        WHERE outcome = 'failure'
          AND timestamp > ?
          AND timestamp <= ?
        """,
        (trigger_time, trigger_time + success_window),
    ).fetchone()

    if error_row and error_row[0] > 0:
        inferred = "failure"
        result = update_confidence(store, pattern_id, inferred)
        log_outcome(store, pattern_id, inferred, context="auto_report: error entries after apply")
        return {
            "success": True,
            "inferred_outcome": inferred,
            "reason": "errors_after_apply",
            "confidence_update": result,
        }

    # ── 4. No errors in success_window → infer SUCCESS ───────────────────────
    elapsed = now - trigger_time
    if elapsed >= success_window:
        inferred = "success"
        result = update_confidence(store, pattern_id, inferred)
        log_outcome(store, pattern_id, inferred, context="auto_report: no errors in success window")
        return {
            "success": True,
            "inferred_outcome": inferred,
            "reason": "no_errors_in_success_window",
            "elapsed_s": round(elapsed, 1),
            "confidence_update": result,
        }

    # ── 5. Still within success window — too early to decide ─────────────────
    return {
        "success": False,
        "reason": "still_within_success_window",
        "elapsed_s": round(elapsed, 1),
        "success_window_s": success_window,
    }
