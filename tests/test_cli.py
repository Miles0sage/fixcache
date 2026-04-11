"""
tests/test_cli.py — Tests for the lore-memory CLI entry point.
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


class TestCLIRemember:
    def test_remember_basic(self, db, capsys):
        rc = run(["remember", "User prefers dark mode"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Stored:" in out

    def test_remember_with_type(self, db, capsys):
        rc = run(["remember", "Had a great meeting", "--type", "experience"], db)
        assert rc == 0

    def test_remember_with_format(self, db, capsys):
        rc = run(["remember", "Some note", "--format", "plain"], db)
        assert rc == 0


class TestCLIRecall:
    def test_recall_finds_memory(self, db, capsys):
        run(["remember", "User prefers dark mode in all apps"], db)
        rc = run(["recall", "dark mode"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "dark mode" in out

    def test_recall_no_results(self, db, capsys):
        rc = run(["recall", "quantum physics zebra"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No results" in out

    def test_recall_json_output(self, db, capsys):
        import json
        run(["remember", "dark mode preference"], db)
        capsys.readouterr()  # flush "Stored:" output
        rc = run(["recall", "dark mode", "--json"], db)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_recall_top_k(self, db, capsys):
        for i in range(5):
            run(["remember", f"memory {i} about searching"], db)
        rc = run(["recall", "searching", "--top-k", "2"], db)
        assert rc == 0

    def test_recall_type_filter(self, db, capsys):
        run(["remember", "fact about python", "--type", "fact"], db)
        rc = run(["recall", "python", "--type", "fact"], db)
        assert rc == 0


class TestCLIStats:
    def test_stats_empty(self, db, capsys):
        rc = run(["stats"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total memories" in out

    def test_stats_after_remember(self, db, capsys):
        run(["remember", "a fact"], db)
        run(["remember", "b fact"], db)
        rc = run(["stats"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "2" in out


class TestCLIIdentity:
    def test_identity_get_empty(self, db, capsys):
        rc = run(["identity", "get"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "L0 IDENTITY" in out

    def test_identity_set_and_get(self, db, capsys):
        rc = run(["identity", "set", "name=Miles", "role=CTO"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Identity updated" in out

        run(["identity", "get"], db)
        out2 = capsys.readouterr().out
        assert "Miles" in out2

    def test_identity_set_invalid_pair(self, db, capsys):
        rc = run(["identity", "set", "invalidpair"], db)
        assert rc == 1

    def test_identity_clear(self, db, capsys):
        run(["identity", "set", "name=Alice"], db)
        rc = run(["identity", "clear"], db)
        assert rc == 0
        out = capsys.readouterr().out
        assert "cleared" in out
