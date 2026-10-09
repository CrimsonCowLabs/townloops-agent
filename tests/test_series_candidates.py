"""The weekly Series-candidates report, through the skill's entry point
against the fake API: Clusters with no Series that pass TownLoops' rule (two
or more town-local dates at one resolved Venue) are listed for a curator;
same-evening duplicates and performer Clusters are not; Series that span
several venues or have same-day duplicates are flagged for the Series audit.
Read-only: nothing is written to TownLoops."""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dateutil import tz

from tests.fake_townloops import FakeAPI


TZ = tz.gettz("America/Los_Angeles")
SERIES = [{"id": 11, "slug": "trivia-thursday", "title": "Trivia Thursday"}]


def local(days: int, hour: int = 19) -> str:
    d = (datetime.now(timezone.utc) + timedelta(days=days)).astimezone(TZ).date()
    return datetime(d.year, d.month, d.day, hour, tzinfo=TZ).astimezone(
        timezone.utc).isoformat(timespec="seconds")


def ev(slug, title, start, venue_id=7, series_slug=None, status="published"):
    return {"id": abs(hash(slug)) % 10000, "slug": slug, "title": title, "start_at": start,
            "venue_id": venue_id, "venue_slug": f"venue-{venue_id}" if venue_id else None,
            "source_url": None, "category_slugs": [], "tags": [], "status": status,
            "series_slug": series_slug, "series_id": 11 if series_slug else None}


@pytest.fixture
def report(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    path = Path(__file__).resolve().parent.parent / "skills/business/series-candidates/report.py"
    spec = importlib.util.spec_from_file_location("series_candidates_report", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: fresh_db)
    sent: list[str] = []
    monkeypatch.setattr("engine.orchestrator._telegram_notify", sent.append)

    def go(api):
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        assert mod.main([]) == 0
        [md] = (tmp_state_dir / "reports").glob("series-candidates-*.md")
        return md.read_text()

    go.telegrams = sent
    return go


def test_report_lists_standing_events_and_drift_only(report):
    api = FakeAPI([
        # a weekly open mic with no Series: two dates, one venue (one row unbound)
        ev("mic-1", "Open Mic Night", local(2)), ev("mic-2", "Open Mic Night!", local(9), None),
        # one evening ingested twice: one date
        ev("fest-a", "Fall Fest", local(4, 17)), ev("fest-b", "Fall Fest", local(4, 19)),
        # a touring performer: three venues
        ev("jon-1", "Jon Sills", local(3), 7), ev("jon-2", "Jon Sills", local(5), 8),
        ev("jon-3", "Jon Sills", local(6), 9),
        # not published: not part of the population
        ev("draft-1", "Pottery Class", local(2), status="pending"),
        ev("draft-2", "Pottery Class", local(9), status="pending"),
        # a Series that has drifted onto two venues and picked up a same-day copy
        ev("t-1", "Trivia Thursday", local(1), 7, "trivia-thursday"),
        ev("t-2", "Trivia Thursday", local(8), 8, "trivia-thursday"),
        ev("t-3", "Trivia Thursday", local(8, 20), 8, "trivia-thursday"),
    ], series=SERIES)
    md = report(api)
    assert "Open Mic Night" in md and "open-mic-night" in md
    assert "Fall Fest" not in md and "Jon Sills" not in md and "Pottery" not in md
    assert "trivia-thursday" in md and "2 venues" in md
    [msg] = report.telegrams
    assert "1 Cluster" in msg and "series-candidates-" in msg
    assert not [c for c in api.calls if c[0] in ("POST", "PATCH")]


def test_nothing_to_report_sends_nothing(report):
    md = report(FakeAPI([ev("one", "Solo Show", local(2))], series=SERIES))
    assert "No Clusters" in md
    assert report.telegrams == []
