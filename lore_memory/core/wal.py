"""
core/wal.py — Write-ahead log for lore-memory.

Every mutation to the DB is recorded in wal_log before the actual write.
This gives us an audit trail and replay capability.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


class WAL:
    """
    Write-ahead logger. Records every write operation into wal_log
    before the actual table mutation is committed.

    Usage:
        wal = WAL(conn)
        wal.record("INSERT", "memories", record_id="abc123", data={"content": "..."})
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(
        self,
        operation: str,
        table_name: str,
        record_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> int:
        """
        Insert a WAL entry.

        Args:
            operation: INSERT, UPDATE, DELETE, or UPSERT
            table_name: target table name
            record_id: primary key of the affected record (optional)
            data: the payload being written, serialized as JSON

        Returns:
            The rowid of the new wal_log entry.
        """
        now = time.time()
        data_json = json.dumps(data) if data is not None else None

        cursor = self._conn.execute(
            "INSERT INTO wal_log(operation, table_name, record_id, data, timestamp) VALUES (?, ?, ?, ?, ?)",
            (operation.upper(), table_name, record_id, data_json, now),
        )
        return cursor.lastrowid

    def tail(self, n: int = 50) -> list[dict[str, Any]]:
        """Return the last N WAL entries, most recent first."""
        rows = self._conn.execute(
            "SELECT id, operation, table_name, record_id, data, timestamp "
            "FROM wal_log ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "operation": r[1],
                "table_name": r[2],
                "record_id": r[3],
                "data": json.loads(r[4]) if r[4] else None,
                "timestamp": r[5],
            }
            for r in rows
        ]

    def count(self) -> int:
        """Total number of WAL entries."""
        row = self._conn.execute("SELECT COUNT(*) FROM wal_log").fetchone()
        return row[0]

    def prune(self, older_than: float) -> int:
        """
        Delete WAL entries older than `older_than` (Unix timestamp).
        Returns the number of rows deleted.
        """
        cursor = self._conn.execute(
            "DELETE FROM wal_log WHERE timestamp < ?", (older_than,)
        )
        self._conn.commit()
        return cursor.rowcount
