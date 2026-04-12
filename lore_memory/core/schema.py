"""
core/schema.py — SQLite DDL and migration system for lore-memory
"""

from __future__ import annotations

import sqlite3

# Schema version — bump when DDL changes
SCHEMA_VERSION = 3

# All CREATE statements in dependency order
_DDL: list[str] = [
    # Core memories table
    """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        memory_type TEXT CHECK(memory_type IN ('fact','experience','opinion','meta')),
        source_format TEXT,
        created_at REAL NOT NULL,
        last_accessed REAL,
        access_count INTEGER DEFAULT 0,
        decay_score REAL DEFAULT 1.0,
        drawer_id INTEGER,
        chunk_index INTEGER,
        parent_id TEXT,
        metadata TEXT
    )
    """,
    # FTS5 virtual table — content mirror for BM25 search
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        content='memories',
        content_rowid='rowid',
        tokenize='porter unicode61'
    )
    """,
    # FTS5 triggers to keep virtual table in sync
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ai
    AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_ad
    AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_fts_au
    AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
        INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
    END
    """,
    # Knowledge graph nodes
    """
    CREATE TABLE IF NOT EXISTS kg_nodes (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        entity_type TEXT,
        first_seen REAL,
        last_seen REAL,
        mention_count INTEGER DEFAULT 1,
        metadata TEXT
    )
    """,
    # Knowledge graph edges
    """
    CREATE TABLE IF NOT EXISTS kg_edges (
        id TEXT PRIMARY KEY,
        src_id TEXT REFERENCES kg_nodes(id),
        dst_id TEXT REFERENCES kg_nodes(id),
        predicate TEXT NOT NULL,
        weight REAL DEFAULT 1.0,
        valid_from REAL,
        valid_to REAL,
        source_memory_id TEXT REFERENCES memories(id),
        created_at REAL
    )
    """,
    # L0 Identity — key/value YAML blobs
    """
    CREATE TABLE IF NOT EXISTS identity (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at REAL
    )
    """,
    # L1 Drawers — topic summaries
    """
    CREATE TABLE IF NOT EXISTS drawers (
        id INTEGER PRIMARY KEY,
        topic TEXT NOT NULL,
        summary TEXT,
        memory_count INTEGER DEFAULT 0,
        last_updated REAL
    )
    """,
    # Reflections — synthesized opinions, conflicts, etc.
    """
    CREATE TABLE IF NOT EXISTS reflections (
        id TEXT PRIMARY KEY,
        reflection_type TEXT CHECK(reflection_type IN ('opinion','conflict','promotion','decay')),
        content TEXT NOT NULL,
        source_memory_ids TEXT,
        created_at REAL,
        confidence REAL DEFAULT 0.5
    )
    """,
    # Darwin journal — retrieval outcomes for learning
    """
    CREATE TABLE IF NOT EXISTS darwin_journal (
        id TEXT PRIMARY KEY,
        query TEXT NOT NULL,
        result_ids TEXT,
        outcome TEXT CHECK(outcome IN ('success','failure','partial','corrected')),
        correction TEXT,
        timestamp REAL,
        metadata TEXT
    )
    """,
    # Darwin patterns — extracted from journal entries
    """
    CREATE TABLE IF NOT EXISTS darwin_patterns (
        id TEXT PRIMARY KEY,
        pattern_type TEXT,
        description TEXT,
        rule TEXT,
        frequency INTEGER DEFAULT 1,
        confidence REAL DEFAULT 0.5,
        created_at REAL,
        last_triggered REAL,
        metadata TEXT
    )
    """,
    # WAL log — every write recorded before commit
    """
    CREATE TABLE IF NOT EXISTS wal_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        operation TEXT NOT NULL,
        table_name TEXT NOT NULL,
        record_id TEXT,
        data TEXT,
        timestamp REAL NOT NULL
    )
    """,
    # Optional: neural embeddings
    """
    CREATE TABLE IF NOT EXISTS embeddings (
        memory_id TEXT PRIMARY KEY REFERENCES memories(id),
        vector BLOB NOT NULL,
        model TEXT NOT NULL,
        created_at REAL
    )
    """,
    # Schema version tracker
    """
    CREATE TABLE IF NOT EXISTS _schema_version (
        version INTEGER NOT NULL,
        applied_at REAL NOT NULL
    )
    """,
    # Access patterns — records what memories were accessed in what context
    # Used by the Prefetcher to predict future context needs
    """
    CREATE TABLE IF NOT EXISTS access_patterns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hour_of_day INTEGER,
        entity TEXT,
        tool_used TEXT,
        memory_ids TEXT,
        timestamp REAL
    )
    """,
    # Darwin fingerprints — the moat.
    # Normalized failure signatures with cross-repo aggregated efficacy.
    # Each row is one canonical failure class; darwin_patterns link back
    # via pattern.metadata.fingerprint_hash for efficacy rollup.
    """
    CREATE TABLE IF NOT EXISTS fingerprints (
        hash TEXT PRIMARY KEY,
        error_type TEXT NOT NULL,
        ecosystem TEXT NOT NULL,
        tool TEXT NOT NULL,
        essence TEXT NOT NULL,
        top_frame TEXT,
        total_seen INTEGER DEFAULT 1,
        total_success INTEGER DEFAULT 0,
        total_failure INTEGER DEFAULT 0,
        first_seen REAL NOT NULL,
        last_seen REAL NOT NULL,
        best_pattern_id TEXT,
        metadata TEXT
    )
    """,
]

