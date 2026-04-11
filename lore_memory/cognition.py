"""
cognition.py — Cognition Loader for lore-memory.

Transforms wiki articles into injectable knowledge:
  ingest_wiki()         — scan wiki dir, parse + store article sections as meta memories
  extract_procedures()  — extract actionable patterns and store as darwin_patterns
  query_knowledge()     — search wiki-ingested memories with article/section attribution
  handle_lore_knowledge — MCP tool handler wrapping query_knowledge
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .core.store import MemoryStore


# ── Markdown parsing helpers ──────────────────────────────────────────────────

def _content_hash(content: str) -> str:
    """SHA-256 of file content for duplicate detection."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_title(content: str, filename: str) -> str:
    """Extract title from first # heading, fall back to filename stem."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    # Fall back to filename without extension, replacing dashes/underscores
    return Path(filename).stem.replace("-", " ").replace("_", " ").title()


def _parse_sections(content: str) -> list[dict[str, str]]:
    """
    Split markdown into sections by ## headings.

    Returns list of {"heading": str, "body": str}.
    If no ## headings exist, returns the whole document as one section
    with heading equal to the document title (first # heading) or "Overview".
    """
    lines = content.splitlines()
    sections: list[dict[str, str]] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        # Detect ## headings (but not ### or deeper)
        if re.match(r"^## [^#]", stripped):
            # Save previous section
            if current_heading is not None:
                body = "\n".join(current_lines).strip()
                if body:
                    sections.append({"heading": current_heading, "body": body})
            current_heading = stripped[3:].strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Save last section
    if current_heading is not None:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append({"heading": current_heading, "body": body})

    # No ## sections found — treat whole doc as one section
    if not sections:
        # Strip frontmatter and first # heading from body
        body_lines = []
        skip_frontmatter = False
        in_frontmatter = False
        for i, line in enumerate(lines):
            if i == 0 and line.strip() == "---":
                in_frontmatter = True
                skip_frontmatter = True
                continue
            if in_frontmatter and line.strip() == "---":
                in_frontmatter = False
                continue
            if in_frontmatter:
                continue
            if line.strip().startswith("# ") and not line.strip().startswith("## "):
                continue  # skip title line
            body_lines.append(line)
        body = "\n".join(body_lines).strip()
        if body:
            sections.append({"heading": "Overview", "body": body})

    return sections


def _already_ingested(store: MemoryStore, content_hash: str) -> bool:
    """Check if a file with this content hash was already ingested."""
    row = store.conn.execute(
        """
        SELECT id FROM memories
        WHERE memory_type = 'meta'
          AND json_extract(metadata, '$.source') = 'wiki'
          AND json_extract(metadata, '$.content_hash') = ?
        LIMIT 1
        """,
        (content_hash,),
    ).fetchone()
    return row is not None


# ── Public API ────────────────────────────────────────────────────────────────

def ingest_wiki(
    store: MemoryStore,
    wiki_dir: str | Path,
    tags: list[str] | None = None,
) -> dict[str, int]:
    """
    Scan wiki_dir for .md files and ingest each article into the store.

    Each ## section becomes a separate "meta" memory with metadata:
        {source: "wiki", article: <title>, section: <heading>,
         content_hash: <sha256>, tags: [...]}

    Duplicate detection: files already ingested (by content hash) are skipped.

    Args:
        store:    MemoryStore to write into.
        wiki_dir: Directory containing .md wiki articles.
        tags:     Optional extra tags applied to every ingested memory.

    Returns:
        {"ingested": N, "skipped": N, "sections": N}
    """
    wiki_path = Path(wiki_dir).expanduser().resolve()
    if not wiki_path.is_dir():
        raise ValueError(f"wiki_dir does not exist or is not a directory: {wiki_path}")

    extra_tags = list(tags or [])
    ingested = 0
    skipped = 0
    total_sections = 0

    md_files = sorted(wiki_path.glob("*.md"))

    for md_file in md_files:
        raw = md_file.read_text(encoding="utf-8", errors="replace")
        chash = _content_hash(raw)

        if _already_ingested(store, chash):
            skipped += 1
            continue

        title = _parse_title(raw, md_file.name)
        sections = _parse_sections(raw)

        for section in sections:
            heading = section["heading"]
            body = section["body"]
            section_tags = ["wiki", title.lower().replace(" ", "-")] + extra_tags

            metadata: dict[str, Any] = {
                "source": "wiki",
                "article": title,
                "section": heading,
                "content_hash": chash,
                "filename": md_file.name,
                "tags": section_tags,
                "trust_score": 0.9,
                "source_type": "mined",
            }

            store.add(
                content=f"[{title} / {heading}]\n\n{body}",
                memory_type="meta",
                source_format="wiki",
                metadata=metadata,
            )
            total_sections += 1

        ingested += 1

    return {"ingested": ingested, "skipped": skipped, "sections": total_sections}


# ── Procedure extraction helpers ──────────────────────────────────────────────

_NUMBERED_LIST_RE = re.compile(
    r"(?:^|\n)(?:(\d+)\.\s+.+\n?){2,}",
    re.MULTILINE,
)

_WHEN_DO_RE = re.compile(
    r"(?:when|if)\s+.{5,80}[,;]\s*(?:do|use|call|run|invoke|trigger|apply)\s+.{5,80}",
    re.IGNORECASE,
)

_CODE_BLOCK_RE = re.compile(
    r"```(?:\w+)?\n([\s\S]+?)```",
)


def _extract_numbered_lists(text: str) -> list[str]:
    """Find numbered list blocks of 2+ items and return each as a joined string."""
    found: list[str] = []
    # Split into paragraphs and look for runs of numbered lines
    paragraphs = re.split(r"\n{2,}", text)
    for para in paragraphs:
        lines = para.strip().splitlines()
        numbered = [l for l in lines if re.match(r"^\d+\.\s+", l.strip())]
        if len(numbered) >= 2:
            found.append("\n".join(numbered))
    return found


def _extract_when_do_patterns(text: str) -> list[str]:
    """Find 'when X, do Y' patterns."""
    return _WHEN_DO_RE.findall(text)


def _extract_code_blocks(text: str) -> list[str]:
    """Extract fenced code blocks."""
    return _CODE_BLOCK_RE.findall(text)


def extract_procedures(
    store: MemoryStore,
    wiki_dir: str | Path,
) -> dict[str, int]:
    """
    Scan wiki_dir for actionable patterns and store as darwin_patterns.

    Looks for:
    - Numbered lists (step-by-step instructions)
    - "when X, do Y" patterns
    - Code blocks within articles

    Each procedure is stored with pattern_type="wiki_procedure".
    The trigger pattern uses keywords from the article title + section.

    Args:
        store:    MemoryStore to write into.
        wiki_dir: Directory containing .md wiki articles.

    Returns:
        {"procedures_extracted": N}
    """
    wiki_path = Path(wiki_dir).expanduser().resolve()
    if not wiki_path.is_dir():
        raise ValueError(f"wiki_dir does not exist or is not a directory: {wiki_path}")

    count = 0
    now = time.time()

    for md_file in sorted(wiki_path.glob("*.md")):
        raw = md_file.read_text(encoding="utf-8", errors="replace")
        title = _parse_title(raw, md_file.name)
        sections = _parse_sections(raw)
        title_keywords = re.sub(r"[^a-z0-9 ]", "", title.lower()).split()

        for section in sections:
            heading = section["heading"]
            body = section["body"]
            heading_keywords = re.sub(r"[^a-z0-9 ]", "", heading.lower()).split()
            keywords = list(dict.fromkeys(title_keywords + heading_keywords))  # preserve order, dedupe
            trigger_pattern = "|".join(k for k in keywords if len(k) > 3)

            if not trigger_pattern:
                continue

            procedures: list[tuple[str, str]] = []  # (procedure_type, text)

            # 1. Numbered lists
            for lst in _extract_numbered_lists(body):
                procedures.append(("numbered_list", lst))

            # 2. When-do patterns
            for wd in _extract_when_do_patterns(body):
                procedures.append(("when_do", wd))

            # 3. Code blocks
            for code in _extract_code_blocks(body):
                code_stripped = code.strip()
                if len(code_stripped) > 20:  # skip trivial snippets
                    procedures.append(("code_block", code_stripped))

            for proc_type, proc_text in procedures:
                pattern_id = str(uuid.uuid4())
                description = f"[wiki:{title} / {heading}] {proc_type}"
                rule = json.dumps({
                    "article": title,
                    "section": heading,
                    "type": proc_type,
                    "text": proc_text[:500],
                })

                store.wal.record(
                    "INSERT", "darwin_patterns", record_id=pattern_id,
                    data={"title": title, "section": heading, "proc_type": proc_type},
                )
                store.conn.execute(
                    """
                    INSERT INTO darwin_patterns
                        (id, pattern_type, description, rule,
                         frequency, confidence, created_at, last_triggered)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (pattern_id, "wiki_procedure", description, rule,
                     1, 0.7, now, now),
                )
                count += 1

    store.conn.commit()
    return {"procedures_extracted": count}


