"""The weekly newsletter collapses Series (issue #3), through `newsletter.py
compose` against the fake API: a Series appears once, as its next
occurrence in the week, with its cadence, a "+N more dates" count and a hub
link only while the Series is active and has a `url`; singletons render as
before; Events are never grouped by title. The JSON summary the Sunday cron
reads counts Series and one-off entries separately."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from tests.fake_townloops import FakeAPI


WEEK = "2026-W42"   # Mon Oct 12 – Sun Oct 18, 2026

SERIES = [
    {"id": 1, "slug": "trivia-thursday", "title": "Trivia Thursday",
     "cadence_text": "Weekly on Thursdays", "url": "/slt/series/trivia-thursday"},
    {"id": 2, "slug": "daily-live-music", "title": "Daily Live Music",
     "cadence_text": "Daily", "url": "/slt/series/daily-live-music"},
    {"id": 3, "slug": "old-market", "title": "Old Market", "is_active": False,
     "cadence_text": "Weekly on Saturdays", "url": None},
]


def ev(slug, title, start, series_slug=None, **kw):
    return {"slug": slug, "title": title, "start_at": start, "end_at": None,
            "description": f"About {slug}.", "url": f"/slt/events/{slug}",
            "venue_name": kw.pop("venue_name", "Riva Grill"), "status": "published",
            "series_slug": series_slug, "series_id": None, **kw}


@pytest.fixture
def compose(fresh_db, tmp_state_dir, monkeypatch, capsys):
    from engine import db as db_mod
    path = Path(__file__).resolve().parent.parent / "skills/business/newsletter-weekly/newsletter.py"
    spec = importlib.util.spec_from_file_location("newsletter_weekly", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    connect = db_mod.connect
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: connect(str(tmp_state_dir / "townloops.db")))
    monkeypatch.setenv("TOWNLOOPS_API_URL", "https://townloops.example/api/v1")

    def go(api):
        monkeypatch.setattr(mod.townloops_api, "Client", lambda: api)
        monkeypatch.setattr("sys.argv", ["newsletter.py", "compose", "--week", WEEK])
        capsys.readouterr()
        assert mod.main() == 0
        out = json.loads(capsys.readouterr().out)
        return out, Path(out["body"]).read_text()

    return go


def entries(html):
    body = html.split('<div class="footer">')[0]
    return body.split('<div class="event')[1:]


def test_week_collapses_each_series_to_one_entry(compose):
    api = FakeAPI([
        *[ev(f"music-{d}", "Daily Live Music", f"2026-10-{d}T18:00:00Z", "daily-live-music")
          for d in range(12, 19)],                                         # 7 dates in the week
        ev("trivia-a", "Trivia Thursday", "2026-10-16T02:00:00Z", "trivia-thursday"),
        ev("trivia-b", "Trivia Thursday", "2026-10-13T02:00:00Z", "trivia-thursday"),
        ev("fall-fest", "Fall Fest", "2026-10-17T18:00:00Z"),
        ev("book-talk", "Book Talk", "2026-10-14T01:00:00Z"),
        ev("pumpkin-run", "Pumpkin Run", "2026-10-18T16:00:00Z"),
    ], series=SERIES)
    out, html = compose(api)

    assert len(entries(html)) == 5
    assert out["events"] == 12
    assert out["series"] == 2 and out["singletons"] == 3

    music = next(e for e in entries(html) if "Daily Live Music" in e)
    assert "music-12" in music                    # represented by its next occurrence
    assert "Daily" in music and "+6 more dates this week" in music
    assert 'href="https://townloops.example/slt/series/daily-live-music"' in music

    trivia = next(e for e in entries(html) if "Trivia Thursday" in e)
    assert "trivia-b" in trivia and "trivia-a" not in trivia
    assert "Weekly on Thursdays" in trivia and "+1 more date this week" in trivia

    book = next(e for e in entries(html) if "Book Talk" in e)
    assert "more date" not in book and "/series/" not in book
    assert 'href="https://townloops.example/slt/events/book-talk"' in book


def test_series_details_are_read_once_per_slug(compose):
    api = FakeAPI([ev(f"music-{d}", "Daily Live Music", f"2026-10-{d}T18:00:00Z", "daily-live-music")
                   for d in range(13, 17)], series=SERIES)
    compose(api)
    assert [c for c in api.calls if c[1] == "series"] == [("GET", "series", "daily-live-music")]


def test_archived_series_groups_without_a_link(compose):
    api = FakeAPI([
        ev("market-1", "Old Market", "2026-10-17T16:00:00Z", "old-market"),
        ev("market-2", "Old Market", "2026-10-18T16:00:00Z", "old-market"),
    ], series=SERIES)
    _, html = compose(api)
    [entry] = entries(html)
    assert "+1 more date this week" in entry
    assert "/series/" not in entry


def test_unknown_series_still_groups_without_a_link(compose):
    api = FakeAPI([
        ev("gone-1", "Gone Night", "2026-10-15T02:00:00Z", "gone-night"),
        ev("gone-2", "Gone Night", "2026-10-16T02:00:00Z", "gone-night"),
    ], series=SERIES)
    _, html = compose(api)
    [entry] = entries(html)
    assert "+1 more date this week" in entry and "/series/" not in entry


def test_same_title_without_a_series_is_not_merged(compose):
    api = FakeAPI([
        ev("jazz-riva", "Jazz Night", "2026-10-15T02:00:00Z", venue_name="Riva Grill"),
        ev("jazz-hub", "Jazz Night", "2026-10-15T03:00:00Z", venue_name="Social House"),
    ], series=SERIES)
    out, html = compose(api)
    assert len(entries(html)) == 2
    assert out["series"] == 0 and out["singletons"] == 2
