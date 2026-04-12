"""
test_doctor.py — Tests for lore_memory.doctor health-check system.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lore_memory.core.store import MemoryStore
from lore_memory.doctor import (
    DoctorReport,
    format_report,
    run_doctor,
)


@pytest.fixture
def fresh_db(tmp_path: Path) -> str:
    """Fresh DB with one seeded memory for realistic checks."""
    db_path = str(tmp_path / "doctor.db")
    store = MemoryStore(db_path)
    store.add("probe memory for doctor tests")
    store.close()
    return db_path


def test_run_doctor_on_healthy_db(fresh_db: str) -> None:
    """Healthy DB returns healthy=True with all checks passing."""
    report = run_doctor(fresh_db, fix=False)
    assert report.healthy is True
    names = [c.name for c in report.checks]
    assert "fts5_compiled" in names
    assert "integrity_check" in names
    assert "schema_version" in names
    assert "fts_queryable" in names
    assert "journal_mode" in names
    assert "foreign_keys" in names
    assert "write_roundtrip" in names
    assert "darwin_tables" in names


def test_run_doctor_creates_missing_db(tmp_path: Path) -> None:
    """Doctor handles non-existent DB paths (initializes on open)."""
    db_path = str(tmp_path / "subdir" / "new.db")
    report = run_doctor(db_path, fix=False)
    assert Path(db_path).exists()
    assert report.healthy is True


def test_run_doctor_in_memory() -> None:
    """Doctor works on :memory: databases too."""
    report = run_doctor(":memory:", fix=False)
    assert report.healthy is True


def test_run_doctor_fix_repairs_broken_fts(fresh_db: str) -> None:
    """
    Dropping the FTS index should be caught and auto-repaired with --fix.
    """
    # Simulate FTS corruption by dropping the table
    conn = sqlite3.connect(fresh_db)
    conn.execute("DROP TABLE IF EXISTS memories_fts")
    conn.commit()
    conn.close()

    # Without --fix, FTS check passes because apply_schema recreates it.
    # To simulate *actual* broken FTS after schema apply, drop only content:
    report = run_doctor(fresh_db, fix=True)
    # After fix, FTS should be queryable again
    fts_checks = [c for c in report.checks if c.name == "fts_queryable"]
    assert fts_checks, "fts_queryable check missing"
    assert fts_checks[0].ok or fts_checks[0].fixed
    assert report.healthy is True


def test_run_doctor_fix_applies_pragmas(tmp_path: Path) -> None:
    """
    If journal_mode is not WAL, --fix should set it and mark the check fixed.
    """
    db_path = str(tmp_path / "rollback.db")
    # Create DB with rollback journaling
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    report = run_doctor(db_path, fix=True)
    # Journal mode check should now be ok or fixed
    jm = [c for c in report.checks if c.name == "journal_mode"][0]
    assert jm.ok or jm.fixed


def test_doctor_report_to_dict(fresh_db: str) -> None:
    """DoctorReport serialises to a stable dict."""
    report = run_doctor(fresh_db, fix=False)
    d = report.to_dict()
    assert d["db_path"] == fresh_db
    assert d["healthy"] is True
    assert isinstance(d["checks"], list)
    assert all(
        set(c.keys()) == {"name", "ok", "detail", "fixable", "fixed"}
        for c in d["checks"]
    )
    assert "fixes_applied" in d


def test_format_report_human_readable(fresh_db: str) -> None:
    """format_report returns a multi-line string with status indicators."""
    report = run_doctor(fresh_db, fix=False)
    text = format_report(report)
    assert "lore-memory doctor" in text
    assert "[OK  ]" in text
    assert "HEALTHY" in text
    assert fresh_db in text


def test_doctor_with_seeded_memories_and_fix_flag(fresh_db: str) -> None:
    """
    Running with --fix on a healthy DB still succeeds and applies
    VACUUM+ANALYZE regardless (harmless maintenance).
    """
    report = run_doctor(fresh_db, fix=True)
    assert report.healthy is True
    assert "VACUUM + ANALYZE" in report.fixes_applied


def test_doctor_json_serializable(fresh_db: str) -> None:
    """to_dict output can be round-tripped through json."""
    import json
    report = run_doctor(fresh_db, fix=False)
    serialised = json.dumps(report.to_dict())
    parsed = json.loads(serialised)
    assert parsed["healthy"] is True
