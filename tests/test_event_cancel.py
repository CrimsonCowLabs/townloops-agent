"""Cancellation detection: an upcoming shipped event missing from every one of
its sources on N consecutive finds is flagged for the operator; only the
operator's confirmation marks it cancelled on TownLoops."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from engine import event_cancel as ec
from engine.event_extract import event_uid, upsert_event
from tests.fake_townloops import FakeAPI


NOW = datetime(2027, 3, 1, tzinfo=timezone.utc)
N = 3


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    for name, url in (("Riva Grill", "https://riva.example/events"),
                      ("Chamber", "https://chamber.example/events")):
        fresh_db.execute(
            "INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
            "VALUES (?, ?, 'venue', 'approved', 'x', 'x')", (name, url))
    return fresh_db


JAZZ = {"title": "Jazz Night", "date": "2027-03-20T20:00:00", "venue": "Riva Grill"}
OTHER = {"title": "Open Mic", "date": "2027-03-25T19:00:00", "venue": "Riva Grill"}


def _uid(seed, ev):
    return event_uid(seed, ev["title"], ev["date"])


def _row(conn, uid):
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def _ship(conn, uid, slug="jazz-night"):
    conn.execute("UPDATE events SET townloops_slug = ?, pushed_at = 'x', state = 'shipped', "
                 "  approval_status = 'approved' WHERE event_uid = ?", (slug, uid))



def _find(conn, seed, *events):
    """One successful event-find run of `seed` that listed `events`."""
    for ev in events:
        upsert_event(conn, seed, ev)
    ec.record_seed_run(conn, seed, [_uid(seed, e) for e in events],
                       [e["date"] for e in events], now=NOW)
    return ec.flag_vanished(conn, threshold=N, now=NOW)


@pytest.fixture
def shipped_jazz(conn):
    _find(conn, "Riva Grill", JAZZ, OTHER)
    uid = _uid("Riva Grill", JAZZ)
    _ship(conn, uid)
    return uid


class TestVanishDetection:
    def test_listed_every_run_is_never_flagged(self, conn, shipped_jazz):
        for _ in range(N + 1):
            _find(conn, "Riva Grill", JAZZ, OTHER)
        assert _row(conn, shipped_jazz)["missed_finds"] == 0
        assert _row(conn, shipped_jazz)["cancel_status"] is None

    def test_flagged_after_n_consecutive_misses_not_before(self, conn, shipped_jazz):
        for _ in range(N - 1):
            _find(conn, "Riva Grill", OTHER)
        assert _row(conn, shipped_jazz)["cancel_status"] is None
        result = _find(conn, "Riva Grill", OTHER)
        row = _row(conn, shipped_jazz)
        assert row["cancel_status"] == "possibly-cancelled"
        assert row["cancel_flagged_at"] and "Riva Grill" in row["cancel_note"]
        assert result["flagged"] == [shipped_jazz]

    def test_a_sighting_resets_the_count(self, conn, shipped_jazz):
        for _ in range(N - 1):
            _find(conn, "Riva Grill", OTHER)
        _find(conn, "Riva Grill", JAZZ, OTHER)
        for _ in range(N - 1):
            _find(conn, "Riva Grill", OTHER)
        assert _row(conn, shipped_jazz)["cancel_status"] is None

    def test_reappearing_clears_the_flag(self, conn, shipped_jazz):
        for _ in range(N):
            _find(conn, "Riva Grill", OTHER)
        result = _find(conn, "Riva Grill", JAZZ, OTHER)
        assert _row(conn, shipped_jazz)["cancel_status"] is None
        assert result["cleared"] == [shipped_jazz]

    def test_event_beyond_what_the_page_lists_is_not_a_miss(self, conn, shipped_jazz):
        early = {"title": "Coffee Social", "date": "2027-03-05T09:00:00"}
        for _ in range(N + 1):
            _find(conn, "Riva Grill", early)  # page only shows the next few days
        assert _row(conn, shipped_jazz)["missed_finds"] == 0

    def test_empty_or_failed_run_is_not_a_miss(self, conn, shipped_jazz):
        for _ in range(N + 1):
            ec.record_seed_run(conn, "Riva Grill", [], [], now=NOW)
        assert _row(conn, shipped_jazz)["missed_finds"] == 0

    def test_still_listed_by_another_source_is_not_flagged(self, conn, shipped_jazz):
        _find(conn, "Chamber", JAZZ)  # a copy, linked by dedup
        for _ in range(N + 1):
            _find(conn, "Riva Grill", OTHER)
            _find(conn, "Chamber", JAZZ)
        assert _row(conn, shipped_jazz)["cancel_status"] is None

    def test_missing_from_all_sources_is_flagged(self, conn, shipped_jazz):
        _find(conn, "Chamber", JAZZ, OTHER)
        for _ in range(N):
            _find(conn, "Riva Grill", OTHER)
            _find(conn, "Chamber", OTHER)
        assert _row(conn, shipped_jazz)["cancel_status"] == "possibly-cancelled"

    def test_unshipped_and_past_events_are_not_flagged(self, conn):
        _find(conn, "Riva Grill", JAZZ, OTHER)
        for _ in range(N + 1):
            _find(conn, "Riva Grill", OTHER)
        assert _row(conn, _uid("Riva Grill", JAZZ))["cancel_status"] is None
        later = datetime(2027, 4, 1, tzinfo=timezone.utc)
        _ship(conn, _uid("Riva Grill", JAZZ))
        assert ec.flag_vanished(conn, threshold=N, now=later)["flagged"] == []

    def test_dismissed_is_never_reflagged(self, conn, shipped_jazz):
        for _ in range(N):
            _find(conn, "Riva Grill", OTHER)
        assert ec.dismiss(conn, [shipped_jazz])["dismissed"] == 1
        for _ in range(N + 1):
            _find(conn, "Riva Grill", OTHER)
        assert _row(conn, shipped_jazz)["cancel_status"] == "dismissed"


@pytest.fixture
def flagged(conn, shipped_jazz):
    for _ in range(N):
        _find(conn, "Riva Grill", OTHER)
    return shipped_jazz


def _live(**over):
    row = {"id": 7, "slug": "jazz-night", "title": "Jazz Night", "description": "Live jazz.",
           "start_at": "2027-03-21T03:00:00+00:00", "end_at": None,
           "category_slugs": ["live-music"], "tags": ["all-ages"], "source_url": None,
           "is_featured": False, "accessibility_notes": None}
    row.update(over)
    return row


class TestOperatorConfirmation:
    def test_flagging_never_touches_live(self, conn, flagged):
        api = FakeAPI(events=[_live()])
        assert api.calls == []
        assert ec.apply_confirmed(conn, api)["cancelled"] == 0  # nothing confirmed
        assert api.patches() == []

    def test_confirmed_event_is_marked_cancelled_live(self, conn, flagged):
        api = FakeAPI(events=[_live()])
        assert ec.confirm(conn, [flagged])["confirmed"] == 1
        out = ec.apply_confirmed(conn, api)
        assert out["cancelled"] == 1
        live = api.events["jazz-night"]
        assert live["title"] == "Cancelled: Jazz Night"
        assert sorted(live["tags"]) == ["all-ages", "cancelled"]
        assert live["category_slugs"] == ["live-music"]      # full body: set kept
        assert live["description"] == "Live jazz."
        assert _row(conn, flagged)["cancel_status"] == "cancelled"

    def test_marking_is_idempotent(self, conn, flagged):
        api = FakeAPI(events=[_live(title="Cancelled: Jazz Night", tags=["cancelled"])])
        ec.confirm(conn, [flagged])
        ec.apply_confirmed(conn, api)
        assert api.events["jazz-night"]["title"] == "Cancelled: Jazz Night"

    def test_live_event_already_gone(self, conn, flagged):
        api = FakeAPI(events=[])
        ec.confirm(conn, [flagged])
        assert ec.apply_confirmed(conn, api)["cancelled"] == 1
        assert api.patches() == []
        assert "not on TownLoops" in _row(conn, flagged)["cancel_note"]

    def test_patch_that_does_not_land_is_parked(self, conn, flagged):
        api = FakeAPI(events=[_live()], ignore={"tags"})
        ec.confirm(conn, [flagged])
        out = ec.apply_confirmed(conn, api)
        assert out["failed"] == 1
        row = _row(conn, flagged)
        assert row["cancel_status"] == "apply-failed" and "tags" in row["cancel_note"]
        ec.apply_confirmed(conn, api)
        assert len(api.patches()) == 1  # not retried until the operator re-confirms

    def test_only_flagged_events_can_be_confirmed(self, conn, shipped_jazz):
        assert ec.confirm(conn, [shipped_jazz]) == {"confirmed": 0, "skipped": 1,
                                                    "skipped_uids": [shipped_jazz]}

    def test_propose_lists_flagged_events(self, conn, flagged):
        out = ec.propose(conn, "confirm")
        assert out["proposal"] == {"kind": "confirm_cancellations", "uids": [flagged], "count": 1}
        assert out["preview"][0]["title"] == "Jazz Night"
        assert ec.propose(conn, "dismiss", uids=["nomatch"])["proposal"] is None


class TestEventFindWiring:
    """event-find links copies, counts misses per successful seed run and
    flags vanished live events — and a failed run counts nothing."""

    def _find_mod(self, tmp_state_dir, monkeypatch, pages):
        import argparse
        import importlib.util
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "skills/business/event-find/find.py"
        spec = importlib.util.spec_from_file_location("event_find_cancel", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
        monkeypatch.setattr(mod, "_load_town_profile", lambda: {"cancellation_miss_threshold": 2})
        monkeypatch.setattr(mod, "_firecrawl_listing", lambda url: (url in pages, "md", "down"))
        monkeypatch.setattr(mod, "_build_extraction_prompt", lambda p, name, url, md: url)
        monkeypatch.setattr(mod, "_codex_exec", lambda url: (True, json.dumps(pages[url]), ""))
        args = argparse.Namespace(seed_id=None, limit=None, dry_run=False)
        return lambda: mod.cmd_find(args)

    def test_vanished_event_is_flagged_after_threshold(self, conn, tmp_state_dir, monkeypatch):
        conn.commit()
        pages = {"https://riva.example/events": [JAZZ, OTHER],
                 "https://chamber.example/events": [OTHER]}
        run = self._find_mod(tmp_state_dir, monkeypatch, pages)
        run()
        jazz = _uid("Riva Grill", JAZZ)
        _ship(conn, jazz)
        assert _row(conn, _uid("Chamber", OTHER))["status"] == "duplicate"

        pages["https://riva.example/events"] = [OTHER]
        run()
        assert _row(conn, jazz)["cancel_status"] is None
        run()
        assert _row(conn, jazz)["cancel_status"] == "possibly-cancelled"

    def test_failed_scrape_is_not_a_miss(self, conn, tmp_state_dir, monkeypatch):
        conn.commit()
        pages = {"https://riva.example/events": [JAZZ, OTHER]}
        run = self._find_mod(tmp_state_dir, monkeypatch, pages)
        run()
        jazz = _uid("Riva Grill", JAZZ)
        _ship(conn, jazz)
        pages.clear()  # Firecrawl down
        for _ in range(3):
            run()
        assert _row(conn, jazz)["missed_finds"] == 0


@pytest.fixture
def dash_db(conn, tmp_state_dir, monkeypatch):
    import dashboard.db as dashboard_db
    monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
    conn.commit()
    return conn


def test_confirm_widget_confirms_and_dismisses(dash_db):
    from dashboard import chat_worker, intent_handlers
    conn = dash_db
    _find(conn, "Riva Grill", JAZZ, OTHER)
    jazz, mic = _uid("Riva Grill", JAZZ), _uid("Riva Grill", OTHER)
    _ship(conn, jazz)
    _ship(conn, mic, slug="open-mic")
    for _ in range(N):
        _find(conn, "Riva Grill", {"title": "Coffee", "date": "2027-03-30T09:00:00"})
    for kind in ("confirm_cancellations", "dismiss_cancellations"):
        assert intent_handlers.validate_proposal({"kind": kind, "uids": [jazz]}) == (True, "")
        assert kind in chat_worker._ALLOWED_PROPOSAL_KINDS

    assert intent_handlers.execute({"kind": "confirm_cancellations", "uids": [jazz]})["confirmed"] == 1
    assert intent_handlers.execute({"kind": "dismiss_cancellations", "uids": [mic]})["dismissed"] == 1
    assert _row(conn, jazz)["cancel_status"] == "confirmed"
    assert _row(conn, mic)["cancel_status"] == "dismissed"


def test_copy_retired_onto_the_live_event_is_flagged_once(conn, shipped_jazz):
    _find(conn, "Chamber", JAZZ, OTHER)
    copy = _uid("Chamber", JAZZ)
    # event-ship retired the copy onto the live event: same slug, status duplicate
    conn.execute("UPDATE events SET townloops_slug = 'jazz-night', status = 'duplicate' "
                 "WHERE event_uid = ?", (copy,))
    for _ in range(N):
        _find(conn, "Riva Grill", OTHER)
        _find(conn, "Chamber", OTHER)
    assert [r["event_uid"] for r in ec.flagged(conn)] == [shipped_jazz]


def test_reappearing_clears_an_apply_failed_flag(conn, flagged):
    api = FakeAPI(events=[_live()], ignore={"tags"})
    ec.confirm(conn, [flagged])
    ec.apply_confirmed(conn, api)
    assert _row(conn, flagged)["cancel_status"] == "apply-failed"
    _find(conn, "Riva Grill", JAZZ, OTHER)
    assert _row(conn, flagged)["cancel_status"] is None


def test_cancelled_tag_fits_under_the_tag_cap():
    live = _live(tags=[f"t{i}" for i in range(10)])
    tags = ec.cancelled_overrides(live)["tags"]
    assert len(tags) == 10 and tags[-1] == "cancelled" and tags[:9] == live["tags"][:9]
