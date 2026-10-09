"""event-reconcile and TownLoops Series, through the skill's `run` entry
point against the fake API (Firecrawl + Codex faked): recurrence is decided
by `series_slug` when the live row has one — a new date in a Series is a new
occurrence, never a reschedule — and by today's title/source_url rules for a
one-off; the Series live rows report reaches the local rows they were
shipped as."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from dateutil import tz

from engine import reconcile as rc
from tests.fake_townloops import FakeAPI
from tests.test_reconcile import _load_skill


TZ = tz.gettz("America/Los_Angeles")
SEED_URL = "https://loft.example/calendar"
SERIES = [{"id": 11, "slug": "trivia-thursday", "title": "Trivia Thursday"},
          {"id": 12, "slug": "old-quiz", "title": "Old Quiz", "is_active": False}]


def local_day(days: int, hour: int = 19) -> datetime:
    d = (datetime.now(timezone.utc) + timedelta(days=days)).astimezone(TZ).date()
    return datetime(d.year, d.month, d.day, hour, tzinfo=TZ)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def naive(dt: datetime) -> str:
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def live(slug, title, start, series_slug=None, url=None, **kw):
    sid = {"trivia-thursday": 11, "old-quiz": 12}.get(series_slug)
    return {"id": abs(hash(slug)) % 10000, "slug": slug, "title": title, "start_at": utc(start),
            "end_at": None, "venue_id": None, "venue_slug": None, "source_url": url,
            "category_slugs": [], "tags": [], "series_slug": series_slug, "series_id": sid, **kw}


@pytest.fixture
def run(fresh_db, tmp_state_dir, monkeypatch):
    """Run `event-reconcile run --skip-enrich` with the seed page extracting
    to `extracted` and TownLoops answering from `api`."""
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    now = datetime.now(timezone.utc).isoformat()
    fresh_db.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
                     "VALUES ('Loft', ?, 'venue', 'approved', ?, ?)", (SEED_URL, now, now))
    mod = _load_skill()
    monkeypatch.setattr(mod, "open_db", lambda: fresh_db)
    monkeypatch.setattr("engine.db.DEFAULT_DB_PATH", str(tmp_state_dir / "townloops.db"))
    monkeypatch.setattr(mod, "_telegram", lambda text: None)

    def go(api, extracted):
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        monkeypatch.setattr(mod, "firecrawl_markdown", lambda url: (True, "# page", ""))
        monkeypatch.setattr(mod, "codex_exec",
                            lambda prompt, timeout_s=0: (True, json.dumps(extracted), ""))
        assert mod.main(["run", "--skip-enrich"]) == 0
        return {r["title"] + "@" + r["date"]: json.loads(r["extra_json"])["reconcile"]
                for r in fresh_db.execute("SELECT * FROM events WHERE extra_json IS NOT NULL")}

    go.conn = fresh_db
    return go


class TestRecurrenceBySeries:
    def test_new_date_in_a_series_is_a_new_occurrence(self, run):
        api = FakeAPI([live("trivia-1", "Trivia Thursday", local_day(3), "trivia-thursday",
                            url="https://loft.example/trivia")], series=SERIES)
        staged = run(api, [{"title": "Trivia Thursday", "date": naive(local_day(10)),
                            "source_url": "https://loft.example/trivia"}])
        [meta] = staged.values()
        assert meta["kind"] == "new"

    def test_archived_series_still_groups(self, run):
        api = FakeAPI([live("quiz-1", "Old Quiz", local_day(3), "old-quiz")], series=SERIES)
        staged = run(api, [{"title": "Old Quiz", "date": naive(local_day(10))}])
        [meta] = staged.values()
        assert meta["kind"] == "new"

    def test_moved_one_off_is_still_a_change(self, run):
        api = FakeAPI([live("fest-1", "Fall Harvest Festival", local_day(3, 10))], series=SERIES)
        staged = run(api, [{"title": "Fall Harvest Festival", "date": naive(local_day(10, 10))}])
        [meta] = staged.values()
        assert meta["kind"] == "changed" and meta["live_slug"] == "fest-1"


class TestSeriesMirror:
    def test_series_linked_later_reaches_the_local_row(self, run):
        api = FakeAPI([live("trivia-1", "Trivia Thursday", local_day(3))], series=SERIES)
        conn = run.conn
        from engine.event_extract import event_uid, upsert_event
        ev = {"title": "Trivia Thursday", "date": naive(local_day(3))}
        upsert_event(conn, "Loft", ev)
        uid = event_uid("Loft", ev["title"], ev["date"])
        conn.execute("UPDATE events SET townloops_slug = 'trivia-1', pushed_at = ?, "
                     "state = 'shipped' WHERE event_uid = ?", (utc(local_day(0)), uid))
        api.link("trivia-1", "trivia-thursday")   # a curator / the backfill
        run(api, [])
        row = conn.execute("SELECT series_slug, series_id FROM events WHERE event_uid = ?",
                           (uid,)).fetchone()
        assert tuple(row) == ("trivia-thursday", 11)


class TestClassifyBySeries:
    """The rule, at the classifier."""

    def test_variant_title_on_a_series_source_is_new_not_moved(self):
        now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        idx = rc.LiveIndex.build(
            [{"slug": "t1", "title": "Trivia Thursday", "start_at": "2026-10-09T02:00:00Z",
              "source_url": "https://loft.example/trivia", "series_slug": "trivia-thursday"}],
            [], TZ)
        [c] = rc.classify_all([{"title": "Thursday Quiz", "date": "2026-10-15T19:00:00",
                                "source_url": "https://loft.example/trivia"}], idx, now=now)
        assert c.kind == "new" and "Series" in c.reason
