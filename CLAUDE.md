# fixcache (this project)

**The procedural memory layer for AI coding agents.** Fingerprints agent stderr, matches to learned fix recipes, tracks Bayesian efficacy. Zero cloud, zero deps. Public at `github.com/Miles0sage/fixcache`.

## Commands
- `lore-memory watch -- <cmd>` — run cmd, capture stderr, surface matching fix recipes
- `lore-memory fix "<signature>" --steps "<step1>" "<step2>"` — teach a recipe
- `lore-memory darwin classify` — stateless classify from stdin
- `lore-memory darwin report <pattern_id> success|failure` — rate a fix (closes efficacy loop)
- `lore-memory stats` — hit rate + efficacy dashboard (your daily KPI)
- `python -m pytest -q --timeout=60` — 552+ tests, must stay green

## Architecture (5 core files only)
- `lore_memory/fingerprint.py` — `compute_fingerprint()` + `_TARGETED_REDACTORS` (smell library)
- `lore_memory/darwin_replay.py` — `upsert_fingerprint`, `classify`, `record_outcome`, `export_sanitized` (the moat)
- `lore_memory/watch.py` — `watch_command`, `WatchResult`, `classify_and_format`
- `lore_memory/mcp/server.py` — JSON-RPC 2.0 MCP server (hardened: isinstance guard + notification drop)
- `lore_memory/core/store.py` — SQLite+FTS5 store, WAL mode

## Gotchas
- **`fix` and `watch` must produce the same hash for the same error**. Bug A (psf/requests dogfood) and Bug A2 (httpx dogfood) both lived here. `_pick_final_line` strips pytest `FAILED <path> - ` prefixes; `_detect_ecosystem` infers from error type when text cues fail.
- **Honest benchmark is `bench/corpus_v2.jsonl`** (123 real held-out errors, 1.78x collapse, 100% purity). NOT `corpus.jsonl` (45 train-on-test samples, lying 4.09x).
- **Phase 2 CLI cut is NOT done yet** — `cli.py` still wires 18 verbs even though only 5 are supported. Fix before PyPI upload.
- **Don't ship without a fresh dogfood on a NEW repo** — "dogfood as a gate, not a ribbon."

## Anti-patterns
- Don't add features until phase 2 cut lands. Surface is a lie until then.
- Don't write tests without adversarial inputs (see `tests/test_*_hardening.py`).
- Don't skip the held-out corpus re-run before tagging a release.

## When in doubt — STOP and read:
1. `/root/claude-code-agentic/.omc/wiki/dogfood-as-gate.md`
2. `/root/claude-code-agentic/.omc/wiki/bug-fix-chronicle.md`
3. `bench/corpus_v2_notes.md`
