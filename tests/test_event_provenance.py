"""engine/event_provenance.py — an event's source_url is the page its fields
came from (#45).

Generic list-page extraction can cross-wire the cards of a multi-event page:
SLT shipped "Line Dancing" at Cascade Kitchens with a half marathon's detail
URL, date and time. Two guards: at extraction a source_url is kept only when
it is a link in that event's own card on the listing, and before ship the
detail page must name the event and its date. An event either guard can't
vouch for is held for review with what disagreed, never shipped.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine import event_detail, event_provenance
from engine.event_extract import event_uid, upsert_event


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "provenance"
SEED = "Tahoe Chamber"
SEED_URL = "https://business.tahoechamber.org/events/"
MARATHON_URL = ("https://business.tahoechamber.org/events/details/"
                "lakeside-marathon-nevada-half-marathon-10-09-2026-17355")
DANCING_URL = "https://business.tahoechamber.org/events/details/line-dancing-10-07-2026-17301"
ART_WALK_URL = "https://business.tahoechamber.org/events/details/fall-art-walk-10-10-2026-17360"

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


# --------------------------------------------------------------------------
# At extraction: a source_url is a link in the event's own card
# --------------------------------------------------------------------------

class TestCardUrls:
    def check(self, *events):
        return event_provenance.check_card_urls([dict(e) for e in events],
                                                page("chamber_listing"), SEED_URL)

    def test_each_card_keeps_its_own_link(self):
        out = self.check(MARATHON, DANCING, ART_WALK, MIXER)
        assert [e["source_url"] for e in out] == [MARATHON_URL, DANCING_URL, ART_WALK_URL, None]
        assert not any(e.get(event_provenance.REFUSED) for e in out)

    def test_a_neighbouring_cards_link_is_refused_and_named(self):
        out = self.check(MARATHON, CROSS_WIRED, ART_WALK, MIXER)
        assert out[1]["source_url"] is None
        assert out[1][event_provenance.REFUSED] == {
            "url": MARATHON_URL, "belongs_to": "Lakeside Marathon - Nevada Half Marathon"}
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

    def test_a_url_not_on_the_listing_is_dropped_not_refused(self):
        out = self.check({**DANCING, "source_url": "https://elsewhere.example/line-dancing"})
        assert out[0]["source_url"] is None
        assert not out[0].get(event_provenance.REFUSED)

    def test_the_listing_itself_is_left_alone(self):
        assert self.check({**MIXER, "source_url": SEED_URL})[0]["source_url"] == SEED_URL

    def test_occurrences_of_one_title_share_its_link(self):
        later = {**DANCING, "date": "2026-10-14T19:30:00"}
        out = self.check(DANCING, later, MARATHON)
        assert [e["source_url"] for e in out[:2]] == [DANCING_URL, DANCING_URL]


# --------------------------------------------------------------------------
# event-find: re-ingesting the Chamber listing
# --------------------------------------------------------------------------

def _find(tmp_state_dir, monkeypatch, answer: list[dict]):
    path = ROOT / "skills/business/event-find/find.py"
    spec = importlib.util.spec_from_file_location("event_find_provenance", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr(mod, "_load_town_profile", lambda: {})
    monkeypatch.setattr(mod, "_firecrawl_listing", lambda url: (True, page("chamber_listing"), ""))
    monkeypatch.setattr(mod, "_build_extraction_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(mod, "_codex_exec", lambda p: (True, json.dumps(answer), ""))
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")
    return lambda: mod.cmd_find(argparse.Namespace(seed_id=None, limit=None, dry_run=False))


class TestEventFind:
    def test_reingesting_the_chamber_listing_does_not_reproduce_the_cross_wire(
            self, conn, tmp_state_dir, monkeypatch):
        run = _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED, ART_WALK, MIXER])
        assert run() == 0
        uid = event_uid(SEED, "Line Dancing", CROSS_WIRED["date"])
        assert row(conn, uid)["source_url"] is None
        assert provenance(conn, uid)["status"] == "refused-url"
        assert "Lakeside Marathon - Nevada Half Marathon" in row(conn, uid)["approval_note"]
        marathon = event_uid(SEED, MARATHON["title"], MARATHON["date"])
        assert row(conn, marathon)["source_url"] == MARATHON_URL
        held = conn.execute("SELECT COUNT(*) FROM events WHERE source_url = ?",
                            (MARATHON_URL,)).fetchone()[0]
        assert held == 1

    def test_an_existing_row_loses_the_neighbours_url(self, conn, tmp_state_dir, monkeypatch):
        upsert_event(conn, SEED, CROSS_WIRED)   # written before the guard
        uid = event_uid(SEED, "Line Dancing", CROSS_WIRED["date"])
        assert row(conn, uid)["source_url"] == MARATHON_URL
        _find(tmp_state_dir, monkeypatch, [MARATHON, CROSS_WIRED])()
        assert row(conn, uid)["source_url"] is None
        assert provenance(conn, uid)["status"] == "refused-url"

    def test_a_clean_run_clears_an_earlier_refusal(self, conn, tmp_state_dir, monkeypatch):
        _find(tmp_state_dir, monkeypatch, [MARATHON, {**DANCING, "source_url": MARATHON_URL}])()
        uid = event_uid(SEED, "Line Dancing", DANCING["date"])
        assert provenance(conn, uid)["status"] == "refused-url"
        _find(tmp_state_dir, monkeypatch, [MARATHON, DANCING])()
        assert provenance(conn, uid) == {}
        assert not row(conn, uid)["approval_note"]


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
    from engine import event_state
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
        # Supervised: back in the operator's approval queue, with the reason.
        assert (r["state"], r["approval_status"]) == ("metadata-extracted", "pending")
        note = r["approval_note"]
        assert "Line Dancing" in note and "Lakeside Marathon - Nevada Half Marathon" in note
        assert "2026-10-08" in note and "Oct 9" in note
        assert MARATHON_URL in note
        held = provenance(conn, ev["event_uid"])
        assert held["status"] == "held" and len(held["disagreed"]) == 2

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

    def test_a_series_page_shared_by_occurrences_is_not_a_date_mismatch(self, conn):
        approved(conn, DANCING)
        later = approved(conn, {**DANCING, "date": "2026-10-14T19:30:00"})
        assert read(conn, later, Stub(page("line_dancing_detail"))).status == "read"

    def test_a_refused_listing_link_is_held_without_a_fetch(self, conn):
        upsert_event(conn, SEED, {**CROSS_WIRED, "source_url": None})
        uid = event_uid(SEED, "Line Dancing", CROSS_WIRED["date"])
        event_provenance.record_extraction(
            conn, uid, {event_provenance.REFUSED: {
                "url": MARATHON_URL, "belongs_to": "Lakeside Marathon - Nevada Half Marathon"}})
        from engine import event_state
        assert event_state.approve(conn, uid)
        stub = Stub(page("marathon_detail"))
        assert read(conn, row(conn, uid), stub).status == "held"
        assert stub.fetched == []
        assert "card of \"Lakeside Marathon - Nevada Half Marathon\"" in row(conn, uid)["approval_note"]

    def test_operator_reapproval_after_a_hold_is_accepted(self, conn):
        from engine import event_state
        ev = approved(conn, {**CROSS_WIRED})
        read(conn, ev, Stub(page("marathon_detail")))
        assert event_state.approve(conn, ev["event_uid"], "checked: it is line dancing")
        result = read(conn, row(conn, ev["event_uid"]), Stub(page("marathon_detail")))
        assert result.status == "read"
        assert provenance(conn, ev["event_uid"])["status"] == "accepted"

    def test_a_blocked_detail_page_is_not_judged(self, conn):
        ev = approved(conn, DANCING)
        stub = Stub("Attention Required! | Cloudflare\n\nPlease enable cookies.")
        assert read(conn, ev, stub).status != "held"

    def test_autonomous_declines_with_a_recorded_decision(self, conn, tmp_state_dir):
        autonomous(tmp_state_dir)
        ev = approved(conn, {**CROSS_WIRED})
        assert read(conn, ev, Stub(page("marathon_detail"))).status == "held"
        r = row(conn, ev["event_uid"])
        assert r["state"] == "declined"
        assert "Lakeside Marathon" in r["approval_note"]
        d = conn.execute("SELECT action, detail FROM agent_activity WHERE worker = 'autonomy' "
                         "AND target = ?", (ev["event_uid"],)).fetchone()
        assert d["action"] == "event:drop"
        detail = json.loads(d["detail"])
        assert detail["reason"] == "provenance-mismatch"
        assert detail["evidence"]["source_url"] == MARATHON_URL
        assert detail["evidence"]["disagreed"]


class TestSkill:
    def test_a_held_event_does_not_advance(self, conn, monkeypatch):
        """The orchestrator only advances from the state its action serves."""
        from engine import event_state, workqueue
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
        assert workqueue.counts_by_action(conn)  # the claim is the orchestrator's to complete


class TestDigest:
    def test_held_events_are_listed_with_what_disagreed(self, conn, tmp_state_dir, monkeypatch):
        from engine import orchestrator
        ev = approved(conn, {**CROSS_WIRED})
        read(conn, ev, Stub(page("marathon_detail")))
        monkeypatch.setattr(orchestrator, "_telegram_notify", lambda t: None)
        out = orchestrator.summary(conn)
        text = Path(out["report_path"]).read_text()
        assert "## Held — source mismatch" in text
        assert "Line Dancing" in text and "Lakeside Marathon - Nevada Half Marathon" in text
        assert out["provenance_held"][0]["event_uid"] == ev["event_uid"]