# ── Knowledge query ───────────────────────────────────────────────────────────

def query_knowledge(
    store: MemoryStore,
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Search wiki-ingested memories (source="wiki") using FTS5 BM25.

    Returns ranked results with article + section attribution.

    Args:
        store: MemoryStore to search.
        query: Natural language query.
        top_k: Maximum results to return.

    Returns:
        List of result dicts: [{article, section, content, relevance, memory_id}]
    """
    if not query or not query.strip():
        return []

    # Use FTS5 to get candidate memories, then filter to wiki source
    # Fetch more candidates than needed since we filter by metadata
    raw_results = store.search(query, top_k=top_k * 4, memory_type="meta")

    results: list[dict[str, Any]] = []
    for mem in raw_results:
        meta = mem.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}

        if not isinstance(meta, dict) or meta.get("source") != "wiki":
            continue

        article = meta.get("article", "Unknown")
        section = meta.get("section", "")
        content = mem.get("content", "")
        tags = meta.get("tags", [])

        # Strip the "[Title / Section]\n\n" prefix from content for clean display
        display_content = re.sub(r"^\[.+?/.+?\]\n\n", "", content, count=1).strip()

        results.append({
            "memory_id": mem["id"],
            "article": article,
            "section": section,
            "content": display_content,
            "tags": tags,
            "created_at": mem.get("created_at"),
        })

        if len(results) >= top_k:
            break

    return results


# ── MCP tool handler ──────────────────────────────────────────────────────────

def handle_lore_knowledge(
    query: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """
    MCP tool handler: search wiki-ingested cognition base.

    Parameters:
        query:  Natural language search query.
        top_k:  Maximum number of results (default 5).

    Returns dict with key "results" — list of {article, section, content, relevance}.
    """
    if not query or not isinstance(query, str):
        return {
            "query": query,
            "results": [],
            "count": 0,
            "error": "query must be a non-empty string",
        }
    if not isinstance(top_k, int) or top_k < 1:
        top_k = 5

    # Import here to avoid circular import — server.py imports cognition.py
    from .mcp.server import _get_store
    store = _get_store()

    results = query_knowledge(store, query, top_k=top_k)
    return {
        "query": query,
        "results": results,
        "count": len(results),
    }
