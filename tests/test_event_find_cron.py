"""event-find under cron: the wrapper detaches past Hermes' 120s script
timeout, and the skill's own lock stops overlapping runs."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _activity(db: Path) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT action, ok, detail, error FROM agent_activity WHERE worker = 'event-find'"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def find(fresh_db, tmp_state_dir, monkeypatch):
    mod = _load(ROOT / "skills/business/event-find/find.py", "event_find_cron")
    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")
    return mod


class TestOverlap:
    def test_second_run_skips_while_first_holds_the_lock(self, find, tmp_state_dir, monkeypatch, capsys):
        from engine import lock
        ran = []
        monkeypatch.setattr(find, "cmd_find", lambda args: ran.append(args) or 0)

        with lock.acquire("event-find"):
            assert find.main([]) == 0

        assert ran == []
        assert "previous run still active" in capsys.readouterr().out
        assert [(a, ok) for a, ok, _, _ in _activity(tmp_state_dir / "townloops.db")] == [
            ("skipped-overlap", 1)]

    def test_runs_once_the_lock_is_free(self, find, monkeypatch):
        ran = []
        monkeypatch.setattr(find, "cmd_find", lambda args: ran.append(args) or 0)
        assert find.main(["--limit", "2"]) == 0
        assert ran[0].limit == 2


class TestOutcomeVisibleToOperator:
    def test_crash_is_recorded_for_the_dashboard(self, find, tmp_state_dir, monkeypatch):
        def boom(args):
            raise RuntimeError("firecrawl exploded")
        monkeypatch.setattr(find, "cmd_find", boom)

        with pytest.raises(RuntimeError):
            find.main([])

        rows = _activity(tmp_state_dir / "townloops.db")
        assert [(a, ok) for a, ok, _, _ in rows] == [("run-failed", 0)]
        assert "firecrawl exploded" in rows[0][3]

    def test_crash_mid_write_still_reaches_the_dashboard(self, find, fresh_db, tmp_state_dir,
                                                        monkeypatch):
        """A crash with the run's write transaction open (e.g. a schema the DB
        hasn't migrated to) must not leave the DB locked against the
        failure record."""
        fresh_db.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
                         "VALUES ('Arena', 'https://arena.test/events', 'venue', 'approved', '2026-10-01', '2026-10-01')")
        fresh_db.commit()
        monkeypatch.setattr(find, "_load_town_profile", lambda: {})
        monkeypatch.setattr(find, "_firecrawl_markdown", lambda url: (True, "md", ""))
        monkeypatch.setattr(find, "_build_extraction_prompt", lambda *a: "prompt")
        monkeypatch.setattr(find, "_codex_exec", lambda p: (True, "[]", ""))

        def half_written(conn, seed, **kw):
            conn.execute("UPDATE event_seeds SET last_scraped_at = 'x'")
            raise sqlite3.OperationalError("no such column: health_status")
        monkeypatch.setattr("engine.source_health.record_run", half_written)

        with pytest.raises(sqlite3.OperationalError):
            find.main([])

        rows = _activity(tmp_state_dir / "townloops.db")
        assert ("run-failed", 0) in [(a, ok) for a, ok, _, _ in rows]


class TestCronWrapper:
    def test_detaches_and_returns_before_the_run_finishes(self, tmp_state_dir, tmp_path,
                                                          monkeypatch, capsys):
        skill = tmp_path / "fake_find.py"
        skill.write_text(
            "import os, sys, time\n"
            "time.sleep(1.5)\n"
            "print('args', sys.argv[1:], 'sid', os.getsid(0), flush=True)\n"
        )
        wrapper = _load(ROOT / "scripts/event_find.py", "event_find_wrapper")
        monkeypatch.setattr(wrapper, "SKILL", skill)
        monkeypatch.setattr(sys, "argv", ["event_find.py", "--limit", "3"])
        monkeypatch.setenv("PYTHON", sys.executable)

        t0 = time.monotonic()
        assert wrapper.main() == 0
        assert time.monotonic() - t0 < 1.0

        logs = list((tmp_state_dir / "logs").glob("event-find-*.log"))
        assert len(logs) == 1
        assert str(logs[0]) in capsys.readouterr().out

        deadline = time.monotonic() + 10
        while "args" not in logs[0].read_text() and time.monotonic() < deadline:
            time.sleep(0.1)
        out = logs[0].read_text()
        assert "['--limit', '3']" in out
        child_sid = int(out.split("sid")[1].split()[0])
        assert child_sid != os.getsid(0)   # own session: survives the cron script being killed

    def test_missing_skill_fails_loudly(self, tmp_state_dir, tmp_path, monkeypatch):
        wrapper = _load(ROOT / "scripts/event_find.py", "event_find_wrapper_missing")
        monkeypatch.setattr(wrapper, "SKILL", tmp_path / "nope.py")
        assert wrapper.main() == 1
