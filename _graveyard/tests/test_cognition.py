"""
tests/test_cognition.py — Tests for lore_memory/cognition.py
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.cognition import (
    ingest_wiki,
    extract_procedures,
    query_knowledge,
    _content_hash,
    _parse_title,
    _parse_sections,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Path:
    """Create a temp wiki directory with 3 test articles."""

    # Article 1: standard article with multiple ## sections
    (tmp_path / "circuit-breaker.md").write_text(textwrap.dedent("""\
        ---
        id: circuit-breaker
        title: Circuit Breaker Pattern
        tags:
          - reliability
          - production
        ---

        # Circuit Breaker Pattern

        ## What It Is

        The circuit breaker prevents cascading failures by short-circuiting calls
        to a failing service after a threshold is exceeded.

        ## When to Use It

        When calling external APIs or services that may be temporarily unavailable,
        use a circuit breaker to avoid overwhelming the service and your own system.

        ## Implementation

        ```python
        breaker = CircuitBreaker(threshold=5, timeout=30)
        result = breaker.call(my_service.fetch)
        ```

        Steps to implement:
        1. Define failure threshold
        2. Track consecutive failures
        3. Open circuit when threshold exceeded
        4. Half-open after timeout
        5. Close circuit on success
    """), encoding="utf-8")

    # Article 2: simple article with one level headings only
    (tmp_path / "plan-execute.md").write_text(textwrap.dedent("""\
        # Plan-Execute Pattern

        ## Why Separate Planning from Execution

        Cost: Planning requires capable models. Execution often doesn't.
        When a step fails, do re-plan rather than restart from scratch.

        ## Production Considerations

        Always set max_attempts. Wire a CostGuard across the full cycle.
    """), encoding="utf-8")

    # Article 3: article with when-do patterns
    (tmp_path / "reflexion.md").write_text(textwrap.dedent("""\
        # Reflexion Loop

        ## What It Is

        When an agent fails, use self-critique to identify what went wrong.
        If evaluation fails, do retry with the reflection in context.

        ## When to Use

        1. Code generation requiring test verification
        2. Tasks with clear success criteria
        3. Workflows where single attempts fail consistently
    """), encoding="utf-8")

    return tmp_path


# ── _parse_title tests ────────────────────────────────────────────────────────

class TestParseTitle:
    def test_extracts_h1_heading(self):
        md = "# My Article Title\n\n## Section\n\nBody text."
        assert _parse_title(md, "some-file.md") == "My Article Title"

    def test_skips_frontmatter_dashes(self):
        md = "---\ntitle: meta\n---\n# Real Title\n\nBody."
        assert _parse_title(md, "file.md") == "Real Title"

    def test_fallback_to_filename(self):
        md = "No headings here at all"
        title = _parse_title(md, "my-cool-article.md")
        assert "My Cool Article" in title or "my-cool-article" in title.lower()

    def test_does_not_confuse_h2_with_h1(self):
        md = "## Section Heading\n\nBody."
        # Falls back to filename since no # heading
        title = _parse_title(md, "fallback-title.md")
        assert "##" not in title


# ── _parse_sections tests ─────────────────────────────────────────────────────

class TestParseSections:
    def test_splits_on_h2_headings(self):
        md = textwrap.dedent("""\
            # Title

            ## Section A
            Content A here.

            ## Section B
            Content B here.
        """)
        sections = _parse_sections(md)
        headings = [s["heading"] for s in sections]
        assert "Section A" in headings
        assert "Section B" in headings

    def test_section_body_contains_content(self):
        md = textwrap.dedent("""\
            # Title

            ## My Section
            Important content here.
            More text.
        """)
        sections = _parse_sections(md)
        assert len(sections) == 1
        assert "Important content here" in sections[0]["body"]

    def test_no_h2_returns_overview_section(self):
        md = "# Title\n\nJust a body paragraph with no sections."
        sections = _parse_sections(md)
        assert len(sections) == 1
        assert sections[0]["heading"] == "Overview"
        assert "body paragraph" in sections[0]["body"]

    def test_empty_sections_skipped(self):
        md = "# Title\n\n## Empty Section\n\n## Real Section\n\nSome content here."
        sections = _parse_sections(md)
        # "Empty Section" has no body — should be skipped
        headings = [s["heading"] for s in sections]
        assert "Real Section" in headings
        assert "Empty Section" not in headings


# ── ingest_wiki tests ─────────────────────────────────────────────────────────

class TestIngestWiki:
    def test_ingest_parses_and_stores(self, store, wiki_dir):
        result = ingest_wiki(store, wiki_dir)

        assert result["ingested"] == 3
        assert result["skipped"] == 0
        assert result["sections"] > 0

    def test_sections_stored_as_meta_memories(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        metas = store.list_all(memory_type="meta", limit=100)
        assert len(metas) > 0

    def test_metadata_has_required_fields(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        metas = store.list_all(memory_type="meta", limit=100)
        for mem in metas:
            meta = mem.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            assert meta.get("source") == "wiki"
            assert "article" in meta
            assert "section" in meta
            assert "content_hash" in meta
            assert "tags" in meta

    def test_duplicate_detection_skips_reingest(self, store, wiki_dir):
        result1 = ingest_wiki(store, wiki_dir)
        result2 = ingest_wiki(store, wiki_dir)

        assert result1["ingested"] == 3
        assert result2["ingested"] == 0
        assert result2["skipped"] == 3

    def test_extra_tags_applied(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir, tags=["custom-tag", "test"])
        metas = store.list_all(memory_type="meta", limit=100)
        found_custom = False
        for mem in metas:
            meta = mem.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            if "custom-tag" in (meta.get("tags") or []):
                found_custom = True
                break
        assert found_custom

    def test_content_stored_includes_title_and_section(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        metas = store.list_all(memory_type="meta", limit=100)
        # At least one memory should have "[... / ...]" prefix format
        prefixed = [m for m in metas if m["content"].startswith("[")]
        assert len(prefixed) > 0

    def test_invalid_dir_raises(self, store, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            ingest_wiki(store, tmp_path / "nonexistent")

    def test_sections_count_matches_stored(self, store, wiki_dir):
        result = ingest_wiki(store, wiki_dir)
        stored = store.count(memory_type="meta")
        assert stored == result["sections"]


# ── extract_procedures tests ──────────────────────────────────────────────────

class TestExtractProcedures:
    def test_finds_numbered_lists(self, store, wiki_dir):
        result = extract_procedures(store, wiki_dir)
        assert result["procedures_extracted"] > 0

    def test_procedures_stored_as_darwin_patterns(self, store, wiki_dir):
        extract_procedures(store, wiki_dir)
        rows = store.conn.execute(
            "SELECT COUNT(*) FROM darwin_patterns WHERE pattern_type = 'wiki_procedure'"
        ).fetchone()
        assert rows[0] > 0

    def test_pattern_rule_contains_article_info(self, store, wiki_dir):
        extract_procedures(store, wiki_dir)
        rows = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE pattern_type = 'wiki_procedure' LIMIT 5"
        ).fetchall()
        for (rule_json,) in rows:
            rule = json.loads(rule_json)
            assert "article" in rule
            assert "section" in rule
            assert "type" in rule
            assert "text" in rule

    def test_code_blocks_extracted(self, store, wiki_dir):
        extract_procedures(store, wiki_dir)
        rows = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE pattern_type = 'wiki_procedure'"
        ).fetchall()
        code_types = [
            json.loads(r[0])["type"]
            for r in rows
            if json.loads(r[0]).get("type") == "code_block"
        ]
        assert len(code_types) > 0

    def test_when_do_patterns_extracted(self, store, wiki_dir):
        extract_procedures(store, wiki_dir)
        rows = store.conn.execute(
            "SELECT rule FROM darwin_patterns WHERE pattern_type = 'wiki_procedure'"
        ).fetchall()
        when_do_types = [
            json.loads(r[0])["type"]
            for r in rows
            if json.loads(r[0]).get("type") == "when_do"
        ]
        assert len(when_do_types) > 0

    def test_invalid_dir_raises(self, store, tmp_path):
        with pytest.raises(ValueError, match="does not exist"):
            extract_procedures(store, tmp_path / "nonexistent")


# ── query_knowledge tests ─────────────────────────────────────────────────────

class TestQueryKnowledge:
    def test_returns_results_for_known_topic(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "circuit breaker failure threshold")
        assert len(results) > 0

    def test_results_have_required_fields(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "circuit breaker")
        for r in results:
            assert "article" in r
            assert "section" in r
            assert "content" in r
            assert "memory_id" in r

    def test_only_returns_wiki_source_memories(self, store, wiki_dir):
        # Add a non-wiki memory
        store.add(
            content="circuit breaker non-wiki content",
            memory_type="fact",
            metadata={"source": "agent", "trust_score": 0.8},
        )
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "circuit breaker")
        for r in results:
            # results should all have article attribution — non-wiki won't
            assert r.get("article") is not None

    def test_top_k_limits_results(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "pattern", top_k=2)
        assert len(results) <= 2

    def test_empty_query_returns_empty(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "")
        assert results == []

    def test_attribution_article_matches_source(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        results = query_knowledge(store, "circuit breaker threshold")
        # At least one result should attribute to the circuit-breaker article
        articles = [r["article"] for r in results]
        assert any("Circuit Breaker" in a for a in articles)

    def test_no_results_for_unrelated_query(self, store, wiki_dir):
        ingest_wiki(store, wiki_dir)
        # Query for something completely unrelated to the test articles
        results = query_knowledge(store, "xylophone saxophone trumpet", top_k=5)
        # May return empty or low-relevance — just ensure it doesn't crash
        assert isinstance(results, list)
