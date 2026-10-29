"""Cancelled or postponed events their sources still list (issue #1, story 39).

An event can stay on its seed's listing — or keep its own page — while that
page says it is cancelled or postponed. The cheapest signals are the ones
already in hand: the listing event-find scraped (the extracted title or
description says so, and the statement is on the page), and the event's own
page when read-detail reads it. Only a statement that is on the page counts.

- Not yet shipped: autonomous declines it with the quote as the reason;
  supervised flags it for the operator, and a confirmed flag declines it.
- On TownLoops: it is flagged `possibly-cancelled` like a vanished event —
  the operator confirms it (supervised), or `submit_evidenced` re-reads its
  own page and submits a pending cancellation only if that page says so
  (autonomous). A flag held by the listing isn't cleared just because the
  listing still shows the event; it clears when the listing stops saying so.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from engine import event_cancel as ec
from engine import event_detail, workqueue
from engine.event_extract import event_uid, upsert_event
from tests.autonomy_town import PROFILE, SKILLS, Town, load, page_text, soon


NOW = datetime(2027, 3, 1, tzinfo=timezone.utc)
N = 3
LISTING = "https://riva.example/events"
JAZZ = {"title": "Jazz Night", "date": "2027-03-20T20:00:00", "venue": "Riva Grill",
        "description": "Live jazz with the Trio."}
OTHER = {"title": "Open Mic", "date": "2027-03-25T19:00:00", "venue": "Riva Grill"}


def listed_page(*evts) -> str:
    return "\n\n".join(f"{e['title']} — {e['date']}\n\n{e.get('description') or ''}"
                       for e in evts)


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, "
                     "last_seen_at) VALUES ('Riva Grill', ?, 'venue', 'approved', 'x', 'x')",
                     (LISTING,))
    return fresh_db


def _uid(ev, seed="Riva Grill"):
    return event_uid(seed, ev["title"], ev["date"])


def _row(conn, uid):
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def _find(conn, *events):
    """One event-find run of Riva Grill, as find.py drives event_cancel."""
    page = listed_page(*events)
    uids = []
    for ev in events:
        quote = ec.listing_statement(ev, page)
        if quote:
            ev = {**ev, "title": ec.strip_status(ev["title"])}
        upsert_event(conn, "Riva Grill", ev)
        uids.append(_uid(ev))
        ec.record_listing(conn, uids[-1], quote, "Riva Grill")
    ec.record_seed_run(conn, "Riva Grill", uids, [e["date"] for e in events], now=NOW)
    return ec.flag_vanished(conn, threshold=N, now=NOW)


def _ship(conn, uid, slug="jazz-night"):
    conn.execute("UPDATE events SET townloops_slug = ?, pushed_at = 'x', state = 'shipped', "
                 "  approval_status = 'approved' WHERE event_uid = ?", (slug, uid))


CANCELLED_JAZZ = {**JAZZ, "title": "CANCELLED: Jazz Night"}


class TestListingStatement:
    @pytest.mark.parametrize("evt", [
        {**JAZZ, "title": "CANCELLED: Jazz Night"},
        {**JAZZ, "title": "Jazz Night (Postponed)"},
        {**JAZZ, "description": "This event has been cancelled due to weather."},
    ])
    def test_an_explicit_statement_on_the_listing_is_quoted(self, evt):
        assert ec.listing_statement(evt, listed_page(evt))

    def test_a_statement_the_page_does_not_carry_is_ignored(self):
        evt = {**JAZZ, "title": "CANCELLED: Jazz Night"}
        assert ec.listing_statement(evt, listed_page(JAZZ)) is None

    @pytest.mark.parametrize("desc", [
        "Refunds are issued if an event is cancelled.",
        "Jazz Night will not be cancelled, rain or shine.",
        "Live jazz with the Trio.",
    ])
    def test_policy_wording_or_nothing_is_no_statement(self, desc):
        evt = {**JAZZ, "description": desc}
        assert ec.listing_statement(evt, listed_page(evt)) is None

    @pytest.mark.parametrize("title,clean", [
        ("CANCELLED: Jazz Night", "Jazz Night"),
        ("Postponed - Jazz Night", "Jazz Night"),
        ("Jazz Night (Cancelled)", "Jazz Night"),
        ("Jazz Night — POSTPONED", "Jazz Night"),
        ("Jazz Night", "Jazz Night"),
        ("Cancelled Plans Comedy Hour", "Cancelled Plans Comedy Hour"),
    ])
    def test_the_marker_comes_off_the_title(self, title, clean):
        assert ec.strip_status(title) == clean


class TestListedLiveEvent:
    @pytest.fixture
    def shipped(self, conn):
        _find(conn, JAZZ, OTHER)
        uid = _uid(JAZZ)
        _ship(conn, uid)
        return uid

    def test_a_live_event_listed_as_cancelled_is_flagged_on_its_own_row(self, conn, shipped):
        _find(conn, CANCELLED_JAZZ, OTHER)
        row = _row(conn, shipped)
        assert row["cancel_status"] == "possibly-cancelled"
        assert "CANCELLED: Jazz Night" in row["cancel_note"]
        assert row["title"] == "Jazz Night"            # same row, marker not stored
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2

    def test_the_flag_holds_while_the_listing_says_so(self, conn, shipped):
        for _ in range(N + 1):
            _find(conn, CANCELLED_JAZZ, OTHER)
        assert _row(conn, shipped)["cancel_status"] == "possibly-cancelled"
        assert [r["event_uid"] for r in ec.flagged(conn)] == [shipped]

    def test_the_flag_clears_once_the_listing_stops_saying_so(self, conn, shipped):
        _find(conn, CANCELLED_JAZZ, OTHER)
        _find(conn, JAZZ, OTHER)
        assert _row(conn, shipped)["cancel_status"] is None

    def test_an_operator_decision_stands(self, conn, shipped):
        _find(conn, CANCELLED_JAZZ, OTHER)
        ec.dismiss(conn, [shipped])
        _find(conn, CANCELLED_JAZZ, OTHER)
        assert _row(conn, shipped)["cancel_status"] == "dismissed"

    def test_a_flag_is_never_applied_live_without_confirmation(self, conn, shipped):
        from tests.fake_townloops import FakeAPI
        _find(conn, CANCELLED_JAZZ, OTHER)
        api = FakeAPI(events=[{"id": 7, "slug": "jazz-night", "title": "Jazz Night",
                               "start_at": "2027-03-21T03:00:00+00:00", "tags": []}])
        assert ec.apply_confirmed(conn, api)["cancelled"] == 0
        assert api.patches() == []


@pytest.fixture
def autonomous(monkeypatch):
    from engine import autonomy
    monkeypatch.setattr(autonomy, "is_autonomous", lambda: True)


@pytest.mark.usefixtures("autonomous")
class TestListedUnshippedEvent:
    def test_a_pending_event_listed_as_cancelled_is_declined(self, conn):
        _find(conn, CANCELLED_JAZZ, OTHER)
        row = _row(conn, _uid(JAZZ))
        assert (row["state"], row["approval_status"]) == ("declined", "declined")
        assert "cancelled" in row["approval_note"].lower()
        assert "CANCELLED: Jazz Night" in row["approval_note"]

    def test_an_approved_in_flight_event_is_declined_and_dequeued(self, conn):
        _find(conn, JAZZ, OTHER)
        uid = _uid(JAZZ)
        conn.execute("UPDATE events SET state = 'venue-derived', approval_status = 'approved' "
                     "WHERE event_uid = ?", (uid,))
        workqueue.enqueue(conn, uid, "extract-hosts")
        _find(conn, CANCELLED_JAZZ, OTHER)
        assert _row(conn, uid)["state"] == "declined"
        assert conn.execute("SELECT COUNT(*) FROM event_workqueue WHERE event_uid = ? "
                            "AND resolved_at IS NULL", (uid,)).fetchone()[0] == 0


class TestListedUnshippedEventSupervised:
    """Supervised: the operator declines — the agent only flags the copy for
    the operator's cancellations list, and a confirmed flag declines it."""

    def test_a_pending_event_listed_as_cancelled_is_flagged_not_declined(self, conn):
        _find(conn, CANCELLED_JAZZ, OTHER)
        row = _row(conn, _uid(JAZZ))
        assert row["state"] == "metadata-extracted"
        assert row["cancel_status"] == "possibly-cancelled"
        assert "CANCELLED: Jazz Night" in row["cancel_note"]
        assert [r["event_uid"] for r in ec.flagged(conn)] == [_uid(JAZZ)]

    def test_a_confirmed_flag_on_an_unshipped_copy_declines_it(self, conn):
        _find(conn, CANCELLED_JAZZ, OTHER)
        uid = _uid(JAZZ)
        ec.confirm(conn, [uid])
        out = ec.apply_confirmed(conn, client=None)
        row = _row(conn, uid)
        assert (row["state"], row["cancel_status"]) == ("declined", "cancelled")
        assert out["cancelled"] == 1


