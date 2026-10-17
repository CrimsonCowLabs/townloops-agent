"""engine/event_provenance.py — an event's source_url is the page its fields
came from (#45).

Generic list-page extraction can cross-wire the cards of a multi-event page:
SLT shipped "Line Dancing" at Cascade Kitchens with a half marathon's detail
URL, date and time. Two guards: at extraction each event is read against its
own card on the listing (its times, its link), and before ship the detail
page must name the event and its date. An event neither its listing card nor
its detail page vouches for is held for review with what disagreed — pending
for the operator in both modes, never shipped, never dropped.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from engine import autonomy, event_detail, event_provenance, event_state, workqueue
from engine.event_extract import event_uid, upsert_event


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "provenance"
SEED = "Tahoe Chamber"
SEED_URL = "https://business.tahoechamber.org/events/"
MARATHON_URL = ("https://business.tahoechamber.org/events/details/"
                "lakeside-marathon-nevada-half-marathon-10-09-2026-17355")
DANCING_URL = "https://business.tahoechamber.org/events/details/line-dancing-10-07-2026-17301"
ART_WALK_URL = "https://business.tahoechamber.org/events/details/fall-art-walk-10-10-2026-17360"
HARVEST_URL = "https://business.tahoechamber.org/events/details/harvest-festival-09-26-2026-17200"

MARATHON = {"title": "Lakeside Marathon - Nevada Half Marathon", "date": "2026-10-09T06:30:00",
            "end_at": "2026-10-09T13:00:00", "venue": "Zephyr Cove Resort",
            "source_url": MARATHON_URL}
DANCING = {"title": "Line Dancing", "date": "2026-10-07T19:30:00", "end_at": "2026-10-07T21:30:00",
           "venue": "Cascade Kitchens", "source_url": DANCING_URL}
ART_WALK = {"title": "Fall Art Walk", "date": "2026-10-10T11:00:00", "source_url": ART_WALK_URL}
MIXER = {"title": "Chamber Mixer", "date": "2026-10-13T17:30:00", "source_url": None}
# What produced line-dancing-2026-10-08: the dance class's title and venue,
# the marathon's URL, date and time.
CROSS_WIRED = {"title": "Line Dancing", "date": "2026-10-09T06:30:00",
               "end_at": "2026-10-09T13:00:00", "venue": "Cascade Kitchens",
               "source_url": MARATHON_URL}
CHECK = event_provenance.LISTING_CHECK


def page(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text()


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds(name, url, kind, status, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'aggregator', 'approved', 'now', 'now')", (SEED, SEED_URL))
    return fresh_db


def autonomous(tmp_state_dir):
    (tmp_state_dir.parent / "town-profile.yaml").write_text(
        "name: Test Town\noperating_mode: autonomous\n")


def row(conn, uid) -> dict:
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def provenance(conn, uid) -> dict:
    return json.loads(row(conn, uid)["extra_json"] or "{}").get("provenance") or {}


def listing(events, name="chamber_listing"):
    """The one extraction entry point, on an LLM answer of `events`."""
    return event_provenance.listing_events(json.dumps(events), page(name), SEED_URL)


# --------------------------------------------------------------------------
# At extraction: each event is read against its own listing card
# --------------------------------------------------------------------------

class TestCardUrls:
    def check(self, *events, name="chamber_listing"):
        return listing([dict(e) for e in events], name)

    def test_each_card_keeps_its_own_link(self):
        out = self.check(MARATHON, DANCING, ART_WALK, MIXER)
        assert [e["source_url"] for e in out] == [MARATHON_URL, DANCING_URL, ART_WALK_URL, None]
        assert not any(e.get(CHECK) for e in out)

    def test_a_neighbouring_cards_link_is_refused_and_named(self):
        out = self.check(MARATHON, CROSS_WIRED, ART_WALK, MIXER)
        assert out[1]["source_url"] is None
        check = out[1][CHECK]
        assert (check["problem"], check["url"], check["belongs_to"]) == (
            "refused", MARATHON_URL, "Lakeside Marathon - Nevada Half Marathon")
        assert out[0]["source_url"] == MARATHON_URL

    def test_no_event_holds_a_neighbouring_cards_detail_url(self):
        # Every way the extractor could hand one card's link to another.
        cards = [MARATHON, DANCING, ART_WALK, MIXER]
        urls = [MARATHON_URL, DANCING_URL, ART_WALK_URL]
        for i, card in enumerate(cards):
            for url in urls:
                out = self.check(*[{**c, "source_url": url} if j == i else c
                                   for j, c in enumerate(cards)])
                own = card["source_url"]
                assert out[i]["source_url"] in (own, None), (card["title"], url)

    def test_a_link_after_the_cards_description_is_the_cards(self):
        # Fall Art Walk's only link is "More Info" under a long description,
        # right above the next card.
        assert self.check(ART_WALK, MIXER)[0]["source_url"] == ART_WALK_URL

    def test_a_relative_link_matches_its_absolute_form(self):
        assert self.check({**ART_WALK, "source_url": ART_WALK_URL + "/"})[0]["source_url"]

    def test_a_url_not_on_the_listing_is_dropped(self):
        out = self.check({**DANCING, "source_url": "https://elsewhere.example/line-dancing"})
        assert out[0]["source_url"] is None
        assert out[0][CHECK]["problem"] == "not-on-page"

    def test_the_listing_itself_is_left_alone(self):
        out = self.check({**MIXER, "source_url": SEED_URL})
        assert out[0]["source_url"] == SEED_URL and not out[0].get(CHECK)

    def test_occurrences_of_one_title_share_its_link(self):
        later = {**DANCING, "date": "2026-10-14T19:30:00"}
        out = self.check(DANCING, later, MARATHON)
        assert [e["source_url"] for e in out[:2]] == [DANCING_URL, DANCING_URL]


class TestUnextractedNeighbour:
    """A card the extractor skipped (a past event) between two it kept."""

    def test_a_heading_ends_the_card(self):
        out = listing([{**DANCING, "source_url": HARVEST_URL}, ART_WALK], "chamber_listing_past")
        assert out[0]["source_url"] is None
        assert out[0][CHECK]["problem"] == "not-in-card"
        assert out[1]["source_url"] == ART_WALK_URL

    def test_a_link_further_down_the_card_is_not_its_own(self):
        # No headings: the skipped card's link sits inside Line Dancing's card,
        # after Line Dancing's own link.
        out = listing([{**DANCING, "source_url": HARVEST_URL}, ART_WALK], "bold_listing")
        assert out[0]["source_url"] is None
        assert out[0][CHECK]["problem"] == "not-in-card"
        assert listing([DANCING, ART_WALK], "bold_listing")[0]["source_url"] == DANCING_URL


class TestCardTimes:
    def test_a_neighbouring_cards_time_is_not_the_events(self):
        # 6:30 AM is on the listing — in the marathon's card, not Line Dancing's.
        [out] = listing([{**DANCING, "date": "2026-10-07T06:30:00", "end_at": None}])
        assert out["date"] == "2026-10-07"

    def test_its_own_cards_time_stands(self):
        [out] = listing([DANCING])
        assert (out["date"], out["end_at"]) == (DANCING["date"], DANCING["end_at"])


class TestEstablished:
    """When the listing card itself vouches for an event, and when nothing does."""

    def test_a_card_without_a_link_is_the_events_provenance(self):
        [out] = listing([MIXER])
        assert not out.get(CHECK)

    def test_a_dropped_link_with_title_and_date_on_its_card_is_established(self):
        [out] = listing([{**DANCING, "source_url": MARATHON_URL}])
        assert out[CHECK]["established"] is True

    def test_a_dropped_link_and_a_date_its_card_does_not_give_is_not(self):
        out = listing([MARATHON, CROSS_WIRED])[1]
        assert out[CHECK]["established"] is False
        assert out["date"] == "2026-10-09"   # the marathon's time is not on its card
        assert "does not give 2026-10-09" in out[CHECK]["reason"]

    def test_a_title_not_on_the_listing_is_not(self):
        [out] = listing([{"title": "Moonlight Kayak Tour", "date": "2026-10-12T19:00:00",
                          "source_url": None}])
        assert out[CHECK]["problem"] == "unplaced"
        assert out[CHECK]["established"] is False


# --------------------------------------------------------------------------
# event-find: re-ingesting the Chamber listing
# --------------------------------------------------------------------------

def _find(tmp_state_dir, monkeypatch, answer: list[dict], name="chamber_listing"):
    path = ROOT / "skills/business/event-find/find.py"
    spec = importlib.util.spec_from_file_location("event_find_provenance", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr(mod, "_load_town_profile", lambda: {})
    monkeypatch.setattr(mod, "_firecrawl_listing", lambda url: (True, page(name), ""))
    monkeypatch.setattr(mod, "_build_extraction_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(mod, "_codex_exec", lambda p: (True, json.dumps(answer), ""))
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")
    run = lambda: mod.cmd_find(argparse.Namespace(seed_id=None, limit=None, dry_run=False))  # noqa: E731
    run.mod = mod
    return run


class TestEventFind:
    def test_reingesting_the_chamber_listing_does_not_reproduce_the_cross_wire(
            self, conn, tmp_state_dir, monkeypatch):
        run = _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED, ART_WALK, MIXER])
        assert run() == 0
        uid = event_uid(SEED, "Line Dancing", "2026-10-09")
        r = row(conn, uid)
        assert r["source_url"] is None
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert provenance(conn, uid)["status"] == event_provenance.HELD_FOR_REVIEW
        assert "Lakeside Marathon - Nevada Half Marathon" in r["approval_note"]
        marathon = event_uid(SEED, MARATHON["title"], MARATHON["date"])
        assert row(conn, marathon)["source_url"] == MARATHON_URL
        held = conn.execute("SELECT COUNT(*) FROM events WHERE source_url = ?",
                            (MARATHON_URL,)).fetchone()[0]
        assert held == 1

    def test_a_link_its_card_vouches_without_is_dropped_not_held(
            self, conn, tmp_state_dir, monkeypatch):
        _find(tmp_state_dir, monkeypatch, [MARATHON, {**DANCING, "source_url": MARATHON_URL}])()
        uid = event_uid(SEED, "Line Dancing", DANCING["date"])
        assert provenance(conn, uid)["status"] == event_provenance.URL_DROPPED
        assert row(conn, uid)["source_url"] is None
        assert not row(conn, uid)["approval_note"]

    def test_a_clean_run_clears_an_earlier_listing_hold(self, conn, tmp_state_dir, monkeypatch):
        late = {**DANCING, "date": "2026-10-09T19:30:00", "end_at": None,
                "source_url": MARATHON_URL}
        _find(tmp_state_dir, monkeypatch, [MARATHON, late])()
        uid = event_uid(SEED, "Line Dancing", late["date"])
        assert provenance(conn, uid)["status"] == event_provenance.HELD_FOR_REVIEW
        _find(tmp_state_dir, monkeypatch, [MARATHON, {**late, "source_url": None}])()
        assert provenance(conn, uid) == {}
        assert not row(conn, uid)["approval_note"]

    def test_the_autonomous_judge_leaves_a_held_event_to_the_operator(
            self, conn, tmp_state_dir, monkeypatch):
        run = _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED])
        run()
        uid = event_uid(SEED, "Line Dancing", "2026-10-09")
        pending = [r["event_uid"] for r in run.mod._pending_rows(conn, "1 = 1", ())]
        assert uid not in pending
        assert event_uid(SEED, MARATHON["title"], MARATHON["date"]) in pending

    def test_a_shipped_row_whose_link_is_refused_is_surfaced_not_changed(
            self, conn, tmp_state_dir, monkeypatch):
        from engine import orchestrator
        upsert_event(conn, SEED, CROSS_WIRED)   # shipped before the guard
        uid = event_uid(SEED, "Line Dancing", CROSS_WIRED["date"])
        conn.execute("UPDATE events SET state = 'shipped', approval_status = 'approved', "
                     "  date_utc = '2099-10-09T13:30:00+00:00' WHERE event_uid = ?", (uid,))
        _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED])()
        r = row(conn, uid)
        assert (r["state"], r["source_url"]) == ("shipped", MARATHON_URL)
        assert provenance(conn, uid)["status"] == event_provenance.LIVE_MISMATCH
        monkeypatch.setattr(orchestrator, "_telegram_notify", lambda t: None)
        out = orchestrator.summary(conn)
        text = Path(out["report_path"]).read_text()
        assert "## Live — source mismatch" in text
        assert out["provenance_live"][0]["event_uid"] == uid


class TestSeedTrial:
    def test_the_trial_reads_events_through_the_same_card_checks(self, conn, monkeypatch):
        from engine import seeds
        seen = []
        monkeypatch.setattr(seeds, "_judge_listing",
                            lambda conn, profile, mode, tools, events, md, url: seen.extend(events))
        tools = seeds.Tools(fetch=lambda u: (True, page("chamber_listing"), ""),
                            scrape=None, search=None,
                            llm=lambda p, **k: (True, json.dumps([MARATHON, CROSS_WIRED]), ""))
        seeds.trial(conn, {}, autonomy.settings({}), tools, SEED, SEED_URL)
        assert seen[1]["source_url"] is None
        assert seen[1]["date"] == "2026-10-09"


# --------------------------------------------------------------------------
# Reopening for review is one event_state transition
# --------------------------------------------------------------------------

class TestReopenForReview:
    def test_back_to_pending_its_approval_and_queue_rows_gone(self, conn):
        ev = approved(conn, DANCING)
        conn.execute("UPDATE event_workqueue SET attempts = 2")
        assert event_state.reopen_for_review(conn, ev["event_uid"], ("approved",), "why")
        r = row(conn, ev["event_uid"])
        assert (r["state"], r["approval_status"], r["approval_at"], r["approval_note"]) == (
            "metadata-extracted", "pending", None, "why")
        assert conn.execute("SELECT COUNT(*) FROM event_workqueue WHERE event_uid = ? AND "
                            "(resolved_at IS NULL OR attempts > 0)",
                            (ev["event_uid"],)).fetchone()[0] == 0

    def test_an_event_that_moved_on_is_left_alone(self, conn):
        ev = approved(conn, DANCING)
        conn.execute("UPDATE events SET state = 'detail-read' WHERE event_uid = ?",
                     (ev["event_uid"],))
        assert not event_state.reopen_for_review(conn, ev["event_uid"], ("approved",))
        assert row(conn, ev["event_uid"])["state"] == "detail-read"


# --------------------------------------------------------------------------
# Before ship: read-detail checks the detail page agrees with the event
# --------------------------------------------------------------------------

class Stub:
    def __init__(self, markdown=""):
        self.markdown = markdown
        self.fetched: list[str] = []
        self.prompts: list[str] = []

    def fetch(self, url, timeout_s=0):
        self.fetched.append(url)
        return (True, self.markdown, "")

    def llm(self, prompt, timeout_s=0):
        self.prompts.append(prompt)
        return (True, json.dumps({"accessibility_quotes": []}), "")


def approved(conn, evt: dict) -> dict:
    upsert_event(conn, SEED, evt)
    uid = event_uid(SEED, evt["title"], evt["date"])
    assert event_state.approve(conn, uid)
    return row(conn, uid)


def read(conn, ev, stub):
    return event_detail.read_detail(conn, ev, final_attempt=False, fetch=stub.fetch, llm=stub.llm)


class TestDetailAgreement:
    def test_a_detail_page_naming_the_event_and_its_date_is_read(self, conn):
        ev = approved(conn, DANCING)
        assert read(conn, ev, Stub(page("line_dancing_detail"))).status == "read"
        assert row(conn, ev["event_uid"])["state"] == "approved"

    def test_another_events_page_holds_the_event_naming_what_disagreed(self, conn):
        ev = approved(conn, {**DANCING, "date": "2026-10-08T19:30:00", "source_url": MARATHON_URL})
        stub = Stub(page("marathon_detail"))
        result = read(conn, ev, stub)
        assert result.status == "held"
        assert stub.prompts == []          # no Codex call on a page that isn't the event's
        r = row(conn, ev["event_uid"])
        # Back in the operator's approval queue, with the reason.
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        note = r["approval_note"]
        assert "Line Dancing" in note and "Lakeside Marathon - Nevada Half Marathon" in note
        assert "2026-10-08" in note and "Oct 9" in note
        assert MARATHON_URL in note
        held = provenance(conn, ev["event_uid"])
        assert held["status"] == event_provenance.HELD_FOR_REVIEW and len(held["disagreed"]) == 2
        assert not conn.execute("SELECT COUNT(*) FROM event_workqueue WHERE event_uid = ? "
                                "AND resolved_at IS NULL", (ev["event_uid"],)).fetchone()[0]

    def test_title_alone_disagreeing_holds(self, conn):
        ev = approved(conn, {**CROSS_WIRED})
        result = read(conn, ev, Stub(page("marathon_detail")))
        assert result.status == "held"
        assert provenance(conn, ev["event_uid"])["disagreed"] == [
            'detail page does not name "Line Dancing" '
            '(it reads "Lakeside Marathon - Nevada Half Marathon")']

    def test_date_alone_disagreeing_holds(self, conn):
        ev = approved(conn, {**DANCING, "date": "2026-10-21T19:30:00"})
        assert read(conn, ev, Stub(page("line_dancing_detail"))).status == "held"
        assert provenance(conn, ev["event_uid"])["disagreed"][0].startswith(
            "detail page does not give 2026-10-21")

    def test_a_sidebar_naming_the_event_is_not_the_page_naming_it(self, conn):
        ev = approved(conn, {**CROSS_WIRED, "date": "2026-10-07T19:30:00"})
        assert read(conn, ev, Stub(page("marathon_sidebar_detail"))).status == "held"
        assert provenance(conn, ev["event_uid"])["disagreed"][0].startswith(
            'detail page does not name "Line Dancing"')

    def test_a_sidebar_date_is_not_the_events_date(self, conn):
        ev = approved(conn, {**DANCING, "date": "2026-10-21T19:30:00"})
        assert read(conn, ev, Stub(page("line_dancing_sidebar_detail"))).status == "held"
        assert "it gives Oct 7)" in provenance(conn, ev["event_uid"])["disagreed"][0]

    def test_a_title_on_a_line_of_its_own_names_the_event(self, conn):
        # The site's name is the only heading; the event's title is bold.
        ev = approved(conn, DANCING)
        md = ("# Tahoe Chamber\n\n**Line Dancing**\n\nWednesday Oct 7, 2026 7:30 PM\n\n"
              "## Upcoming Events\n\n- Trivia Night — Oct 21\n")
        assert read(conn, ev, Stub(md)).status == "read"

    def test_a_posted_date_alone_does_not_hold(self, conn):
        ev = approved(conn, {**DANCING, "date": "2026-10-14T19:30:00"})
        assert read(conn, ev, Stub(page("posted_detail"))).status == "read"

    def test_a_series_page_shared_by_occurrences_is_not_a_date_mismatch(self, conn):
        approved(conn, DANCING)
        later = approved(conn, {**DANCING, "date": "2026-10-14T19:30:00"})
        assert read(conn, later, Stub(page("line_dancing_detail"))).status == "read"

    def test_operator_reapproval_after_a_hold_is_accepted(self, conn):
        ev = approved(conn, {**CROSS_WIRED})
        read(conn, ev, Stub(page("marathon_detail")))
        assert event_state.approve(conn, ev["event_uid"], "checked: it is line dancing")
        stub = Stub(page("marathon_detail"))
        result = read(conn, row(conn, ev["event_uid"]), stub)
        assert result.status == "read"
        assert provenance(conn, ev["event_uid"])["status"] == event_provenance.ACCEPTED
        assert stub.prompts   # read for its fields, no longer judged

    def test_a_hold_that_lost_its_race_is_not_an_acceptance(self, conn):
        ev = approved(conn, {**CROSS_WIRED})
        uid = ev["event_uid"]
        # Another worker moved the event between the read and the hold.
        conn.execute("UPDATE events SET state = 'declined' WHERE event_uid = ?", (uid,))
        read(conn, ev, Stub(page("marathon_detail")))
        assert row(conn, uid)["state"] == "declined"
        assert provenance(conn, uid).get("status") != event_provenance.HELD_FOR_REVIEW
        # Back to approved without a new approval: the next read judges again.
        conn.execute("UPDATE events SET state = 'approved' WHERE event_uid = ?", (uid,))
        assert read(conn, row(conn, uid), Stub(page("marathon_detail"))).status == "held"

    def test_a_stale_hold_mark_on_an_approved_event_is_not_an_acceptance(self, conn):
        # The mark a pre-fix lost race left: held, but approved before it.
        ev = approved(conn, {**CROSS_WIRED})
        uid = ev["event_uid"]
        later = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
        conn.execute("UPDATE events SET extra_json = ? WHERE event_uid = ?", (json.dumps(
            {"provenance": {"status": event_provenance.HELD_FOR_REVIEW, "held_at": later,
                            "disagreed": ["x"]}}), uid))
        stub = Stub(page("marathon_detail"))
        assert read(conn, row(conn, uid), stub).status == "held"
        assert row(conn, uid)["state"] == "metadata-extracted"

    def test_a_blocked_detail_page_is_not_judged(self, conn):
        ev = approved(conn, DANCING)
        stub = Stub("Attention Required! | Cloudflare\n\nPlease enable cookies.")
        assert read(conn, ev, stub).status != "held"


class TestListingHold:
    """An event held at extraction reaches read-detail only through the operator."""

    def _held(self, conn, tmp_state_dir, monkeypatch):
        _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED])()
        return event_uid(SEED, "Line Dancing", "2026-10-09")

    def test_operator_approval_accepts_it(self, conn, tmp_state_dir, monkeypatch):
        uid = self._held(conn, tmp_state_dir, monkeypatch)
        assert event_state.approve(conn, uid)
        result = read(conn, row(conn, uid), Stub(page("marathon_detail")))
        assert result.status == "no-detail-page"   # its link was refused; the listing it is
        assert provenance(conn, uid)["status"] == event_provenance.ACCEPTED

    def test_operator_decline_ends_it(self, conn, tmp_state_dir, monkeypatch):
        uid = self._held(conn, tmp_state_dir, monkeypatch)
        assert event_state.decline(conn, uid, "not real")
        assert event_provenance.held_for_review(conn) == []


class TestAutonomous:
    def test_held_not_declined_with_a_recorded_decision(self, conn, tmp_state_dir):
        autonomous(tmp_state_dir)
        ev = approved(conn, {**CROSS_WIRED})
        assert read(conn, ev, Stub(page("marathon_detail"))).status == "held"
        r = row(conn, ev["event_uid"])
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        assert "Lakeside Marathon" in r["approval_note"]
        d = conn.execute("SELECT action, detail FROM agent_activity WHERE worker = 'autonomy' "
                         "AND target = ?", (ev["event_uid"],)).fetchone()
        assert d["action"] == "event:hold"
        detail = json.loads(d["detail"])
        assert detail["reason"] == "provenance-mismatch"
        assert detail["evidence"]["source_url"] == MARATHON_URL
        assert detail["evidence"]["disagreed"]

    def test_the_verdict_is_autonomys(self):
        evidence = {"disagreed": ["detail page does not name it"], "source_url": MARATHON_URL}
        assert autonomy.judge_provenance(evidence, autonomy.Settings(mode="autonomous")).verdict \
            == "hold"
        assert autonomy.judge_provenance(evidence, autonomy.Settings()).verdict == autonomy.AWAIT
        assert autonomy.judge_provenance({"disagreed": []}, autonomy.Settings()).verdict == "ship"

    def test_the_operator_resolves_it_from_the_autonomy_skill(self, conn, tmp_state_dir,
                                                               monkeypatch):
        autonomous(tmp_state_dir)
        spec = importlib.util.spec_from_file_location(
            "autonomy_skill_prov", ROOT / "skills/business/autonomy/autonomy.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "open_db", lambda: conn)
        accept = approved(conn, {**CROSS_WIRED})
        decline = approved(conn, {**CROSS_WIRED, "date": "2026-10-10T06:30:00"})
        read(conn, accept, Stub(page("marathon_detail")))
        read(conn, decline, Stub(page("marathon_detail")))
        assert mod.main(["accept", accept["event_uid"]]) == 0
        assert mod.main(["decline", decline["event_uid"]]) == 0
        assert row(conn, accept["event_uid"])["state"] == "approved"
        assert row(conn, decline["event_uid"])["state"] == "declined"
        # Only a held event: anything else is the agent's call.
        assert mod.main(["accept", accept["event_uid"]]) == 1


class TestSkill:
    def test_a_held_event_does_not_advance(self, conn, monkeypatch):
        """The orchestrator only advances from the state its action serves."""
        ev = approved(conn, {**CROSS_WIRED})
        spec = importlib.util.spec_from_file_location(
            "event_read_detail_prov", ROOT / "skills/business/event-read-detail/read.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        stub = Stub(page("marathon_detail"))
        monkeypatch.setattr(mod, "open_db", lambda: conn)
        monkeypatch.setattr(event_detail, "firecrawl_markdown", stub.fetch)
        monkeypatch.setattr(event_detail, "codex_exec", stub.llm)
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: ev["event_uid"])
        assert mod.main() == 0
        state = row(conn, ev["event_uid"])["state"]
        assert event_state.next_action_for(state) != "read-detail"


class TestDigest:
    def test_held_events_are_listed_every_night_until_resolved(self, conn, tmp_state_dir,
                                                               monkeypatch):
        from engine import orchestrator
        ev = approved(conn, {**CROSS_WIRED})
        read(conn, ev, Stub(page("marathon_detail")))
        # Held days ago: still listed.
        conn.execute("UPDATE events SET state_updated_at = '2026-01-01T00:00:00+00:00'")
        monkeypatch.setattr(orchestrator, "_telegram_notify", lambda t: None)
        out = orchestrator.summary(conn)
        text = Path(out["report_path"]).read_text()
        assert "## Held — source mismatch" in text
        assert "Line Dancing" in text and "Lakeside Marathon - Nevada Half Marathon" in text
        assert out["provenance_held"][0]["event_uid"] == ev["event_uid"]
        assert event_state.decline(conn, ev["event_uid"], "not real")
        assert orchestrator.summary(conn)["provenance_held"] == []
