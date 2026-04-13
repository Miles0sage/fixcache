"""
tests/test_cli.py — Tests for the fixcache CLI entry point.
"""

import pytest

from lore_memory.cli import main


@pytest.fixture
def db(tmp_path):
    """Path to a temp SQLite db for CLI tests."""
    return str(tmp_path / "test.db")


def run(args, db_path):
    """Helper: run CLI with --db pointed at temp db."""
    return main(["--db", db_path] + args)


class TestCLIStats:
    def test_stats_empty(self, db, capsys):
        rc = run(["stats"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total memories" in out
