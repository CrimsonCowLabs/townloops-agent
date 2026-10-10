"""Tests for engine/db.py — connection defaults + atomic migrations.

The atomic-migration work (P1.3, 2026-07-08) fixed a real bug where
`executescript()`'s implicit COMMIT could leave a half-applied migration
recorded as `applied` in `schema_version`. These tests pin the new
behavior so a well-meaning refactor can't regress it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest


class TestSplitSql:
    def test_strips_line_comments(self):
        from engine.db import _split_sql
        text = "-- comment 1\nCREATE TABLE a (x INT);\n-- comment 2\nCREATE TABLE b (y INT);"
        stmts = _split_sql(text)
        assert len(stmts) == 2
        assert "CREATE TABLE a" in stmts[0]
        assert "CREATE TABLE b" in stmts[1]
        assert "comment" not in " ".join(stmts)

    def test_respects_single_quoted_semicolon(self):
        from engine.db import _split_sql
        # A ; inside a string literal must NOT be a statement separator.
        text = "INSERT INTO t VALUES ('a;b'); SELECT 1;"
        stmts = _split_sql(text)
        assert len(stmts) == 2
        assert stmts[0] == "INSERT INTO t VALUES ('a;b')"
        assert stmts[1] == "SELECT 1"

    def test_respects_double_quoted_semicolon(self):
        from engine.db import _split_sql
        text = 'CREATE TABLE "a;b" (x INT); SELECT 1;'
        stmts = _split_sql(text)
        assert len(stmts) == 2
        assert '"a;b"' in stmts[0]

    def test_empty_returns_empty(self):
        from engine.db import _split_sql
        assert _split_sql("") == []
        assert _split_sql("\n\n\n") == []
        assert _split_sql("-- just a comment") == []

    def test_trailing_stmt_without_semicolon(self):
        from engine.db import _split_sql
        # Some migrations don't end with ; — we shouldn't drop the last statement.
        assert _split_sql("SELECT 1") == ["SELECT 1"]


class TestConnectPath:
    """DEFAULT_DB_PATH is evaluated at module-import time. importlib.reload
    forces the module body to re-execute so the assertion sees the effect of
    monkeypatched env — sys.modules.pop alone doesn't work because
    `from engine import db` resolves through the still-cached parent package.
    """

    @staticmethod
    def _reload_db():
        import importlib
        from engine import db as db_mod
        return importlib.reload(db_mod)

    def test_derives_from_hermes_home(self, monkeypatch, tmp_path):
        """P3.1: DEFAULT_DB_PATH honors $HERMES_HOME, not a hardcoded /home/agent path."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("TOWNLOOPS_DB_PATH", raising=False)
        db = self._reload_db()
        assert db.DEFAULT_DB_PATH == str(tmp_path / "state" / "townloops.db")

    def test_falls_back_to_docker_default(self, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.delenv("TOWNLOOPS_DB_PATH", raising=False)
        db = self._reload_db()
        assert db.DEFAULT_DB_PATH == "/home/agent/.hermes/state/townloops.db"

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", "/ignored/path")
        monkeypatch.setenv("TOWNLOOPS_DB_PATH", "/explicit/path.db")
        db = self._reload_db()
        assert db.DEFAULT_DB_PATH == "/explicit/path.db"


class TestRunMigrations:
    def test_applies_all_shipped_migrations(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        applied = db_mod.run_migrations(conn)
        # There are 7 migrations shipped as of 2026-07-08 (through 007).
        # If new ones land, this test's expectation adjusts — but the count
        # should never GO DOWN.
        assert len(applied) >= 7
        # First and last known migrations by prefix.
        assert applied[0].startswith("001_")
        assert applied[-1][:3].isdigit() and applied[-1][3] == "_"

    def test_idempotent_rerun(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        first = db_mod.run_migrations(conn)
        assert first  # sanity
        second = db_mod.run_migrations(conn)
        assert second == [], "second run must apply zero migrations"

    def test_schema_version_populated(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        applied = db_mod.run_migrations(conn)
        rows = conn.execute("SELECT filename FROM schema_version ORDER BY filename").fetchall()
        recorded = [r["filename"] for r in rows]
        assert recorded == sorted(applied)


class TestMigrationAtomicity:
    """P1.3 rollback semantics — the whole point of the atomic-migration work."""

    def test_broken_migration_rolls_back_schema(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        # Apply the real migrations first so we have a clean baseline.
        db_mod.run_migrations(conn)
        # Now write a broken migration to a tmpfile and try to apply it.
        broken = tmp_path / "999_broken.sql"
        broken.write_text("CREATE TABLE half_committed (x INT);\nBOGUS STATEMENT HERE;\n")
        with pytest.raises(sqlite3.OperationalError):
            db_mod._apply_one_migration(conn, broken)
        # The half-committed table must NOT exist — rollback fired.
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='half_committed'"
        ).fetchone()
        assert row is None, "broken migration must not leave a partial schema behind"

    def test_broken_migration_does_not_record_schema_version(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        db_mod.run_migrations(conn)
        broken = tmp_path / "999_broken.sql"
        broken.write_text("CREATE TABLE t (x INT);\nNOT A REAL STATEMENT;\n")
        with pytest.raises(sqlite3.OperationalError):
            db_mod._apply_one_migration(conn, broken)
        row = conn.execute(
            "SELECT filename FROM schema_version WHERE filename='999_broken.sql'"
        ).fetchone()
        assert row is None, "broken migration must not be recorded as applied"

    def test_successful_migration_records_schema_version(self, tmp_path):
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_path / "test.db"))
        db_mod.run_migrations(conn)
        good = tmp_path / "999_good.sql"
        good.write_text("CREATE TABLE good_table (x INT);\n")
        db_mod._apply_one_migration(conn, good)
        assert conn.execute(
            "SELECT filename FROM schema_version WHERE filename='999_good.sql'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='good_table'"
        ).fetchone() is not None


class TestConcurrentMigrations:
    """Every cron entry point migrates (issue 25), so two jobs firing together
    race on the same pending files."""

    def test_racing_runners_each_succeed_and_record_files_once(self, tmp_path):
        import threading
        from engine import db as db_mod
        path = str(tmp_path / "race.db")
        db_mod.connect(path).close()   # create the WAL DB before the race
        errors: list[BaseException] = []
        start = threading.Barrier(4)

        def runner():
            conn = db_mod.connect(path)
            try:
                start.wait()
                db_mod.run_migrations(conn)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=runner) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        conn = db_mod.connect(path)
        rows = [r["filename"] for r in conn.execute("SELECT filename FROM schema_version")]
        assert sorted(rows) == sorted(p.name for p in db_mod.MIGRATIONS_DIR.glob("*.sql"))

    def test_file_applied_by_another_process_is_skipped(self, tmp_path):
        from engine import db as db_mod
        path = str(tmp_path / "test.db")
        first, second = db_mod.connect(path), db_mod.connect(path)
        db_mod.run_migrations(first)
        good = tmp_path / "999_good.sql"
        good.write_text("CREATE TABLE good_table (x INT);\n")
        assert db_mod._apply_one_migration(first, good) is True
        # `second` read schema_version before `first` applied the file.
        assert db_mod._apply_one_migration(second, good) is False


class TestMigrate:
    def test_resolves_db_path_at_call_time(self, tmp_path, monkeypatch):
        from engine import db as db_mod
        target = tmp_path / "later.db"
        monkeypatch.setenv("TOWNLOOPS_DB_PATH", str(target))

        applied = db_mod.migrate()

        assert applied and applied[0].startswith("001_")
        conn = sqlite3.connect(target)
        assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(applied)

    def test_falls_back_to_hermes_home(self, tmp_path, monkeypatch):
        from engine import db as db_mod
        monkeypatch.delenv("TOWNLOOPS_DB_PATH", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        db_mod.migrate()
        assert (tmp_path / "state" / "townloops.db").exists()

    def test_second_call_applies_nothing(self, tmp_path, monkeypatch):
        from engine import db as db_mod
        monkeypatch.setenv("TOWNLOOPS_DB_PATH", str(tmp_path / "t.db"))
        db_mod.migrate()
        assert db_mod.migrate() == []


class TestParseExtraJson:
    def test_an_object_is_returned_and_anything_else_is_empty(self):
        from engine.derive_helpers import parse_extra_json
        assert parse_extra_json('{"live_id": 7}') == {"live_id": 7}
        for raw in (None, "", "not json", "[1, 2]", "3"):
            assert parse_extra_json(raw) == {}


class TestSeedRef:
    def test_a_seed_reference_round_trips_and_anything_else_is_none(self):
        from engine.derive_helpers import seed_name_of, seed_ref
        assert seed_ref("Lake Tahoe Arena") == "seed:Lake Tahoe Arena"
        assert seed_name_of(seed_ref("Lake Tahoe Arena")) == "Lake Tahoe Arena"
        for ref in (None, "", "Lake Tahoe Arena", "event:x"):
            assert seed_name_of(ref) is None
