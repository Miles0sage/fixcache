# lore-memory-lite — Cut Status

This repo is a **knife-to-bone fork** of [lore-memory](https://github.com/Miles0sage/lore-memory-full) (private full version).
The goal is to sell exactly one story without distractions: **fix cache for AI coding agents**.

## What just happened (phase 1 — structural cut)

Six whole modules moved to `_graveyard/` instead of being deleted. They are recoverable by any git user who wants them back.

| Module | LOC | Why it went |
|---|---:|---|
| `_graveyard/lore_memory/hooks.py` | 153 | Speculative "install your hooks for you" is off-story. |
| `_graveyard/lore_memory/sync.py` | 258 | Dumps memories into CLAUDE.md / .cursorrules / etc. — a different product. |
| `_graveyard/lore_memory/cognition.py` | 417 | Second parallel RAG grafted on the side. Pollutes darwin_patterns with wiki chunks. |
| `_graveyard/lore_memory/prefetch.py` | 253 | Speculative "Copilot for memory" — write amplification on every recall. |
| `_graveyard/lore_memory/ingest.py` | 503 | Claude-Code-only auto-ingest, admitted "high-recall, noisy", pollutes the moat table. |
| `_graveyard/lore_memory/doctor.py` | 363 | Health-check CLI for a database. If SQLite breaks, fail loudly — don't engineer recovery. |
| **Subtotal** | **1,947** | |

Six test files moved in parallel: `test_hooks` (via test_sync), `test_sync.py`, `test_cognition.py`, `test_prefetch.py`, `test_ingest.py`, `test_doctor.py`, `test_windsurf_sync.py`.

## What's NOT yet cut (phase 2 — in-file surgery, queued)

The Agent 4 audit said the full cut is 67% of the codebase (~3,640 LOC). Phase 1 did ~55% of that. The remaining ~1,693 LOC lives inside `cli.py` and `mcp/server.py` as:

- `cli.py` — ~300 lines of subparsers and handlers for removed commands (hook, sync, ingest-wiki, ingest, doctor, teach, identity, activate duplicate, remember, recall, list, forget, stats duplicate)
- `mcp/server.py` — ~600 lines of handlers for removed MCP tools (`lore_knowledge`, `lore_briefing`, `lore_evolve`, `lore_rate_fix`, `lore_teach`, `lore_remember`, `lore_recall`, `lore_list`, `lore_forget`, `lore_stats` duplicate)
- `mcp/tools.py` — 11 of 16 schema entries removed
- `darwin.py` — drop `evolve_patterns` (168 lines) + `consolidate` (123 lines); keep `log_outcome` + `update_confidence`

**Time estimate for phase 2:** 4–6 hours of careful surgery with a full test run after each file.

## What's surviving (the product)

**5 CLI verbs:**
```
lore-memory watch <cmd>            # the activation loop
lore-memory fix <sig> --steps ...  # teach a recipe
lore-memory darwin classify        # stateless classification
lore-memory darwin report <id>     # record outcome (the feedback loop)
lore-memory stats                  # one-line proof it works
```

**5 MCP tools:**
- `lore_fix` — teach a recipe
- `lore_darwin_classify` — match stderr to learned fixes
- `lore_match_procedure` — alternative match entry point
- `lore_report_outcome` — record fix outcome (closes the Bayesian loop)
- `lore_darwin_stats` — efficacy dashboard

**Core files untouched:**
- `lore_memory/fingerprint.py` — the privacy-preserving normalization
- `lore_memory/darwin_replay.py` — fingerprint store + Bayesian efficacy
- `lore_memory/watch.py` — the activation loop
- `lore_memory/darwin.py` — slimmed; keeps log_outcome + update_confidence
- `lore_memory/core/store.py` — SQLite + FTS5 foundation

## The one feature to fight to keep even though it looks cuttable

`darwin_replay.export_sanitized()` + the `fingerprints` table. This is the only thing a competitor can't replicate in one sprint. Every other feature is rebuildable in an afternoon.

## Benchmark reality (honest, post-scrape)

A real held-out corpus (`bench/corpus_v2.jsonl`, 123 errors scraped from public GitHub issues by a separate agent who never touched `fingerprint.py`) scores v0.3.2:

| Metric | v1 (train-on-test, 45 samples) | v2 (held-out, 123 samples) |
|---|---:|---:|
| Collapse ratio | 4.09x | **1.78x** |
| Within-class precision | 100% | **48.6%** |
| Across-class purity | 100% | **100%** |

The v1 numbers were an artifact of handcrafting samples to match the fingerprinter. On real data, lore-memory collapses the common two-thirds of Python errors well and fails open on Rust warnings, CUDA-OOM variants, and Node `Cannot read properties`. **Purity holds — fixes never mis-fire between classes.** All weak points are fixable with targeted redactor additions; none require structural redesign.

## Why the cut matters

The prior critic called out three things, all confirmed:
1. The moat isn't the code, it's the corpus — and the corpus is empty.
2. The benchmark was train-on-test.
3. The 16-tool MCP surface was bloat that diluted the "failure recovery" story.

Cutting the bloat makes the positioning sharp. Keeping the full version private as `lore-memory-full` preserves the work in case any of the cut features turn out to be load-bearing for a customer.

## See also

- [lore-memory-full](https://github.com/Miles0sage/lore-memory-full) — private archive of the pre-cut version
- `bench/corpus_v2.jsonl` + `bench/corpus_v2_notes.md` — the held-out benchmark data and methodology
- `bench/results.md` — the fingerprint collapse benchmark before/after fix
