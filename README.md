# lore-memory

**The Failure Recovery Layer for AI coding agents.**

Your agents keep hitting the same errors. Lore catches every failure, learns the fix, and surfaces it the next time any agent on any repo hits the same wall.

[![PyPI](https://img.shields.io/pypi/v/lore-memory)](https://pypi.org/project/lore-memory/)
[![Tests](https://img.shields.io/badge/tests-427%20passing-brightgreen)](https://github.com/Miles0sage/lore-memory/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/pypi/pyversions/lore-memory)](https://pypi.org/project/lore-memory/)

## The 30-second demo

```bash
# 1. Teach it a fix
$ lore-memory fix "ModuleNotFoundError: No module named 'scikit-learn'" \
    --steps "pip install scikit-learn" "restart kernel"
Stored fix: recipe_id=...
  fingerprint : 714b7ee1df9f3bec (ModuleNotFoundError/python)

# 2. Run any command — lore-memory watches stderr
$ lore-memory watch --cmd "pytest tests/"
Traceback (most recent call last):
ModuleNotFoundError: No module named 'scikit-learn'

💡 lore-memory: matched fingerprint 714b7ee1df9f3bec
   seen 2x — unrated so far

  [1] Fix for: ModuleNotFoundError: No module named 'scikit-learn'  (conf=0.5, freq=1)
      → pip install scikit-learn
      → restart kernel
```

**That's the product.** Every failure becomes a fingerprint. Every fix gets Bayesian efficacy tracking. Every agent on every repo learns from every other agent's mistakes.

## Why it's different

- **Darwin Replay + Fingerprints** — normalized failure signatures with measured fix efficacy. `scikit-learn`, `pandas`, `numpy` all collapse to the same `python/ModuleNotFoundError` fingerprint, so fixes compound across repos.
- **MCP-first, framework-agnostic** — works with Claude Code, Cursor, Windsurf, Codex, and any MCP-compatible tool. No runtime lock-in.
- **Privacy-preserving by construction** — absolute paths, hex IDs, line numbers, quoted literals all redacted at fingerprint time. Safe to share corpus via `lore-memory darwin export`.
- **Local-first, zero cloud** — single SQLite file, WAL mode, FTS5 BM25 search. No cloud. No API keys. One dependency (`pyyaml`).
- **Bayesian efficacy** — every applied recipe updates alpha/beta counts. Failing recipes decay. Successful ones rise. Darwin Journal logs every outcome for audit.
- **Memory immune system** — SHA-256 provenance + trust hierarchy (user=1.0, agent=0.8, mined=0.6, fleet=0.5) prevents hallucinated facts from poisoning your memory.

## What's inside (v0.3.0)

- **16 MCP tools** — remember, recall, fix, match_procedure, teach, list, forget, stats, evolve, rate_fix, report_outcome, briefing, knowledge, **darwin_classify, darwin_stats, darwin_export**
- **3 CLI verbs worth remembering**: `lore-memory fix`, `lore-memory watch`, `lore-memory darwin classify`
- **Darwin Fingerprints** — cross-repo aggregated efficacy, exportable as sanitized corpus
- **Claude Code transcript ingest** — `lore-memory ingest last-session` auto-captures error→fix recipes from your session history
- **Sync to every agent** — `lore-memory sync` writes CLAUDE.md, .cursorrules, .windsurfrules, AGENTS.md
- **Doctor** — `lore-memory doctor --fix` auto-repairs FTS indexes, WAL mode, schema drift
- **427 tests** passing in 1.8s

## Install

```bash
pip install lore-memory
```

## Quick Start (60 seconds)

```bash
# Store a memory
lore-memory remember "We use PostgreSQL, never MySQL"

# Search memories
lore-memory recall "which database do we use"

# Show statistics
lore-memory stats

# Manage identity context (injected into every session)
lore-memory identity set name=Miles role=CTO project=lore
lore-memory identity get
```

## MCP Server (Claude Code, Cursor, Windsurf)

Add to your Claude Code / Cursor MCP settings:

```json
{
  "mcpServers": {
    "lore-memory": {
      "command": "python3",
      "args": ["-m", "lore_memory.mcp.server"]
    }
  }
}
```

Or set a custom database path:

```json
{
  "mcpServers": {
    "lore-memory": {
      "command": "python3",
      "args": ["-m", "lore_memory.mcp.server"],
      "env": {
        "LORE_MEMORY_DB": "/path/to/your/memory.db"
      }
    }
  }
}
```

### 6 MCP Tools

| Tool | Description |
|------|-------------|
| `lore_teach` | Store a convention, rule, or preference. Source defaults to `user` (trust 1.0). Auto-generates provenance hash. |
| `lore_remember` | Store any memory with explicit type and source. Returns memory ID + trust score + provenance hash. |
| `lore_recall` | FTS5 BM25 search with trust threshold, time window, and type filter. Touches accessed memories. |
| `lore_fix` | Store an error recipe: maps an error signature (string or regex) to solution steps. |
| `lore_match_procedure` | Find the best fix for a given error. Regex match first, FTS5 fallback. Returns solution steps. |
| `lore_stats` | Full system statistics: memory counts by type, trust level breakdown, darwin patterns, WAL entries. |

### Example MCP Usage

```
# In Claude Code (after adding MCP server):
lore_teach("Always use f-strings, never .format() or %")
lore_fix("ModuleNotFoundError: No module named", ["pip install -r requirements.txt", "check venv is activated"])
lore_recall("string formatting convention")
lore_match_procedure("ModuleNotFoundError: No module named 'requests'")
```

## Python API

```python
from lore_memory import LoreMemory

with LoreMemory() as mem:
    # Store memories
    mem.remember("User prefers dark mode")
    mem.remember("Always use async/await", memory_type="fact")

    # Search
    results = mem.recall("theme preference")
    for r in results:
        print(r["content"])

    # Identity context
    mem.identity.set({"name": "Miles", "role": "CTO"})

    # Statistics
    print(mem.stats())
```

## How It Works

```
Your input
    │
    ▼
┌─────────────┐    SHA-256 hash     ┌──────────────────┐
│  lore_teach │ ──────────────────► │  SQLite (WAL)    │
│  lore_fix   │   trust scoring     │  memories table  │
│  lore_remember                    │  darwin_journal  │
└─────────────┘                     │  darwin_patterns │
                                    │  identity        │
                                    └────────┬─────────┘
                                             │
                                    FTS5 BM25 index
                                             │
    ┌────────────────────────────────────────▼──────────────┐
    │  lore_recall      → ranked results by relevance       │
    │  lore_match_procedure → regex match → FTS5 fallback   │
    └───────────────────────────────────────────────────────┘
```

**Storage:** Single `~/.lore-memory/default.db` SQLite file. WAL mode for concurrent reads. FTS5 virtual table for BM25 full-text ranking.

**Trust scoring:** Every memory carries a `trust_score` (0.0–1.0) and SHA-256 `provenance_hash` based on content + timestamp. Recall filters by `min_trust` (default 0.5) to suppress low-confidence memories from polluting results.

**Error recipes:** `lore_fix` stores both a `darwin_pattern` (for fast regex matching) and a `memories` entry (for FTS5 fallback). `lore_match_procedure` tries regex first, falls back to BM25 search automatically.

**Decay:** Every memory has a `decay_score` (default 1.0). Memories accessed via `lore_recall` get their `access_count` incremented. Low-decay memories can be filtered out of search results.

## Configuration

Create `~/.lore-memory.yml` to override defaults:

```yaml
db_path: ~/.lore-memory/default.db

layers:
  search:
    top_k: 10
  temporal:
    decay_halflife_days: 30

darwin:
  enabled: true
  pattern_threshold: 3
```

Config search order: explicit path → `./.lore-memory.yml` → `~/.lore-memory.yml` → built-in defaults.

## vs Others

| Feature | lore-memory | claude-mem | mem0 | MemPalace |
|---------|-------------|------------|------|-----------|
| Works with ALL agents | Yes | Claude only | Claude/GPT | Claude only |
| Error recipes + pattern matching | Yes | No | No | No |
| Provenance hashes + trust scoring | Yes | No | No | No |
| Fully local, no cloud | Yes | Yes | No ($249/mo graph tier) | No |
| MCP server included | Yes | No | No | No |
| Python API | Yes | No | Yes | No |
| Dependencies | 1 (`pyyaml`) | — | Many | — |
| Free forever | Yes | Yes | Limited | Limited |

## Ecosystem

| Package | What it does |
|---------|-------------|
| [lore](https://github.com/Miles0sage/lore) | CLI agent reliability audits — scan agents for failure patterns |
| [lore-review](https://github.com/Miles0sage/lore-review) | Security scanner — OWASP/CVE scanning for Python projects |
| **lore-memory** | Persistent memory for AI agents (this package) |
| [phalanx](https://github.com/Miles0sage/phalanx) | Circuit breakers, DLQs, and compliance primitives for agent fleets |

## License

MIT — see [LICENSE](LICENSE)
