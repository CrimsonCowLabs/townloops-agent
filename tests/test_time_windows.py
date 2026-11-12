"""Windows over ISO timestamps ('2026-11-12T06:41:28+00:00') against SQLite's
datetime('now', …) ('2026-11-12 14:21:38'). Compared as text, 'T' sorts after
' ', so every row earlier the same UTC day fell inside any window. Each query
normalises the column with datetime(col) first."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")


@pytest.fixture
def db_path(fresh_db, tmp_state_dir):
    return tmp_state_dir / "townloops.db"


def test_current_task_ignores_activity_older_than_90_seconds(fresh_db, db_path, monkeypatch):
    from engine import inspection
    monkeypatch.setattr(inspection, "_DB_PATH", db_path)
    fresh_db.execute("INSERT INTO agent_activity (ts, worker, action) VALUES (?, 'w', 'old')",
                     (_ago(minutes=10),))
    fresh_db.commit()
    assert inspection.current_task() is None

    fresh_db.execute("INSERT INTO agent_activity (ts, worker, action) VALUES (?, 'w', 'new')",
                     (_ago(seconds=5),))
    fresh_db.commit()
    assert inspection.current_task()["action"] == "new"


def test_recent_source_runs_keeps_only_the_window(fresh_db, db_path, monkeypatch):
    import dashboard.db as dashboard_db
    monkeypatch.setattr(dashboard_db, "_DB_PATH", str(db_path))
    for source, started in (("seed:old", _ago(hours=3)), ("seed:new", _ago(minutes=20))):
        fresh_db.execute("INSERT INTO source_runs (source, started_at, ok) VALUES (?, ?, 1)",
                         (source, started))
    fresh_db.commit()
    assert [r["source"] for r in dashboard_db.recent_source_runs(hours=1)] == ["seed:new"]


def test_events_by_source_keeps_only_the_window(fresh_db, db_path, monkeypatch):
    import dashboard.db as dashboard_db
    monkeypatch.setattr(dashboard_db, "_DB_PATH", str(db_path))
    for uid, source, seen in (("a", "seed:old", _ago(days=3)), ("b", "seed:new", _ago(hours=1))):
        fresh_db.execute(
            "INSERT INTO events(event_uid, source, source_kind, name, date, date_day,"
            " first_seen_at, last_seen_at, content_hash, last_modified_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid, source, "html", "E", seen, seen[:10], seen, seen, "h" + uid, seen))
    fresh_db.commit()
    assert [r["source"] for r in dashboard_db.events_by_source(days=1)] == ["seed:new"]