class TestOwnPageEvidence:
    def test_a_listing_page_must_name_the_event(self):
        from engine import autonomy
        listing = "Open Mic\n\nCANCELLED\n\nJazz Night — Friday"
        assert autonomy.cancellation_evidence(listing, "Jazz Night").verdict == "submit"
        assert autonomy.cancellation_evidence(listing, "Jazz Night",
                                              own_page=False).verdict == "hold"
        named = "CANCELLED: Jazz Night — Friday"
        assert autonomy.cancellation_evidence(named, "Jazz Night",
                                              own_page=False).verdict == "submit"


# --------------------------------------------------------------------------
# read-detail: the event's own page
# --------------------------------------------------------------------------

DETAIL_URL = "https://riva.example/events/jazz-night"


class TestDetailPage:
    def _approved(self, conn):
        upsert_event(conn, "Riva Grill", {**JAZZ, "source_url": DETAIL_URL})
        uid = _uid(JAZZ)
        conn.execute("UPDATE events SET state = 'approved', approval_status = 'approved' "
                     "WHERE event_uid = ?", (uid,))
        return _row(conn, uid)

    def _read(self, conn, event, markdown):
        return event_detail.read_detail(
            conn, event, final_attempt=False,
            fetch=lambda url, timeout_s=0: (True, markdown, ""),
            llm=lambda prompt, timeout_s=0: (True, "{}", ""))

    def test_an_own_page_that_says_cancelled_declines_the_event(self, conn, autonomous):
        event = self._approved(conn)
        out = self._read(conn, event, "# Jazz Night\n\nThis event has been cancelled. "
                                      "Refunds at the box office.")
        assert out.status == "cancelled"
        row = _row(conn, event["event_uid"])
        assert row["state"] == "declined"
        assert "This event has been cancelled" in row["approval_note"]

    def test_supervised_an_own_page_that_says_cancelled_is_flagged_and_read(self, conn):
        event = self._approved(conn)
        out = self._read(conn, event, "# Jazz Night\n\nThis event has been cancelled. "
                                      "Refunds at the box office.")
        assert out.status == "read"
        row = _row(conn, event["event_uid"])
        assert (row["state"], row["cancel_status"]) == ("approved", "possibly-cancelled")
        assert "This event has been cancelled" in row["cancel_note"]

    def test_an_own_page_that_says_nothing_lets_it_go_on(self, conn):
        event = self._approved(conn)
        out = self._read(conn, event, "# Jazz Night\n\nDoors at 7pm. Refunds if cancelled.")
        assert out.status == "read"
        assert _row(conn, event["event_uid"])["state"] == "approved"


