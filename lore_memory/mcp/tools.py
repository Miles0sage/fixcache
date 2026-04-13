"""
mcp/tools.py — Tool schema definitions for lore-memory MCP server.

Each entry: name -> {description, input_schema, handler}
Handlers are registered from server.py after import.
"""

from __future__ import annotations

# Trust score mapping by source_type
TRUST_SCORES: dict[str, float] = {
    "user": 1.0,
    "agent": 0.8,
    "mined": 0.6,
    "fleet": 0.5,
}

# Time window in seconds
TIME_WINDOWS: dict[str, float] = {
    "24h": 86400.0,
    "7d": 604800.0,
    "30d": 2592000.0,
}

# Tool schemas — handlers injected by server.py
TOOL_SCHEMAS: dict[str, dict] = {
    "lore_remember": {
        "description": (
            "Store a memory with provenance tracking and trust scoring. "
            "Auto-generates a SHA-256 hash from content+timestamp. "
            "Trust score assigned by source_type: user=1.0, agent=0.8, mined=0.6, fleet=0.5. "
            "Every write is recorded in the WAL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The text content to remember.",
                },
                "source_type": {
                    "type": "string",
                    "enum": ["user", "agent", "mined", "fleet"],
                    "description": "Source of the memory. Determines trust score. Default: agent.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "experience", "opinion", "meta"],
                    "description": "Memory classification. Default: fact.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorisation.",
                },
            },
            "required": ["content"],
        },
    },
    "lore_recall": {
        "description": (
            "Retrieve memories using FTS5 BM25 search. "
            "Filters by minimum trust_score (default 0.5). "
            "Returns top_k results (default 5) with layer attribution. "
            "Supports time_window filtering: '24h', '7d', '30d'. "
            "Touches recalled memories (increments access_count)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query for BM25 search.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Maximum number of results to return. Default: 5.",
                },
                "min_trust": {
                    "type": "number",
                    "description": "Minimum trust score threshold (0.0–1.0). Default: 0.5.",
                },
                "time_window": {
                    "type": "string",
                    "enum": ["24h", "7d", "30d"],
                    "description": "Optional time window to limit results to recent memories.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "experience", "opinion", "meta"],
                    "description": "Optional filter by memory type.",
                },
            },
            "required": ["query"],
        },
    },
    "lore_fix": {
        "description": (
            "Store an error recipe in procedural memory. "
            "Associates an error_signature (string or regex pattern) with solution_steps. "
            "Uses the darwin_journal for storage. "
            "Confidence starts at 0.5 and evolves via Bayesian updates on feedback."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "error_signature": {
                    "type": "string",
                    "description": "Error string or regex pattern to match against future errors.",
                },
                "solution_steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of steps to resolve the error.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags (e.g. ['python', 'import', 'sqlite']).",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure", "partial", "corrected"],
                    "description": "Outcome of applying this fix. Default: success.",
                },
            },
            "required": ["error_signature", "solution_steps"],
        },
    },
    "lore_match_procedure": {
        "description": (
            "Find the best fix recipe for a given error. "
            "Searches darwin_patterns for regex matches against current_error. "
            "Falls back to FTS5 search if no pattern match found. "
            "Returns the highest-confidence matching procedure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "current_error": {
                    "type": "string",
                    "description": "The stderr text or error message to match against stored recipes.",
                },
            },
            "required": ["current_error"],
        },
    },
    "lore_teach": {
        "description": (
            "Store a convention, rule, or preference as a fact memory. "
            "Use this to teach the system about coding standards, user preferences, "
            "project conventions, or any persistent rule. "
            "Auto-generates provenance hash."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "convention": {
                    "type": "string",
                    "description": "The convention, rule, or preference to remember.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorisation (e.g. ['python', 'style']).",
                },
                "source_type": {
                    "type": "string",
                    "enum": ["user", "agent", "mined", "fleet"],
                    "description": "Source of this convention. Default: user.",
                },
            },
            "required": ["convention"],
        },
    },
    "lore_stats": {
        "description": (
            "Return statistics about the lore-memory system: "
            "total memories, breakdown by type and trust level, "
            "darwin patterns count, WAL entry count, and identity summary."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "lore_list": {
        "description": (
            "List all memories with pagination. "
            "Returns id, content preview (first 100 chars), type, trust_score, and created_at. "
            "Optionally filter by memory_type."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return. Default: 20.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of memories to skip (for pagination). Default: 0.",
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["fact", "experience", "opinion", "meta"],
                    "description": "Optional filter by memory type.",
                },
            },
        },
    },
    "lore_forget": {
        "description": (
            "Soft-delete a memory by setting its decay_score to 0.0. "
            "The memory is not physically deleted — the audit trail is preserved in the WAL. "
            "The memory will be excluded from future recall results."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "The ID of the memory to forget.",
                },
            },
            "required": ["memory_id"],
        },
    },
    "lore_rate_fix": {
        "description": (
            "Rate the outcome of a fix pattern using Bayesian confidence update. "
            "success: increases confidence. failure: decreases confidence. "
            "Updates frequency count and logs to darwin_journal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern_id": {
                    "type": "string",
                    "description": "The darwin_patterns ID to rate (returned by lore_fix as pattern_id).",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure"],
                    "description": "Whether applying this fix succeeded or failed.",
                },
            },
            "required": ["pattern_id", "outcome"],
        },
    },
    "lore_report_outcome": {
        "description": (
            "Close the Darwin feedback loop: log the outcome of applying a fix and update "
            "its Bayesian confidence score. Call this after applying a fix returned by "
            "lore_match_procedure. outcome='success' raises confidence; 'failure' lowers it; "
            "'partial' makes a small neutral update."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern_id": {
                    "type": "string",
                    "description": "The darwin_patterns ID returned by lore_match_procedure or lore_fix.",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure", "partial"],
                    "description": "Result of applying the fix.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional context: error text, environment info, what was tried.",
                },
            },
            "required": ["pattern_id", "outcome"],
        },
    },
    "lore_evolve": {
        "description": (
            "Run the Darwin Evolution Engine: scan the journal for recurring failures, "
            "promote high-performing patterns, demote failing ones, flag errors that need "
            "new recipes, and prune stale low-value memories. "
            "Call periodically (e.g. after every 10 fixes) to keep the memory sharp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_failures": {
                    "type": "integer",
                    "description": "Minimum failure count before a pattern is demoted. Default: 3.",
                },
                "max_age_days": {
                    "type": "integer",
                    "description": "Age threshold in days for consolidation pruning. Default: 30.",
                },
            },
        },
    },
    "lore_briefing": {
        "description": (
            "Generate a session-start briefing by predicting what context you'll need. "
            "Combines: predicted memories (based on past access patterns for this hour/entity/tool), "
            "L0 identity, and top conventions. "
            "Call this on SessionStart to pre-load relevant context (~500-800 tokens). "
            "The prediction model builds automatically from lore_recall usage — no config needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "Optional entity/project context to narrow predictions (e.g. 'lore-memory', 'phalanx').",
                },
                "tool_used": {
                    "type": "string",
                    "description": "Optional tool context to narrow predictions (e.g. 'lore_recall', 'lore_fix').",
                },
            },
        },
    },
    "lore_darwin_classify": {
        "description": (
            "Darwin Replay: given raw error text, compute the normalized "
            "failure fingerprint and return the ranked fix recipes for this "
            "failure class with their measured success rates. "
            "Use this as the front door for 'I'm seeing this error again — "
            "what has worked before?'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "error_text": {
                    "type": "string",
                    "description": "Raw error text to classify.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max candidate recipes to return. Default: 3.",
                },
            },
            "required": ["error_text"],
        },
    },
    "lore_darwin_stats": {
        "description": (
            "Return corpus-wide Darwin fingerprint statistics: total "
            "fingerprints, top ecosystems, top error types, efficacy bands. "
            "This is the dashboard of the Darwin moat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "lore_darwin_export": {
        "description": (
            "Export the fingerprint corpus in a sanitized, shareable form. "
            "Fingerprints are already redacted by construction — this "
            "returns aggregate counts only, safe to publish or ship as "
            "a memory pack."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_total_seen": {
                    "type": "integer",
                    "description": "Floor for inclusion (drop rare noise). Default: 1.",
                },
            },
        },
    },
}
