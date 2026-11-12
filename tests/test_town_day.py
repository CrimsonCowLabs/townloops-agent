"""A whole town day (#64, seam 1): the deterministic schedule cron runs —
harvest, live sync, seed hunt and review, event-find, the 10-minute drain,
hourly moderation feedback and promote, the daily report — in-process
against the fake TownLoops (tests/autonomy_town.Town.run_day). What the day
must produce: pending submissions in the moderation queue and a report
whose "Needs you" names what only the operator can do."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.autonomy_town import PROFILE, Town, soon

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, hour=19, **kw):
    return {"title": title, "date": soon(days, hour), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def section(md: str, name: str) -> str:
    return md.split(f"## {name}")[1].split("\n## ")[0]


def test_a_day_submits_pending_and_reports_what_needs_the_operator(town):
    town.seed("Arena", ARENA)
    town.listing(ARENA, [concert("Jazz Night"), concert("Blues Night", days=8),
                         concert("Sunrise Run", days=9, hour=3)])   # pre-dawn: held

    day = town.run_day()

    assert sorted(town.submitted_titles()) == ["Blues Night", "Jazz Night"]
    assert {e["status"] for e in town.api.events.values()} == {"pending"}
    md = Path(day["report"]["report_path"]).read_text()
    assert "submitted 2" in section(md, "Events")
    needs = section(md, "Needs you")
    assert "held" in needs and "Sunrise Run" in needs
    assert "needs you" in town.telegrams[-1].lower()


def test_an_open_circuit_alerts_at_once_and_needs_the_operator(town):
    town.api.event_status = "published"   # the key can publish: fail closed
    town.seed("Arena", ARENA)
    town.listing(ARENA, [concert("Jazz Night"), concert("Blues Night", days=8)])

    day = town.run_day()

    assert len(town.submitted_events()) == 1   # the first create opened the circuit
    stopped = [t for t in town.telegrams if t.startswith("[slt] autonomous submissions STOPPED")]
    assert len(stopped) == 1                  # once, not on every tick
    assert "published-without-review" in town.telegrams[-1]   # and in the daily report's message
    needs = section(Path(day["report"]["report_path"]).read_text(), "Needs you")
    assert "published-without-review" in needs and "autonomy resume" in needs


@pytest.mark.slow
def test_a_500_event_source_drains_within_the_day(town):
    """The hourly windows are real hours and read-detail bursts on a deep
    backlog: a source that suddenly lists 500 events is submitted the same
    day while the moderator keeps up — not over weeks."""
    town.set_profile({**PROFILE, "workqueue_rate_limits": {
        "read-detail": {"per_hour": 30, "burst": 120}}})
    town.seed("Arena", ARENA)
    town.listing(ARENA, [concert(f"Show {i:03d}", days=2 + i % 90, hour=12 + i % 9)
                         for i in range(500)])

    day = town.run_day(moderate=Town.publish_all)

    assert len(town.submitted_events()) == 500
    assert day["ticks"] <= 144
    states = town.conn.execute("SELECT state, COUNT(*) FROM events GROUP BY state").fetchall()
    assert dict(states) == {"shipped": 500}