# --------------------------------------------------------------------------
# Whole town: event-find → drain → event-cancellations
# --------------------------------------------------------------------------

ARENA = "https://arena.example/events"
JAZZ_PAGE = "https://arena.example/jazz"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def cancellations(town, *argv):
    mod = load(SKILLS / "event-cancellations" / "cancellations.py", "town_listed_cancellations")
    town.mp.setattr("engine.event_extract.firecrawl_markdown", town._fetch)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(list(argv)) == 0
    return out.getvalue()


def jazz_row(town):
    return dict(town.conn.execute("SELECT * FROM events WHERE title = 'Jazz Night'").fetchone())


class TestTown:
    def _ship_jazz(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url=JAZZ_PAGE), concert("Later", days=30)])
        town.run()
        town.api.approve("events", next(town.api.posted_slug("events", p) for p in town.submitted_events()
                                        if p["title"] == "Jazz Night"))
        town.listing(ARENA, [concert("CANCELLED: Jazz Night", source_url=JAZZ_PAGE),
                             concert("Later", days=30)])

    def test_autonomous_listed_cancellation_is_submitted_with_own_page_evidence(self, town):
        self._ship_jazz(town)
        town.pages[JAZZ_PAGE] = page_text("Jazz Night", "This event has been cancelled.")
        before = len(town.submitted_events())
        town.run()
        assert len(town.submitted_events()) == before    # no "CANCELLED: …" copy shipped
        assert jazz_row(town)["cancel_status"] == "possibly-cancelled"
        cancellations(town, "apply")
        [sent] = town.submitted_events()[before:]
        assert sent["title"] == "Cancelled: Jazz Night" and "cancelled" in sent["tags"]
        assert jazz_row(town)["cancel_status"] == "submitted"

    def test_autonomous_listing_alone_is_not_enough(self, town):
        self._ship_jazz(town)
        town.pages[JAZZ_PAGE] = page_text("Jazz Night", "Doors at 7pm.")
        town.run()
        before = len(town.submitted_events())
        cancellations(town, "apply")
        assert len(town.submitted_events()) == before
        assert jazz_row(town)["cancel_status"] == "possibly-cancelled"

    def test_an_event_listed_as_cancelled_before_it_ships_is_declined(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("POSTPONED: Jazz Night"), concert("Later", days=30)])
        town.run()
        assert town.submitted_titles() == ["Later"]
        row = jazz_row(town)
        assert row["state"] == "declined" and "POSTPONED: Jazz Night" in row["approval_note"]
        assert town.reasons()["Jazz Night"] == "listed-cancelled"

    def test_an_own_page_saying_cancelled_stops_the_ship(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url=JAZZ_PAGE), concert("Later", days=30)])
        town.pages[JAZZ_PAGE] = page_text("Jazz Night", "This event has been postponed.")
        town.run()
        assert town.submitted_titles() == ["Later"]
        assert jazz_row(town)["state"] == "declined"
        assert town.reasons()["Jazz Night"] == "page-states-cancelled"

    def test_supervised_flags_for_the_operator_and_never_touches_live(self, town):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert(source_url=JAZZ_PAGE)])
        town.find()
        town.conn.execute("UPDATE events SET townloops_slug = 'jazz-night', state = 'shipped', "
                          "approval_status = 'approved'")
        town.conn.commit()
        town.listing(ARENA, [concert("CANCELLED: Jazz Night", source_url=JAZZ_PAGE)])
        town.find()
        uid = jazz_row(town)["event_uid"]
        assert jazz_row(town)["cancel_status"] == "possibly-cancelled"
        proposal = json.loads(cancellations(town, "propose-confirm"))["proposal"]
        assert proposal["kind"] == "confirm_cancellations" and proposal["uids"] == [uid]
        cancellations(town, "apply")
        assert town.api.calls == [] or all(c[0] == "GET" for c in town.api.calls)
