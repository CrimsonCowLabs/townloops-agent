"""event-ship carries an event's detail-page accessibility notes to
TownLoops: in the create body, and into a live row only where it has none —
a live value is never overwritten or blanked, and what was sent is GET-verified."""

from __future__ import annotations

from tests.fake_townloops import FakeAPI
from tests.test_live_lookup import (  # noqa: F401 — fixtures
    _row, _stage, _use, conn, live_event, ship, snapshot,
)


NOTES = "The lounge is wheelchair accessible. ASL interpretation is available on request."


def _with_notes(conn, uid, notes=NOTES):
    conn.execute("UPDATE events SET accessibility_notes = ? WHERE event_uid = ?", (notes, uid))


class TestCreate:
    def test_notes_are_in_the_create_body(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _with_notes(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        [created] = api.creates()
        assert created["accessibility_notes"] == NOTES
        assert api.events[api.created_slug()]["accessibility_notes"] == NOTES

    def test_no_notes_sends_no_key(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        assert "accessibility_notes" not in api.creates()[0]

    def test_notes_a_create_drops_are_patched_in_and_verified(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(create_drops={"accessibility_notes"}))
        uid = _stage(ship, monkeypatch, conn)
        _with_notes(conn, uid)
        assert ship.main() == 0
        [patch] = api.patches()
        assert patch["accessibility_notes"] == NOTES
        assert api.events[_row(conn, uid)["townloops_slug"]]["accessibility_notes"] == NOTES

    def test_notes_that_never_land_flag_the_event(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI(create_drops={"accessibility_notes"},
                                        ignore={"accessibility_notes"}))
        uid = _stage(ship, monkeypatch, conn)
        _with_notes(conn, uid)
        assert ship.main() != 0
        assert _row(conn, uid)["state"].startswith("stuck-")
        assert _row(conn, uid)["townloops_slug"]   # kept, so nothing re-creates it
        assert len(api.creates()) == 1


class TestAlreadyLive:
    def test_blank_live_notes_are_filled(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", accessibility_notes=None)]))
        _with_notes(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        assert api.patches()[0]["accessibility_notes"] == NOTES
        assert api.events["jazz-live"]["accessibility_notes"] == NOTES

    def test_a_live_value_is_not_overwritten(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", accessibility_notes="Ramp at side door.")]))
        _with_notes(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        assert api.events["jazz-live"]["accessibility_notes"] == "Ramp at side door."
        assert all(p.get("accessibility_notes") in (None, "Ramp at side door.") for p in api.patches())

    def test_no_local_notes_never_blank_a_live_value(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description=None,
                                                    accessibility_notes="Ramp at side door.")]))
        _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        assert api.patches(), "description fill still PATCHes"
        assert api.patches()[0]["accessibility_notes"] == "Ramp at side door."
        assert api.events["jazz-live"]["accessibility_notes"] == "Ramp at side door."

    def test_a_list_row_without_the_field_is_read_in_full_first(self, ship, monkeypatch, conn):
        """A source_url match comes from the list resource; if that omits
        accessibility_notes, "blank" is decided on the GET row."""
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", accessibility_notes="Ramp at side door.")]))
        real_list = api.list

        def list_without_notes(resource, **params):
            return [{k: v for k, v in r.items() if k != "accessibility_notes"}
                    for r in real_list(resource, **params)]
        api.list = list_without_notes
        _with_notes(conn, _stage(ship, monkeypatch, conn))
        assert ship.main() == 0
        assert api.events["jazz-live"]["accessibility_notes"] == "Ramp at side door."
