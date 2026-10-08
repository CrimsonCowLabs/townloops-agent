"""Cross-source dedup: every new event gets a canonical_event_uid, copies of
one event found via several seeds share it, and only one of them ships."""

from __future__ import annotations

import sqlite3

import pytest

from engine import event_dedup as dd
from engine.event_extract import event_uid, upsert_event


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


def _seed(conn, name, url, kind="venue", role=None):
    conn.execute(
        "INSERT INTO event_seeds (name, url, kind, status, source_role, first_seen_at, last_seen_at) "
        "VALUES (?, ?, ?, 'approved', ?, '2026-01-01', '2026-01-01')",
        (name, url, kind, role),
    )


def _add(conn, seed, title, date, venue=None):
    upsert_event(conn, seed, {"title": title, "date": date, "venue": venue})
    return event_uid(seed, title, date)


def _row(conn, uid):
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def _active(*rows):
    return [r for r in rows if r["status"] == "active"]


@pytest.fixture
def seeds(conn):
    _seed(conn, "Riva Grill", "https://rivagrill.example/events")
    _seed(conn, "Tahoe Daily", "https://tahoedaily.example/calendar", kind="newspaper")
    _seed(conn, "Chamber", "https://chamber.example/events")


class TestCanonicalUid:
    def test_every_new_event_gets_one(self, conn, seeds):
        uid = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, uid)["canonical_event_uid"]

    def test_same_event_from_two_seeds_shares_it(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night!", "2026-10-09T20:00:00", "Riva Grill, SLT")
        assert _row(conn, a)["canonical_event_uid"] == _row(conn, b)["canonical_event_uid"]

    def test_missing_time_or_venue_on_one_copy_still_matches(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09")
        assert _row(conn, a)["canonical_event_uid"] == _row(conn, b)["canonical_event_uid"]

    def test_different_venues_are_different_events(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Trivia Night", "2026-10-09T19:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Trivia Night", "2026-10-09T19:00:00", "Gunbarrel Tavern")
        assert _row(conn, a)["canonical_event_uid"] != _row(conn, b)["canonical_event_uid"]
        assert len(_active(_row(conn, a), _row(conn, b))) == 2

    def test_different_showtimes_are_different_events(self, conn, seeds):
        a = _add(conn, "Riva Grill", "The Nutcracker", "2026-12-12T14:00:00", "Valhalla")
        b = _add(conn, "Chamber", "The Nutcracker", "2026-12-12T19:00:00", "Valhalla")
        assert _row(conn, a)["canonical_event_uid"] != _row(conn, b)["canonical_event_uid"]

    def test_different_days_are_different_events(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Riva Grill", "Jazz Night", "2026-10-16T20:00:00", "Riva Grill")
        assert _row(conn, a)["canonical_event_uid"] != _row(conn, b)["canonical_event_uid"]


class TestOnlyOneCopyShips:
    def test_second_copy_is_a_declined_duplicate(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        ra, rb = _row(conn, a), _row(conn, b)
        assert ra["status"] == "active" and ra["approval_status"] == "pending"
        assert rb["status"] == "duplicate"
        assert (rb["approval_status"], rb["state"]) == ("declined", "declined")
        assert a[:12] in rb["approval_note"]

    def test_primary_source_beats_an_earlier_syndicator_copy(self, conn, seeds):
        s = _add(conn, "Tahoe Daily", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        p = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, p)["status"] == "active"
        assert _row(conn, p)["approval_status"] == "pending"
        assert _row(conn, s)["status"] == "duplicate"

    def test_syndicator_copy_after_the_primary_is_the_duplicate(self, conn, seeds):
        p = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        s = _add(conn, "Tahoe Daily", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, p)["status"] == "active"
        assert _row(conn, s)["status"] == "duplicate"

    def test_operator_decided_copy_is_never_displaced(self, conn, seeds):
        s = _add(conn, "Tahoe Daily", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        conn.execute("UPDATE events SET approval_status = 'approved', state = 'approved' "
                     "WHERE event_uid = ?", (s,))
        p = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, s)["state"] == "approved"
        assert _row(conn, p)["status"] == "duplicate"

    def test_copy_of_a_declined_event_is_not_reproposed(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        conn.execute("UPDATE events SET approval_status = 'declined', state = 'declined' "
                     "WHERE event_uid = ?", (a,))
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, b)["approval_status"] == "declined"

    def test_copy_of_a_live_mirror_is_a_duplicate(self, conn, seeds):
        conn.execute(
            "INSERT INTO events (event_uid, source, source_kind, name, title, date, date_day, "
            "  first_seen_at, last_seen_at, last_modified_at, content_hash, state, approval_status, "
            "  townloops_slug) "
            "VALUES ('live-1', 'live', 'live', 'Jazz Night', 'Jazz Night', '2026-10-09T20:00:00', "
            "  '2026-10-09', 'x', 'x', 'x', 'h', 'shipped', 'approved', 'jazz-night')")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, b)["status"] == "duplicate"
        assert _row(conn, "live-1")["canonical_event_uid"] == _row(conn, b)["canonical_event_uid"]

    def test_reseeing_an_existing_event_changes_nothing(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        before = _row(conn, a)["canonical_event_uid"]
        _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        assert _row(conn, a)["canonical_event_uid"] == before
        assert _row(conn, a)["status"] == "active"


class TestSeedRole:
    @pytest.mark.parametrize("seed, role", [
        ({"url": "https://rivagrill.example/events", "kind": "venue"}, "primary"),
        ({"url": "https://tahoedaily.example/cal", "kind": "newspaper"}, "syndicator"),
        ({"url": "https://visit.example/events", "kind": "tourism"}, "syndicator"),
        ({"url": "https://allevents.in/south-lake-tahoe", "kind": "other"}, "syndicator"),
        ({"url": "https://www.eventbrite.com/d/ca--tahoe/events/", "kind": "other"}, "syndicator"),
        ({"url": "https://tahoedaily.example/cal", "kind": "newspaper",
          "source_role": "primary"}, "primary"),
        ({"url": "https://rivagrill.example/events", "kind": "venue",
          "source_role": "syndicator"}, "syndicator"),
    ])
    def test_role(self, seed, role):
        assert dd.seed_role(seed) == role


class TestBackfill:
    def test_links_rows_written_before_dedup(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        conn.execute("UPDATE events SET canonical_event_uid = NULL, status = 'active', "
                     "  approval_status = 'pending', state = 'metadata-extracted'")
        assert dd.backfill(conn) == 2
        ra, rb = _row(conn, a), _row(conn, b)
        assert ra["canonical_event_uid"] == rb["canonical_event_uid"] is not None
        assert {ra["status"], rb["status"]} == {"active", "duplicate"}


class TestShippedSibling:
    def test_finds_the_shipped_copy(self, conn, seeds):
        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        conn.execute("UPDATE events SET townloops_slug = 'jazz-night', pushed_at = 'x' "
                     "WHERE event_uid = ?", (a,))
        assert dd.shipped_sibling(conn, _row(conn, b))["townloops_slug"] == "jazz-night"
        assert dd.shipped_sibling(conn, _row(conn, a)) is None


class TestShipGuard:
    """Belt and braces: a copy that reaches event-ship while another copy is
    already live (both approved before dedup existed) is never created."""

    def test_copy_of_a_shipped_event_is_not_posted(self, conn, seeds, monkeypatch):
        import importlib.util
        from pathlib import Path
        from tests.fake_townloops import FakeAPI

        a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
        conn.execute("UPDATE events SET townloops_slug = 'jazz-night', pushed_at = 'x', "
                     "  state = 'shipped' WHERE event_uid = ?", (a,))
        conn.execute("UPDATE events SET status = 'active', approval_status = 'approved', "
                     "  state = 'entities-enriched' WHERE event_uid = ?", (b,))

        path = Path(__file__).resolve().parent.parent / "skills/business/event-ship/ship.py"
        spec = importlib.util.spec_from_file_location("event_ship_dedup", path)
        ship = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ship)
        api = FakeAPI()
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda: api)
        monkeypatch.setattr(ship, "open_db", lambda: conn)
        monkeypatch.setattr(ship, "parse_event_uid_arg", lambda: b)

        assert ship.main() == 0
        assert api.creates() == [] and api.patches() == []
        rb = _row(conn, b)
        assert (rb["status"], rb["townloops_slug"]) == ("duplicate", "jazz-night")


def test_operator_sets_a_seed_role(conn, seeds, tmp_state_dir, monkeypatch, capsys):
    import importlib.util
    import json
    from pathlib import Path
    conn.commit()
    path = Path(__file__).resolve().parent.parent / "skills/business/event-seed-discovery/discover.py"
    spec = importlib.util.spec_from_file_location("seed_discovery_role", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    seed_id = conn.execute("SELECT seed_id FROM event_seeds WHERE name = 'Tahoe Daily'").fetchone()[0]

    assert mod.main(["set-role", "--seed-id", str(seed_id), "--role", "primary"]) == 0
    capsys.readouterr()
    mod.main(["show"])
    shown = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(r for r in shown if r["seed_id"] == seed_id)["role"] == "primary"
    assert mod.main(["set-role", "--seed-id", "999", "--role", "primary"]) == 1


def test_same_seed_relisting_at_a_new_time_is_not_a_duplicate(conn, seeds):
    # A time change makes a new uid; it goes to review, not into dedup.
    a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
    conn.execute("UPDATE events SET townloops_slug = 'jazz-night', state = 'shipped', "
                 "  approval_status = 'approved' WHERE event_uid = ?", (a,))
    b = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:30:00", "Riva Grill")
    assert _row(conn, b)["status"] == "active"
    assert _row(conn, b)["approval_status"] == "pending"


def test_ship_guard_leaves_a_reconcile_change_alone(conn, seeds):
    import json
    a = _add(conn, "Riva Grill", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
    b = _add(conn, "Chamber", "Jazz Night", "2026-10-09T20:00:00", "Riva Grill")
    conn.execute("UPDATE events SET townloops_slug = 'jazz-night', status = 'duplicate' "
                 "WHERE event_uid = ?", (b,))  # retired copy, same slug
    conn.execute("UPDATE events SET townloops_slug = 'jazz-night', extra_json = ? "
                 "WHERE event_uid = ?",
                 (json.dumps({"reconcile": {"kind": "changed", "live_slug": "jazz-night"}}), a))
    # a retired copy is never "the" live sibling
    assert dd.shipped_sibling(conn, _row(conn, a)) is None
