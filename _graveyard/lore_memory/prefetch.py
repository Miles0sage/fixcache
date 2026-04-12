"""
prefetch.py — Prefetcher: predict what context the agent needs before it asks.

The "Copilot moment" for memory — records access patterns silently and uses
frequency counting to pre-load the memories most likely needed for the
current context (hour of day + entity + tool).

Public API:
    record_access(store, memory_ids, entity=None, tool_used=None)
    predict_context(store, entity=None, tool_used=None, top_k=5) -> list[dict]
    generate_briefing(store, entity=None, tool_used=None) -> dict
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from .core.store import MemoryStore
from .layers.identity import IdentityLayer


# ── Access recording ──────────────────────────────────────────────────────────


def record_access(
    store: MemoryStore,
    memory_ids: list[str],
    entity: str | None = None,
    tool_used: str | None = None,
) -> None:
    """
    Record what memories were accessed, with context.

    Silently stores into access_patterns. Called after every successful recall
    to build the prediction model in the background.

    Args:
        store: Open MemoryStore.
        memory_ids: List of memory IDs that were recalled.
        entity: Optional entity/project context (e.g. "lore-memory", "phalanx").
        tool_used: Optional tool name that triggered the recall (e.g. "lore_recall").
    """
    if not memory_ids:
        return

    now = time.time()
    hour = int(time.gmtime(now).tm_hour)
    ids_json = json.dumps(memory_ids)

    store.conn.execute(
        """
        INSERT INTO access_patterns (hour_of_day, entity, tool_used, memory_ids, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (hour, entity, tool_used, ids_json, now),
    )
    store.conn.commit()


# ── Context prediction ────────────────────────────────────────────────────────


def predict_context(
    store: MemoryStore,
    entity: str | None = None,
    tool_used: str | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Predict which memories will be needed based on past access patterns.

    Looks for access_patterns matching:
      - Same hour bucket ±2 (catches "morning sessions", etc.)
      - Same entity (if provided)
      - Same tool_used (if provided)

    Counts frequency of each memory_id across matching patterns, returns
    the top_k most frequently accessed memories pre-loaded from the store.

    Args:
        store: Open MemoryStore.
        entity: Optional entity context to filter patterns.
        tool_used: Optional tool name to filter patterns.
        top_k: Number of memories to return.

    Returns:
        List of memory dicts ordered by predicted relevance (frequency desc).
    """
    now = time.time()
    hour = int(time.gmtime(now).tm_hour)

    # Build hour bucket: current hour ±2, wrapping around 0/23
    hour_bucket = [(hour + offset) % 24 for offset in range(-2, 3)]
    placeholders = ",".join("?" * len(hour_bucket))

    sql = f"SELECT memory_ids FROM access_patterns WHERE hour_of_day IN ({placeholders})"
    params: list[Any] = list(hour_bucket)

    if entity is not None:
        sql += " AND entity = ?"
        params.append(entity)

    if tool_used is not None:
        sql += " AND tool_used = ?"
        params.append(tool_used)

    rows = store.conn.execute(sql, params).fetchall()

    # Count frequency of each memory_id across matching patterns
    freq: Counter[str] = Counter()
    for row in rows:
        try:
            ids = json.loads(row[0])
            if isinstance(ids, list):
                for mid in ids:
                    freq[mid] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    if not freq:
        return []

    # Fetch the top_k most frequent memory IDs from the store
    top_ids = [mid for mid, _ in freq.most_common(top_k)]
    memories = []
    for mid in top_ids:
        mem = store.get(mid)
        if mem is not None and mem.get("decay_score", 1.0) > 0.0:
            mem["_prefetch_frequency"] = freq[mid]
            memories.append(mem)

    return memories


# ── Briefing generation ───────────────────────────────────────────────────────

_BRIEFING_TEMPLATE = """\
## SESSION BRIEFING
Generated at session start. Predicted context based on {pattern_count} past access patterns.

### Identity
{identity_section}

### Predicted Memories ({memory_count})
{memory_section}

### Top Conventions
{conventions_section}
"""

_MEMORY_ITEM_TEMPLATE = "[{idx}] ({memory_type}, accessed {access_count}x)\n{content}"

_CONVENTION_ITEM_TEMPLATE = "- {content}"


def generate_briefing(
    store: MemoryStore,
    entity: str | None = None,
    tool_used: str | None = None,
) -> dict[str, Any]:
    """
    Generate a session-start briefing by combining:
    1. Predicted memories (from predict_context)
    2. L0 identity
    3. Top conventions (fact memories with convention=True)

    Targets ~500–800 tokens for injection into system prompt.

    Args:
        store: Open MemoryStore.
        entity: Optional entity context (passed to predict_context).
        tool_used: Optional tool context (passed to predict_context).

    Returns:
        {
            "briefing": str,          # formatted briefing text
            "token_estimate": int,    # rough token estimate (~4 chars/token)
            "sources": list[str],     # memory IDs included
        }
    """
    identity_layer = IdentityLayer(store)

    # 1. Predicted memories
    predicted = predict_context(store, entity=entity, tool_used=tool_used, top_k=5)

    # 2. Pattern count for header
    pattern_count_row = store.conn.execute(
        "SELECT COUNT(*) FROM access_patterns"
    ).fetchone()
    pattern_count = pattern_count_row[0] if pattern_count_row else 0

    # 3. Identity section
    identity_text = identity_layer.render()

    # 4. Memory section — format each predicted memory (truncate at 200 chars)
    sources: list[str] = []
    memory_lines: list[str] = []
    for idx, mem in enumerate(predicted, start=1):
        content = (mem.get("content") or "")[:200]
        if len(mem.get("content") or "") > 200:
            content += "..."
        memory_lines.append(
            _MEMORY_ITEM_TEMPLATE.format(
                idx=idx,
                memory_type=mem.get("memory_type", "fact"),
                access_count=mem.get("access_count", 0),
                content=content,
            )
        )
        sources.append(mem["id"])

    memory_section = (
        "\n\n".join(memory_lines) if memory_lines else "(no predictions yet — build up by using lore_recall)"
    )

    # 5. Top conventions — up to 5 fact memories tagged as conventions
    convention_rows = store.conn.execute(
        """
        SELECT content FROM memories
        WHERE memory_type = 'fact'
          AND decay_score > 0.0
          AND json_extract(metadata, '$.convention') = 1
        ORDER BY access_count DESC, created_at DESC
        LIMIT 5
        """
    ).fetchall()

    convention_lines = [
        _CONVENTION_ITEM_TEMPLATE.format(content=(row[0] or "")[:150])
        for row in convention_rows
    ]
    conventions_section = (
        "\n".join(convention_lines) if convention_lines else "(none stored — use lore_teach to add conventions)"
    )

    briefing = _BRIEFING_TEMPLATE.format(
        pattern_count=pattern_count,
        identity_section=identity_text,
        memory_count=len(predicted),
        memory_section=memory_section,
        conventions_section=conventions_section,
    ).strip()

    token_estimate = len(briefing) // 4

    return {
        "briefing": briefing,
        "token_estimate": token_estimate,
        "sources": sources,
    }
