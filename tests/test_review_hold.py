"""engine/review_hold.py — one hold-for-review lifecycle, several reasons.

An event can be held for the operator for a source mismatch (#45,
engine/event_provenance) and for an implausible time (#46,
engine/time_plausibility) — sometimes both. Each kind keeps its own rules
and evidence; the lifecycle (the hold, the approval note, the acceptance,
the listings, bulk approval) is one.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from engine import event_provenance, event_state, review_hold, time_plausibility as tp
from engine.event_extract import event_uid, upsert_event


ROOT = Path(__file__).resolve().parent.parent
SEED = "AleWorX"
TAHOE_100 = {"title": "Tahoe 100", "date": "2099-07-18T05:30:00", "venue": "Kings Beach"}
JAZZ = {"title": "Jazz Night", "date": "2099-07-03T19:00:00", "end_at": "2099-07-03T22:00:00",
        "venue": "Riva Grill"}
CARD = {"title": "Line Dancing", "date": "2099-07-04T19:00:00", "venue": "Cascade Kitchens"}
P, T = review_hold.PROVENANCE, review_hold.TIMES


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


def row(conn, uid) -> dict:
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def add(conn, evt) -> str:
    upsert_event(conn, SEED, evt)
    return event_uid(SEED, evt["title"], evt["date"])


def provenance_hold(conn, uid, why="listing does not name it"):
    assert event_provenance.hold_for_review(conn, row(conn, uid), [why], None, stage="listing")


# --------------------------------------------------------------------------
# The approval note: one part per kind, none clobbers another
# --------------------------------------------------------------------------

class TestNote:
    def test_each_kind_has_its_own_part(self):
        n = review_hold.with_note(None, T, "starts at 05:30")
        n = review_hold.with_note(n, P, "detail page does not name it")
        assert n == "times: starts at 05:30 | provenance: detail page does not name it"
        assert review_hold.without_note(n, T) == "provenance: detail page does not name it"
        assert review_hold.without_note(n, P) == "times: starts at 05:30"

    def test_a_kinds_part_is_replaced_not_repeated(self):
        n = review_hold.with_note("[reconcile] live event x changed: date", T, "a")
        n = review_hold.with_note(n, T, "b")
        assert n == "[reconcile] live event x changed: date | times: b"
        assert review_hold.without_note(n, T) == "[reconcile] live event x changed: date"

    def test_a_part_quoting_the_separator_stays_whole(self):
        n = review_hold.with_note(None, P, 'it reads "Marathon | Half"')
        n = review_hold.with_note(n, T, "ends before it starts")
        assert review_hold.without_note(n, T) == 'provenance: it reads "Marathon | Half"'

    def test_a_new_base_keeps_the_holds_parts(self):
        n = review_hold.with_note("[reconcile] new — not on TownLoops", T, "starts at 05:30")
        n = review_hold.with_note(n, P, "listing does not name it")
        assert review_hold.with_base(n, "[reconcile] live event x changed: date") == (
            "[reconcile] live event x changed: date | times: starts at 05:30"
            " | provenance: listing does not name it")
        assert review_hold.with_base(None, "[reconcile] new") == "[reconcile] new"

    def test_nothing_left_is_no_note(self):
        assert review_hold.without_note("times: a", T) is None
        assert review_hold.without_note(None, T) is None


class TestTwoKinds:
    def test_a_provenance_hold_keeps_the_times_note(self, conn):
        uid = add(conn, TAHOE_100)
        tp.record(conn, uid)
        provenance_hold(conn, uid)
        note = row(conn, uid)["approval_note"]
        assert tp.NOTE_PREFIX in note and event_provenance.NOTE_PREFIX in note

    def test_a_provenance_release_keeps_the_times_note(self, conn):
        uid = add(conn, TAHOE_100)
        tp.record(conn, uid)
        provenance_hold(conn, uid)
        event_provenance.record_extraction(conn, uid, {**TAHOE_100})   # the listing vouches now
        note = row(conn, uid)["approval_note"]
        assert note.startswith(tp.NOTE_PREFIX) and event_provenance.NOTE_PREFIX not in note

    def test_a_times_release_keeps_the_provenance_note(self, conn):
        uid = add(conn, {**JAZZ, "end_at": "2099-07-03T18:00:00"})
        tp.record(conn, uid)
        provenance_hold(conn, uid)
        upsert_event(conn, SEED, JAZZ)          # the source corrects its end
        tp.record(conn, uid)
        assert row(conn, uid)["approval_note"].startswith(event_provenance.NOTE_PREFIX)

    def test_held_lists_an_event_once_with_every_reason(self, conn):
        both = add(conn, TAHOE_100)
        tp.record(conn, both)
        provenance_hold(conn, both)
        only_times = add(conn, {**TAHOE_100, "title": "Tahoe 50"})
        tp.record(conn, only_times)
        held = {h["event_uid"]: h["kinds"] for h in review_hold.held(conn)}
        assert held == {both: [P, T], only_times: [T]}

    def test_the_autonomous_judge_skips_either_kind(self, conn):
        uid = add(conn, TAHOE_100)
        tp.record(conn, uid)
        other = add(conn, CARD)
        provenance_hold(conn, other)
        plain = add(conn, JAZZ)
        rows = conn.execute(f"SELECT event_uid FROM events WHERE {review_hold.NOT_HELD_SQL}")
        assert [r[0] for r in rows] == [plain]


class TestListing:
    """The nightly digest lists holds that can still happen, for either kind."""

    def test_a_past_provenance_hold_is_not_listed_upcoming(self, conn):
        uid = add(conn, {**CARD, "date": "2020-07-04T19:00:00"})
        conn.execute("UPDATE events SET date_utc = '2020-07-05T02:00:00+00:00'")
        provenance_hold(conn, uid)
        assert event_provenance.held_for_review(conn) == []
        assert [h["event_uid"] for h in event_provenance.held_for_review(conn, upcoming=False)] \
            == [uid]

    def test_only_pending_holds_are_listed(self, conn):
        uid = add(conn, CARD)
        provenance_hold(conn, uid)
        conn.execute("UPDATE events SET approval_status = 'approved' WHERE event_uid = ?", (uid,))
        assert event_provenance.held_for_review(conn, upcoming=False) == []


# --------------------------------------------------------------------------
# Bulk approval never accepts a hold: each needs its own approval
# --------------------------------------------------------------------------

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def three(conn):
    held_times = add(conn, TAHOE_100)
    tp.record(conn, held_times)
    held_source = add(conn, CARD)
    provenance_hold(conn, held_source)
    plain = add(conn, JAZZ)
    return held_times, held_source, plain


class TestBulkApprove:
    def test_the_proposal_leaves_out_held_events_and_says_why(self, conn, three, tmp_state_dir,
                                                               monkeypatch):
        held_times, held_source, plain = three
        monkeypatch.setattr("engine.db.DEFAULT_DB_PATH", str(tmp_state_dir / "townloops.db"))
        mod = _load(ROOT / "skills/business/operator-bulk-approve/approve.py", "bulk_approve_t")
        out = io.StringIO()
        with redirect_stdout(out):
            assert mod.main(["--source", "aleworx"]) == 0
        doc = json.loads(out.getvalue())
        assert doc["proposal"]["uids"] == [plain] and doc["proposal"]["count"] == 1
        skipped = doc["skipped_held"]
        assert skipped["count"] == 2
        assert {s["event_uid"]: s["held_for"] for s in skipped["events"]} == {
            held_times: ["implausible time"], held_source: ["source mismatch"]}
        assert "one at a time" in skipped["why"]

    def test_the_confirm_widget_skips_held_events_too(self, conn, three, tmp_state_dir,
                                                       monkeypatch):
        held_times, held_source, plain = three
        import dashboard.db as dashboard_db
        monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
        from dashboard import intent_handlers
        out = intent_handlers.execute({"kind": "approve_events",
                                       "uids": [held_times, held_source, plain]})
        assert out["approved"] == 1
        assert sorted(out["held_uids"]) == sorted([held_times, held_source])
        assert row(conn, plain)["approval_status"] == "approved"
        assert row(conn, held_times)["approval_status"] == "pending"
        assert row(conn, held_source)["approval_status"] == "pending"
