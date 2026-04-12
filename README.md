# fixcache

**The procedural memory layer for AI coding agents.**

Your agents keep hitting the same errors. `fixcache` fingerprints every failure, remembers the fix, and surfaces it the next time any agent on any repo hits the same wall.

[![PyPI](https://img.shields.io/pypi/v/fixcache)](https://pypi.org/project/fixcache/)
[![Tests](https://img.shields.io/badge/tests-292%20passing-brightgreen)](https://github.com/Miles0sage/fixcache/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/pypi/pyversions/fixcache)](https://pypi.org/project/fixcache/)

## The third leg of agent memory

| Memory type | What it stores | Example tool |
|---|---|---|
| **Semantic** | facts, preferences, knowledge | `mem0`, `cognee` |
| **Episodic** | conversation history, session traces | `Letta`, `claude-mem` |
| **Procedural** | *reusable fixes for errors that broke you before* | **`fixcache`** |

mem0 and Letta are passive filing cabinets — retrieve on cosine similarity. `fixcache` is an active immune system — it recognizes *the exact shape of failure* and intercepts it with a proven recipe.

## The 30-second demo

```bash
# 1. Teach it a fix
$ fixcache fix "ModuleNotFoundError: No module named 'scikit-learn'" \
    --steps "pip install scikit-learn" "restart kernel"
Stored fix: recipe_id=...
  fingerprint : 714b7ee1df9f3bec (ModuleNotFoundError/python)

# 2. Run any command — fixcache watches stderr
$ fixcache watch --cmd "pytest tests/"
Traceback (most recent call last):
ModuleNotFoundError: No module named 'scikit-learn'

💡 fixcache: matched fingerprint 714b7ee1df9f3bec
   seen 2x — unrated so far

  [1] Fix for: ModuleNotFoundError: No module named 'scikit-learn'  (conf=0.5, freq=1)
      → pip install scikit-learn
      → restart kernel
```

That's the product. One fingerprint per class of failure. One fix per fingerprint. Hit rate compounds across every repo and every agent that uses it.

## What fixcache is

- **Procedural memory, not semantic** — retrieves by exact pattern match on normalized error signatures, not by cosine similarity on embeddings
- **MCP-first, framework-agnostic** — works with Claude Code, Cursor, Windsurf, Codex, or any MCP-compatible tool. No runtime lock-in.
- **Zero cloud, zero deps** — single SQLite file, WAL mode, FTS5 BM25 search. stdlib only. No API keys. No telemetry leaving your machine.
- **Privacy-preserving by construction** — absolute paths, hex IDs, line numbers, quoted literals all redacted before hashing. Safe to share the sanitized corpus.
- **Local-first** — your failure history is yours.

## What fixcache is NOT

- Not a general-purpose memory layer (that's mem0)
- Not an agent runtime (that's Letta)
- Not an error tracker for humans (that's Sentry)
- Not a Claude-only plugin (that's claude-mem)

## Install

```bash
pip install fixcache
```

## 5 CLI verbs worth learning

```bash
fixcache watch <cmd>               # run a command, catch stderr, surface fixes
fixcache fix <signature> --steps   # teach a recipe for an error class
fixcache darwin classify           # stateless classification: stderr → fingerprint + recipe
fixcache darwin report <id>        # record outcome — closes the Bayesian feedback loop
fixcache stats                     # hit rate + efficacy dashboard
```

## 5 MCP tools worth connecting

```jsonc
{
  "mcpServers": {
    "fixcache": {
      "command": "python3",
      "args": ["-m", "fixcache.mcp.server"]
    }
  }
}
```

| Tool | What it does |
|---|---|
| `fix` | Teach a recipe for a specific error class |
| `darwin_classify` | Classify stderr → matched fingerprint + ranked recipes |
| `match_procedure` | Alternative match entry point |
| `report_outcome` | Record fix success/failure — closes the Bayesian loop |
| `darwin_stats` | Efficacy dashboard + hit rate per fingerprint |

## The honest benchmark

On a held-out corpus of **123 real GitHub errors** scraped from public issues — the author never saw these samples before running `fingerprint.py` on them:

| Metric | Value | What it means |
|---|---:|---|
| Samples | 123 | real errors from real repos |
| Unique fingerprints | 69 | distinct hashes |
| **Collapse ratio** | **1.78x** | on real data (was 4.09x on hand-tuned samples) |
| Within-class precision | 48.6% | average class agreement |
| **Across-class purity** | **100%** | **no false matches between classes — fixes never mis-fire** |

`fixcache` collapses the common two-thirds of Python errors well. It fails open on Rust compiler warnings, CUDA-OOM variants, and Node `Cannot read properties` — all documented, all fixable with targeted redactor additions, none require structural redesign.

**The metric that matters for safety:** across-class purity is 100%. A fix learned for one error class never mis-fires on another. The dataset stays safe even where it fails to compound.

Reproduce it yourself:
```bash
python bench/run_collapse.py --corpus bench/corpus_v2.jsonl
```

See `bench/corpus_v2_notes.md` for the source URLs, methodology, and documented weak points.

## Why this is defensible

The fingerprinter is ~250 lines of regex. Anyone can clone that. The moat is **the corpus**: fingerprints + measured outcomes. Every time a developer uses `fixcache` and rates a fix, that data point makes the next match better for everyone. No funded competitor can replicate a corpus of real user-rated agent failures without building the tool first.

That's the flywheel. Ship it, use it, rate it, share it.

## See also

- [`fixcache` corpus export](bench/corpus_v2.jsonl) — 123 real GitHub errors with equivalence class labels
- [`fixcache` corpus notes](bench/corpus_v2_notes.md) — methodology, source URLs, documented weak points
- [`CUT_STATUS.md`](CUT_STATUS.md) — what was cut from the original `lore-memory` kitchen sink and why
- `Miles0sage/lore-memory-full` (private) — archive of the pre-cut version if you need any of the cut features

## License

MIT — see [LICENSE](LICENSE)
