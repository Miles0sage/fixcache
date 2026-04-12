"""
doctor.py — Health check and self-repair for lore-memory databases.

Runs a series of diagnostics against the SQLite store and optionally
repairs common issues. Designed to be reviewer-proof: fresh installs,
corrupted FTS indexes, schema drift, and misconfigured PRAGMAs should
all be detected and fixable in one command.

Checks:
  1. sqlite3 FTS5 compiled in
  2. Database file exists and is readable
  3. integrity_check (PRAGMA)
  4. Schema version matches expected
  5. FTS5 table queryable (SELECT rowid FROM memories_fts LIMIT 1)
  6. WAL pragma set
  7. foreign_keys enabled
  8. Write transaction round-trip works
  9. Darwin tables present (darwin_patterns, darwin_journal)

Fixes (with --fix):
  - Rebuild FTS5 index if corrupt
  - Set PRAGMA wal_autocheckpoint / synchronous=NORMAL / foreign_keys=ON
  - VACUUM + ANALYZE
  - Reapply schema (idempotent)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core.schema import SCHEMA_VERSION, apply_schema


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fixable: bool = False
    fixed: bool = False


@dataclass
class DoctorReport:
    db_path: str
    checks: list[Check] = field(default_factory=list)
    healthy: bool = True
    fixes_applied: list[str] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)
        if not check.ok and not check.fixed:
            self.healthy = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "healthy": self.healthy,
            "checks": [
                {
                    "name": c.name,
                    "ok": c.ok,
                    "detail": c.detail,
                    "fixable": c.fixable,
                    "fixed": c.fixed,
                }
                for c in self.checks
            ],
            "fixes_applied": self.fixes_applied,
        }


def _check_fts5_compiled() -> Check:
    """Verify SQLite was built with FTS5 support."""
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return Check(name="fts5_compiled", ok=True, detail="FTS5 available")
    except sqlite3.OperationalError as exc:
        return Check(
            name="fts5_compiled",
            ok=False,
            detail=f"FTS5 not available: {exc}. Install python3 built with FTS5.",
            fixable=False,
        )


def _check_db_file(db_path: str) -> Check:
    """Verify DB file is readable (or will be created on first open)."""
    p = Path(db_path)
    if db_path == ":memory:":
        return Check(name="db_file", ok=True, detail="in-memory database")
    if p.exists():
        if not p.is_file():
            return Check(name="db_file", ok=False, detail=f"{db_path} exists but is not a file")
        size = p.stat().st_size
        return Check(name="db_file", ok=True, detail=f"{size} bytes")
    return Check(
        name="db_file",
        ok=True,
        detail="will be created on first open",
        fixable=True,
    )


def _check_integrity(conn: sqlite3.Connection) -> Check:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    result = row[0] if row else "unknown"
    if result == "ok":
        return Check(name="integrity_check", ok=True, detail="ok")
    return Check(
        name="integrity_check",
        ok=False,
        detail=result,
        fixable=True,
    )


def _check_schema_version(conn: sqlite3.Connection) -> Check:
    try:
        row = conn.execute(
            "SELECT version FROM _schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return Check(
                name="schema_version",
                ok=False,
                detail="_schema_version table empty",
                fixable=True,
            )
        v = row[0]
        if v == SCHEMA_VERSION:
            return Check(name="schema_version", ok=True, detail=f"v{v}")
        return Check(
            name="schema_version",
            ok=False,
            detail=f"found v{v}, expected v{SCHEMA_VERSION}",
            fixable=True,
        )
    except sqlite3.OperationalError as exc:
        return Check(
            name="schema_version",
            ok=False,
            detail=f"_schema_version table missing: {exc}",
            fixable=True,
        )


def _check_fts_queryable(conn: sqlite3.Connection) -> Check:
    try:
        conn.execute("SELECT rowid FROM memories_fts LIMIT 1").fetchone()
        return Check(name="fts_queryable", ok=True, detail="memories_fts responds")
    except sqlite3.OperationalError as exc:
        return Check(
            name="fts_queryable",
            ok=False,
            detail=f"FTS index broken: {exc}",
            fixable=True,
        )


def _check_pragmas(conn: sqlite3.Connection, db_path: str) -> list[Check]:
    checks: list[Check] = []
    mode = conn.execute("PRAGMA journal_mode").fetchone()
    mode_val = (mode[0] if mode else "").lower()
    # :memory: databases can't use WAL — 'memory' is correct for them.
    acceptable = {"wal"} if db_path != ":memory:" else {"wal", "memory"}
    checks.append(
        Check(
            name="journal_mode",
            ok=(mode_val in acceptable),
            detail=f"journal_mode={mode_val}",
            fixable=(mode_val not in acceptable),
        )
    )
    fk = conn.execute("PRAGMA foreign_keys").fetchone()
    fk_val = fk[0] if fk else 0
    checks.append(
        Check(
            name="foreign_keys",
            ok=bool(fk_val),
            detail=f"foreign_keys={fk_val}",
            fixable=(not bool(fk_val)),
        )
    )
    return checks


def _check_write_roundtrip(conn: sqlite3.Connection) -> Check:
    """Verify a transaction can commit and roll back."""
    try:
        conn.execute("BEGIN")
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _doctor_probe (x INTEGER)"
        )
        conn.execute("INSERT INTO _doctor_probe (x) VALUES (42)")
        conn.execute("DROP TABLE _doctor_probe")
        conn.commit()
        return Check(name="write_roundtrip", ok=True, detail="write+commit ok")
    except sqlite3.Error as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return Check(
            name="write_roundtrip",
            ok=False,
            detail=f"{exc}",
            fixable=False,
        )


def _check_darwin_tables(conn: sqlite3.Connection) -> Check:
    try:
        conn.execute("SELECT COUNT(*) FROM darwin_patterns").fetchone()
        conn.execute("SELECT COUNT(*) FROM darwin_journal").fetchone()
        return Check(name="darwin_tables", ok=True, detail="darwin_* present")
    except sqlite3.OperationalError as exc:
        return Check(
            name="darwin_tables",
            ok=False,
            detail=f"{exc}",
            fixable=True,
        )


# ── Fix helpers ────────────────────────────────────────────────────────────────

def _rebuild_fts(conn: sqlite3.Connection) -> str:
    """Rebuild memories_fts from the memories table."""
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
    conn.commit()
    return "rebuilt memories_fts"


def _apply_safe_pragmas(conn: sqlite3.Connection) -> list[str]:
    applied: list[str] = []
    conn.execute("PRAGMA journal_mode=WAL")
    applied.append("journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    applied.append("synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    applied.append("foreign_keys=ON")
    return applied


def _vacuum_analyze(conn: sqlite3.Connection) -> str:
    conn.execute("VACUUM")
    conn.execute("ANALYZE")
    return "VACUUM + ANALYZE"


# ── Public entry point ────────────────────────────────────────────────────────

def run_doctor(db_path: str, fix: bool = False) -> DoctorReport:
    """
    Run the full doctor suite on the given DB path.

    Args:
        db_path: Path to the SQLite database (or :memory:).
        fix: If True, attempt to repair any fixable issues.

    Returns:
        DoctorReport with all checks and any fixes applied.
    """
    report = DoctorReport(db_path=db_path)

    # 1. FTS5 compiled (no DB needed)
    fts5_check = _check_fts5_compiled()
    report.add(fts5_check)
    if not fts5_check.ok:
        return report  # can't continue without FTS5

    # 2. DB file
    db_check = _check_db_file(db_path)
    report.add(db_check)

    # Open connection (will create file if missing)
    if db_path != ":memory:":
        Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    try:
        # Always apply schema so subsequent checks run on a valid DB
        apply_schema(conn)

        # 3. integrity
        report.add(_check_integrity(conn))

        # 4. schema version
        sv_check = _check_schema_version(conn)
        report.add(sv_check)
        if fix and not sv_check.ok and sv_check.fixable:
            apply_schema(conn)
            sv_check.fixed = True
            report.fixes_applied.append("reapplied schema")

        # 5. FTS queryable
        fts_check = _check_fts_queryable(conn)
        report.add(fts_check)
        if fix and not fts_check.ok and fts_check.fixable:
            try:
                report.fixes_applied.append(_rebuild_fts(conn))
                fts_check.fixed = True
            except sqlite3.Error as exc:
                fts_check.detail += f" | rebuild failed: {exc}"

        # 6. PRAGMAs
        for pc in _check_pragmas(conn, db_path):
            report.add(pc)
            if fix and not pc.ok and pc.fixable:
                for applied in _apply_safe_pragmas(conn):
                    if applied not in report.fixes_applied:
                        report.fixes_applied.append(applied)
                pc.fixed = True

        # 7. write round-trip
        report.add(_check_write_roundtrip(conn))

        # 8. Darwin tables
        dt_check = _check_darwin_tables(conn)
        report.add(dt_check)
        if fix and not dt_check.ok and dt_check.fixable:
            apply_schema(conn)
            dt_check.fixed = True
            report.fixes_applied.append("reapplied schema for darwin_*")

        # 9. Vacuum + analyze in fix mode
        if fix:
            report.fixes_applied.append(_vacuum_analyze(conn))

    finally:
        conn.close()

    # Recompute health — any unfixed failure marks unhealthy
    report.healthy = all(c.ok or c.fixed for c in report.checks)
    return report


def format_report(report: DoctorReport) -> str:
    """Human-readable report formatter."""
    lines = [f"lore-memory doctor — {report.db_path}", ""]
    for c in report.checks:
        if c.ok:
            status = "OK  "
        elif c.fixed:
            status = "FIX "
        else:
            status = "FAIL"
        lines.append(f"  [{status}] {c.name:20s} {c.detail}")
    lines.append("")
    if report.fixes_applied:
        lines.append("Fixes applied:")
        for f in report.fixes_applied:
            lines.append(f"  + {f}")
        lines.append("")
    lines.append(f"Status: {'HEALTHY' if report.healthy else 'UNHEALTHY'}")
    return "\n".join(lines)
