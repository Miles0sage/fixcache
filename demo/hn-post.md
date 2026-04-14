# Show HN: fixcache – procedural memory for AI coding agents (SQLite, no cloud, MIT)

I noticed my Claude Code and Cursor agents were hitting the same ModuleNotFoundError,
the same SQLAlchemy DetachedInstanceError, the same Pydantic v2 migration error —
over and over, session after session. They have no long-term memory of fixes.

fixcache fingerprints every stderr failure, stores the proven recipe, and surfaces it
the next time any agent on any repo hits the same wall. It's ecosystem-safe: Python
errors never return Rust recipes, Node errors never return Python recipes. 100% cross-class
purity on 123 real GitHub errors means fixes never misfire.

**How it works:**
- `fixcache watch --cmd "pytest tests/"` — wraps any command, intercepts failures
- Error is fingerprinted (normalized, privacy-safe hash — paths/secrets redacted)
- Ecosystem gating: matches are scoped to the originating language/runtime (v0.4.7)
- Matching fix recipe surfaces instantly with confidence score
- `fixcache darwin report <id> success` — Bayesian update, confidence compounds
- Works across repos: ModuleNotFoundError in project A = same fingerprint as project B

**Claude Code hook (auto-fires on every failing Bash command):**
```bash
fixcache hook install .
```
After that, every time Claude runs a bash command that fails, fixcache automatically
intercepts the stderr and surfaces matching fixes. Zero extra steps.

**MCP server** (Cursor, Windsurf, any MCP-compatible tool):
```bash
fixcache-mcp  # exposes darwin_classify, fix, report_outcome, stats
```

**Zero dependencies. Local SQLite. MIT.**
No API keys. No telemetry. Your fix corpus lives at ~/.lore-memory/default.db.

**What it is NOT:**
- Not mem0 (semantic memory for facts)
- Not Letta (agent runtime with episodic memory)
- Not Sentry (error tracker for humans)

It's the third type of agent memory nobody built yet: procedural — reusable fixes
for failures that broke you before. And unlike naive "store everything" approaches,
it gates matches by ecosystem so a Rust fix never poisons a Python project.

GitHub: https://github.com/Miles0sage/fixcache
PyPI: pip install fixcache

---

# Reddit / Discord version (shorter)

I got tired of watching my Claude Code agents hit the same ModuleNotFoundError
in every new session and re-learn the fix from scratch.

Built fixcache: it fingerprints errors, stores fix recipes, and surfaces them
automatically via a PostToolUse hook. Local SQLite, zero deps, MIT.

`pip install fixcache && fixcache init` — takes 90 seconds to wire into Claude Code.

The interesting part is the Darwin loop: Bayesian confidence scoring on fix recipes.
Every success/failure updates confidence. Over time the corpus self-ranks by what
actually works in production.

github.com/Miles0sage/fixcache