# Indexes for performance
_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type)",
    "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_drawer ON memories(drawer_id)",
    "CREATE INDEX IF NOT EXISTS idx_memories_decay ON memories(decay_score)",
    "CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges(src_id)",
    "CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges(dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_kg_edges_predicate ON kg_edges(predicate)",
    "CREATE INDEX IF NOT EXISTS idx_kg_edges_valid ON kg_edges(valid_from, valid_to)",
    "CREATE INDEX IF NOT EXISTS idx_darwin_journal_timestamp ON darwin_journal(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_darwin_journal_outcome ON darwin_journal(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_wal_log_timestamp ON wal_log(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_access_patterns_hour ON access_patterns(hour_of_day)",
    "CREATE INDEX IF NOT EXISTS idx_access_patterns_entity ON access_patterns(entity)",
    "CREATE INDEX IF NOT EXISTS idx_access_patterns_tool ON access_patterns(tool_used)",
    "CREATE INDEX IF NOT EXISTS idx_access_patterns_timestamp ON access_patterns(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_error_type ON fingerprints(error_type)",
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_ecosystem ON fingerprints(ecosystem)",
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_last_seen ON fingerprints(last_seen)",
    "CREATE INDEX IF NOT EXISTS idx_fingerprints_total_seen ON fingerprints(total_seen)",
]


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply DDL and indexes to an open connection. Idempotent."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-8000")  # 8MB cache

    for stmt in _DDL:
        conn.execute(stmt)

    for stmt in _INDEXES:
        conn.execute(stmt)

    # Record schema version if not present; run migrations for existing DBs
    import time as _time
    row = conn.execute("SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1").fetchone()
    current_version = row[0] if row is not None else 0

    if current_version == 0:
        conn.execute(
            "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _time.time()),
        )
    elif current_version < SCHEMA_VERSION:
        # Migrations are idempotent — the CREATE TABLE IF NOT EXISTS
        # and CREATE INDEX IF NOT EXISTS statements above handle:
        #   v1 → v2: access_patterns table
        #   v2 → v3: fingerprints table + indexes (Darwin Replay moat)
        conn.execute(
            "INSERT INTO _schema_version(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, _time.time()),
        )

    conn.commit()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the current schema version, or 0 if unversioned."""
    try:
        row = conn.execute(
            "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0
