"""engine/event_quality.py — what a useful event has.

SLT's moderation queue filled with events that said nothing: Tahoe Blue
Event Center hockey games with no description and no category, Visit Lake
Tahoe rows with no category. Their seeds had moved to deterministic adapters,
which skip the listing LLM that used to classify events. Now the detail read
fills those blanks from the event's own page (tests/test_event_detail.py),
a thin event that is still blank is held for the operator in autonomous mode
instead of submitted, and the daily report measures every seed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from engine import alerts, autonomy, event_quality as eq, review_hold, town_report
from engine.event_extract import event_uid, upsert_event
from tests.autonomy_town import PROFILE, Town, soon


ROOT = Path(__file__).resolve().parent.parent
ARENA = "https://arena.example/events"
PAGE = "https://arena.example/events/hockey"
ABOUT = ("The Knight Monsters take on the Wichita Thunder in a regular-season "
         "ECHL hockey game at the arena.")


def snapshot(state, slugs=("music", "sports")):
    (state / "categories.json").write_text(json.dumps([{"slug": s, "name": s.title()}
                                                       for s in slugs]))


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------

class TestRules:
    def test_the_listings_description_is_the_one_sent(self):
        assert eq.description({"description": ABOUT, "detail_description": "Other text " * 9}) \
            == ABOUT

    def test_a_blank_or_too_short_listing_description_gives_way_to_the_page(self):
        assert eq.description({"description": "", "detail_description": ABOUT}) == ABOUT
        assert eq.description({"description": "Hockey.", "detail_description": ABOUT}) == ABOUT
        assert eq.description({"description": "Hockey.", "detail_description": None}) == "Hockey."
        assert eq.description({"description": None}) is None

    def test_an_event_ship_ticket_line_is_no_description(self):
        assert eq.description({"description": "RSVP: https://x.example/r"}) is None

    def test_no_description_and_no_category_are_both_named(self, tmp_state_dir):
        snapshot(tmp_state_dir)
        assert eq.missing({"description": "", "category_slugs": None}) \
            == [eq.NO_DESCRIPTION, eq.NO_CATEGORY]
        assert eq.missing({"description": ABOUT, "category_slugs": '["sports"]'}) == []

    def test_a_category_outside_the_towns_vocabulary_is_none(self, tmp_state_dir):
        snapshot(tmp_state_dir)
        assert eq.missing({"description": ABOUT, "category_slugs": '["opera"]'}) \
            == [eq.NO_CATEGORY]

    def test_without_a_vocabulary_categories_are_not_judged(self, tmp_state_dir):
        assert eq.missing({"description": ABOUT, "category_slugs": None}) == []


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

@pytest.fixture
def profile(monkeypatch):
    doc = {**PROFILE, "autonomy": {}}
    monkeypatch.setattr(autonomy, "load_town_profile", lambda: doc)
    return doc


@pytest.fixture
def conn(fresh_db, tmp_state_dir, profile):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    snapshot(tmp_state_dir)
    return fresh_db


def staged(conn, *, description="", categories=None, state="entities-enriched", **cols):
    ev = {"title": "Knight Monsters vs. Wichita Thunder", "date": soon(10),
          "description": description, "categories": categories or [], "source_url": PAGE}
    upsert_event(conn, "Arena", ev)
    uid = event_uid("Arena", ev["title"], ev["date"])
    conn.execute("UPDATE events SET state = ?, approval_status = 'approved', "
                 "approval_at = '2026-01-01T00:00:00+00:00' WHERE event_uid = ?", (state, uid))
    for k, v in cols.items():
        conn.execute(f"UPDATE events SET {k} = ? WHERE event_uid = ?", (v, uid))
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def reload(conn, ev):
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?",
                             (ev["event_uid"],)).fetchone())


class TestGate:
    def test_a_thin_event_is_held_for_the_operator(self, conn):
        ev = staged(conn)
        g = eq.gate(conn, ev)
        assert g.held and g.reasons == [eq.REASONS[eq.NO_DESCRIPTION], eq.REASONS[eq.NO_CATEGORY]]
        r = reload(conn, ev)
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert r["approval_note"].startswith("thin: ")
        mark = review_hold.mark_of(r["extra_json"], eq.KIND)
        assert mark["status"] == eq.HELD_FOR_REVIEW and mark["seed"] == "Arena"
        assert [h["event_uid"] for h in eq.held_for_review(conn)] == [ev["event_uid"]]

    def test_a_complete_event_goes_on(self, conn):
        assert not eq.gate(conn, staged(conn, description=ABOUT, categories=["sports"]))

    def test_the_pages_description_counts(self, conn):
        assert not eq.gate(conn, staged(conn, categories=["sports"], detail_description=ABOUT))

    def test_an_approval_after_the_hold_ships_it_as_it_is(self, conn):
        ev = staged(conn)
        eq.gate(conn, ev)
        conn.execute("UPDATE events SET approval_status = 'approved', approval_at = ? "
                     "WHERE event_uid = ?", ("2999-01-01T00:00:00+00:00", ev["event_uid"]))
        assert not eq.gate(conn, reload(conn, ev))
        assert review_hold.mark_of(reload(conn, ev)["extra_json"], eq.KIND)["status"] \
            == eq.ACCEPTED

    def test_supervised_mode_is_never_held_here(self, conn, profile):
        profile["operating_mode"] = "supervised"
        assert not eq.gate(conn, staged(conn))

    def test_it_can_be_turned_off(self, conn, profile):
        profile["autonomy"]["hold_thin_events"] = False
        assert not eq.gate(conn, staged(conn))

    def test_a_live_event_is_never_held(self, conn):
        assert not eq.gate(conn, staged(conn, townloops_slug="hockey-1"))
        assert not eq.gate(conn, staged(conn), live=True)

    def test_a_re_read_that_fills_it_releases_the_hold(self, conn):
        ev = staged(conn)
        eq.gate(conn, ev)
        conn.execute("UPDATE events SET description = ?, category_slugs = '[\"sports\"]' "
                     "WHERE event_uid = ?", (ABOUT, ev["event_uid"]))
        eq.record(conn, ev["event_uid"])
        r = reload(conn, ev)
        assert review_hold.mark_of(r["extra_json"], eq.KIND) == {}
        assert not (r["approval_note"] or "").startswith("thin: ")

    def test_the_autonomous_judge_skips_a_thin_hold(self):
        assert "completeness" in review_hold.NOT_HELD_SQL
        d = autonomy.judge_thin({"rules": [eq.NO_CATEGORY]}, autonomy.Settings(mode="autonomous"))
        assert (d.verdict, d.reason) == ("hold", "thin-event")


# --------------------------------------------------------------------------
# The measure
# --------------------------------------------------------------------------

def _read_events(conn, seed, n, **cols):
    for i in range(n):
        upsert_event(conn, seed, {"title": f"{seed} game {i}", "date": soon(5 + i),
                                  "description": cols.get("description", ""),
                                  "categories": cols.get("categories", [])})
    conn.execute("UPDATE events SET detail_read_status = 'read' WHERE source = ?",
                 (f"seed:{seed}",))


class TestBySeed:
    def test_a_seed_whose_events_lack_a_rule_field_is_thin(self, conn):
        _read_events(conn, "Tahoe Blue", 6)
        _read_events(conn, "Visit Tahoe", 6, description=ABOUT, categories=["music"])
        rows = {s["seed"]: s for s in eq.by_seed(conn)}
        assert rows["Tahoe Blue"]["thin"] == ["description", "category"]
        assert rows["Tahoe Blue"]["rates"]["description"] == 0
        assert rows["Visit Tahoe"]["thin"] == []
        assert rows["Visit Tahoe"]["rates"]["start time"] == 1
        assert [s["seed"] for s in eq.thin_seeds(conn)] == ["Tahoe Blue"]

    def test_a_seed_with_few_events_is_not_judged(self, conn):
        _read_events(conn, "Tiny", eq.SEED_MIN_EVENTS - 1)
        assert eq.thin_seeds(conn) == []

    def test_events_not_read_yet_are_not_counted(self, conn):
        upsert_event(conn, "Unread", {"title": "A", "date": soon(3)})
        assert eq.by_seed(conn) == []

    def test_a_thin_seed_is_a_standing_alert_and_in_the_daily_report(self, conn):
        _read_events(conn, "Tahoe Blue", 6)
        [a] = [a for a in alerts.standing(conn) if a.kind == "data-quality"]
        assert "Tahoe Blue" in a.text and "description 0%" in a.text and not a.needs_you
        md = town_report.build(conn, "2000-01-01T00:00:00+00:00").markdown
        assert "## Event data by seed" in md
        assert "| Tahoe Blue | 6 | **0%** | **0%** |" in md


# --------------------------------------------------------------------------
# A whole town: filled from the page, else held, then accepted
# --------------------------------------------------------------------------

@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    t = Town(tmp_state_dir, monkeypatch, fresh_db)
    t.set_profile({**PROFILE, "autonomy": {}}, hold_thin_events=True)
    return t


def game(title="Knight Monsters vs. Wichita Thunder", source_url=PAGE):
    # What an adapter row looks like: a title, a time, nothing else.
    return {"title": title, "date": soon(10), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "",
            "categories": [], "tags": [], "source_url": source_url}


def autonomy_skill(conn, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "autonomy_skill_quality", ROOT / "skills/business/autonomy/autonomy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    return mod


class TestTown:
    def test_the_detail_read_fills_a_bare_adapter_row_and_it_is_submitted(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [game()])
        town.pages[PAGE] = ("# Knight Monsters vs. Wichita Thunder\n\n" + ABOUT
                            + "\n\nAdd to Google Calendar. Buy tickets at the box office.\n")
        town.details[PAGE] = {"description_quotes": [ABOUT, "Add to Google Calendar."],
                              "categories": ["community", "opera"], "tags": ["Hockey"]}
        town.run()
        [sent] = town.submitted_events()
        assert sent["description"] == ABOUT
        assert sent["category_slugs"] == ["community"]
        assert sent["tags"] == ["hockey"]
        r = dict(town.conn.execute("SELECT * FROM events").fetchone())
        assert r["description"] == ""                  # the listing's text is the listing's
        assert r["detail_description"] == ABOUT

    def test_a_page_that_says_nothing_holds_it_until_the_operator_accepts(
            self, town, monkeypatch):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [game()])
        town.run()
        assert town.submitted_events() == []
        assert town.reasons()["Knight Monsters vs. Wichita Thunder"] == "thin-event"

        _, report, telegram = town.digest()
        assert "## Held — thin event" in report and "- Arena — 1 event(s)" in report
        assert "held as thin: 1" in telegram

        [held] = eq.held_for_review(town.conn)
        mod = autonomy_skill(town.conn, monkeypatch)
        assert mod.main(["accept", held["event_uid"][:10]]) == 0
        town.drain_all()
        assert town.submitted_titles() == ["Knight Monsters vs. Wichita Thunder"]
        assert eq.held_for_review(town.conn) == []

    def test_no_page_of_its_own_is_classified_from_its_text(self, town):
        town.seed("Arena", ARENA)
        ev = {**game(source_url=None), "description": ABOUT}
        town.listing(ARENA, [ev])
        base = town._codex

        def codex(prompt, timeout_s=0):
            if prompt.startswith("Classify this local event"):
                return True, json.dumps({"categories": ["community"], "tags": ["hockey"]}), ""
            return base(prompt, timeout_s)
        town.mp.setattr("engine.event_detail.codex_exec", codex)
        town.run()
        [sent] = town.submitted_events()
        assert sent["category_slugs"] == ["community"] and sent["tags"] == ["hockey"]
