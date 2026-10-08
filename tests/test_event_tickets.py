"""Ticket / price info (cmo-port #21). TownLoops has no ticket fields, so the
detail-page pass stores the stated price (`events.price`) and ticket/RSVP
link (`events.rsvp_url`), and event-ship carries them in what it can send:
free events get the `free` tag, paid/RSVP events one fixed-format line at
the top of the description. Nothing is invented: a price is kept only when
the page states it, and "donation suggested" is not free."""

from __future__ import annotations

import json

import pytest

from engine import event_detail, event_tickets
from engine.event_extract import content_hash, upsert_event
from tests.fake_townloops import FakeAPI
from tests.test_event_detail import (  # noqa: F401 — fixtures
    DETAIL_URL, SEED_URL, Stub, conn, page, row, run, stage,
)
from tests.test_live_lookup import EV, _stage, _use, live_event, ship, snapshot  # noqa: F401


TICKETS_URL = "https://tickets.example/e/lakeview-trio-1009"
PAID = "**Tickets:** $25 general admission – $40 reserved table."
SOLD_OUT = "Tickets $30. SOLD OUT — join the waitlist at"
RSVP_URL = "https://forms.example/potluck-1010?ref=site"
DONATION = "Admission by donation, $10 suggested."
FREE = "This event is free and open to the public."


# --------------------------------------------------------------------------
# What a stated price means
# --------------------------------------------------------------------------

class TestTicketLine:
    def test_paid_with_a_range(self):
        assert event_tickets.ticket_line(PAID, TICKETS_URL) == f"Tickets: $25–$40 — {TICKETS_URL}"

    def test_paid_without_a_link(self):
        assert event_tickets.ticket_line("Tickets are $15 at the door.", None) == "Tickets: $15"

    def test_sold_out_with_a_waitlist(self):
        url = TICKETS_URL + "/waitlist"
        assert (event_tickets.ticket_line(SOLD_OUT, url)
                == f"Tickets: $30 (sold out; waitlist) — {url}")

    def test_sold_out_without_a_price(self):
        assert event_tickets.ticket_line("This show is sold out.", None) == "Tickets: sold out"

    def test_rsvp_only(self):
        assert event_tickets.ticket_line(None, RSVP_URL) == f"RSVP: {RSVP_URL}"

    def test_free_with_a_registration_link_is_an_rsvp(self):
        assert event_tickets.ticket_line(FREE, RSVP_URL) == f"RSVP: {RSVP_URL}"

    def test_free_without_a_link_has_no_line(self):
        assert event_tickets.ticket_line(FREE, None) is None

    def test_donation_suggested(self):
        assert event_tickets.ticket_line(DONATION, None) == "Tickets: by donation, $10 suggested"

    def test_no_info(self):
        assert event_tickets.ticket_line(None, None) is None

    def test_cents_are_kept_when_stated(self):
        assert event_tickets.ticket_line("Tickets $12.50", None) == "Tickets: $12.50"


class TestFree:
    @pytest.mark.parametrize("price", [FREE, "Free admission!", "Cost: $0", "Admission: FREE"])
    def test_stated_free(self, price):
        assert event_tickets.is_free(price)

    @pytest.mark.parametrize("price", [
        PAID, SOLD_OUT, DONATION, "Free for kids, $15 adults.", "Pay what you can.",
        "Gluten-free snacks provided.", "Free parking in the north lot.", None, "",
    ])
    def test_not_free(self, price):
        assert not event_tickets.is_free(price)


class TestTags:
    def test_free_event_gets_the_free_tag_first(self):
        assert event_tickets.ticket_tags(["jazz"], FREE) == ["free", "jazz"]

    def test_free_tag_is_not_repeated(self):
        assert event_tickets.ticket_tags(["free", "jazz"], FREE) == ["free", "jazz"]

    @pytest.mark.parametrize("price", [PAID, SOLD_OUT, DONATION])
    def test_a_stated_price_never_keeps_free(self, price):
        assert event_tickets.ticket_tags(["Free", "jazz"], price) == ["jazz"]

    def test_no_info_leaves_tags_alone(self):
        assert event_tickets.ticket_tags(["free", "jazz"], None) == ["free", "jazz"]

    def test_still_capped_at_the_api_limit(self):
        tags = [f"t{i}" for i in range(12)]
        assert event_tickets.ticket_tags(tags, FREE) == ["free"] + tags[:9]


class TestWithTicketLine:
    LINE = f"Tickets: $25–$40 — {TICKETS_URL}"

    def test_line_goes_first(self):
        assert (event_tickets.with_ticket_line("Weekly jazz.", self.LINE)
                == f"{self.LINE}\n\nWeekly jazz.")

    def test_no_description_is_just_the_line(self):
        assert event_tickets.with_ticket_line(None, self.LINE) == self.LINE

    def test_no_line_leaves_the_description_alone(self):
        assert event_tickets.with_ticket_line("Weekly jazz.", None) == "Weekly jazz."
        assert event_tickets.with_ticket_line(f"{self.LINE}\n\nWeekly jazz.", None) \
            == f"{self.LINE}\n\nWeekly jazz."

    def test_idempotent(self):
        once = event_tickets.with_ticket_line("Weekly jazz.", self.LINE)
        assert event_tickets.with_ticket_line(once, self.LINE) == once

    def test_an_outdated_line_is_replaced_not_stacked(self):
        old = "Tickets: $20 — https://tickets.example/old\n\nWeekly jazz."
        assert (event_tickets.with_ticket_line(old, self.LINE)
                == f"{self.LINE}\n\nWeekly jazz.")

    def test_line_survives_truncation(self):
        out = event_tickets.with_ticket_line("word " * 2000, self.LINE)
        assert len(out) <= event_tickets.DESCRIPTION_MAX
        assert out.startswith(self.LINE + "\n\n")
        assert out.endswith("…")


# --------------------------------------------------------------------------
# The detail-page read
# --------------------------------------------------------------------------

def _answer(price=None, url=None, quotes=()):
    return json.dumps({"accessibility_quotes": list(quotes), "price_quote": price,
                       "ticket_url": url})


class TestExtract:
    def test_prompt_asks_for_price_and_link(self, conn):
        stub = Stub(page("paid_range"), raw=_answer())
        run(conn, stage(conn), stub)
        assert '"price_quote"' in stub.prompts[0] and '"ticket_url"' in stub.prompts[0]

    def test_paid_range_and_ticket_link_are_stored(self, conn):
        ev = stage(conn)
        result = run(conn, ev, Stub(page("paid_range"), raw=_answer(
            "Tickets: $25 general admission – $40 reserved table.", TICKETS_URL)))
        assert result.status == "read"
        r = row(conn, ev)
        assert r["price"] == "Tickets: $25 general admission – $40 reserved table."
        assert r["rsvp_url"] == TICKETS_URL

    def test_sold_out_waitlist(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("sold_out"), raw=_answer(
            "Tickets $30. SOLD OUT — join the waitlist", TICKETS_URL + "/waitlist")))
        r = row(conn, ev)
        assert event_tickets.ticket_line(r["price"], r["rsvp_url"]) == \
            f"Tickets: $30 (sold out; waitlist) — {TICKETS_URL}/waitlist"

    def test_rsvp_only(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("rsvp"), raw=_answer(None, RSVP_URL)))
        r = row(conn, ev)
        assert r["price"] is None and r["rsvp_url"] == RSVP_URL

    def test_donation_is_stored_but_not_free(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("donation"), raw=_answer(DONATION)))
        assert row(conn, ev)["price"] == DONATION
        assert not event_tickets.is_free(row(conn, ev)["price"])

    def test_free(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("free"), raw=_answer(FREE)))
        assert event_tickets.is_free(row(conn, ev)["price"])

    def test_no_info_is_null(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("no_access"), raw=_answer()))
        r = row(conn, ev)
        assert r["price"] is None and r["rsvp_url"] is None

    def test_a_price_not_on_the_page_is_dropped(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("no_access"), raw=_answer("Tickets are $20.")))
        assert row(conn, ev)["price"] is None

    def test_a_quote_that_states_no_price_is_dropped(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("no_access"), raw=_answer("Doors open at 7:30.")))
        assert row(conn, ev)["price"] is None

    def test_free_parking_alone_is_not_a_price(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("paid_range"), raw=_answer("Free parking in the north lot.")))
        assert row(conn, ev)["price"] is None

    @pytest.mark.parametrize("url", [
        "https://tickets.example/e/somewhere-else",   # not on the page
        "https://lakeview.example/",                  # the venue's homepage
        "https://tahoe.example/",                     # the site's homepage
        SEED_URL,                                     # the listing page
        "mailto:box@tahoe.example",
        "javascript:void(0)",
        None, 42,
    ])
    def test_a_bad_link_is_dropped(self, conn, url):
        ev = stage(conn)
        markdown = page("paid_range") + f"\n[All events]({SEED_URL})\n"
        run(conn, ev, Stub(markdown, raw=_answer(PAID.replace("**", ""), url)))
        assert row(conn, ev)["rsvp_url"] is None

    def test_the_event_page_itself_is_a_valid_link(self, conn):
        ev = stage(conn)
        markdown = page("rsvp") + f"\n[This event]({DETAIL_URL})\n"
        run(conn, ev, Stub(markdown, raw=_answer(None, DETAIL_URL)))
        assert row(conn, ev)["rsvp_url"] == DETAIL_URL

    def test_the_read_does_not_change_the_content_hash(self, conn):
        listing = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "source_url": DETAIL_URL}
        ev = stage(conn)
        run(conn, ev, Stub(page("paid_range"), raw=_answer(PAID.replace("**", ""), TICKETS_URL)))
        assert upsert_event(conn, "Tahoe Arts", listing) == "unchanged"
        assert row(conn, ev)["content_hash"] == content_hash(listing)


# --------------------------------------------------------------------------
# event-ship (the `conn` fixture is test_event_detail's: the same fresh DB)
# --------------------------------------------------------------------------

DESC = EV["description"]
LINE = f"Tickets: $25–$40 — {TICKETS_URL}"


def _with_tickets(conn, uid, price, url):
    conn.execute("UPDATE events SET price = ?, rsvp_url = ? WHERE event_uid = ?", (price, url, uid))


class TestShipCreate:
    def test_paid_line_tops_the_description_and_free_is_dropped(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _with_tickets(conn, _stage(ship, monkeypatch, conn), PAID, TICKETS_URL)
        assert ship.main() == 0
        [created] = api.creates()
        assert created["description"] == f"{LINE}\n\n{DESC}"
        assert "free" not in created.get("tags", [])

    def test_rsvp_line(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _with_tickets(conn, _stage(ship, monkeypatch, conn), None, RSVP_URL)
        assert ship.main() == 0
        assert api.creates()[0]["description"] == f"RSVP: {RSVP_URL}\n\n{DESC}"

    def test_free_gets_the_tag_and_no_line(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        uid = _stage(ship, monkeypatch, conn, ev={**EV, "tags": ["jazz"]})
        _with_tickets(conn, uid, FREE, None)
        assert ship.main() == 0
        [created] = api.creates()
        assert created["description"] == DESC
        assert created["tags"][0] == "free"

    def test_donation_never_ships_free(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _with_tickets(conn, _stage(ship, monkeypatch, conn), DONATION, None)
        assert ship.main() == 0
        [created] = api.creates()
        assert created["description"] == f"Tickets: by donation, $10 suggested\n\n{DESC}"
        assert "free" not in created.get("tags", [])

    def test_no_info_ships_as_before(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI())
        _stage(ship, monkeypatch, conn)
        assert ship.main() == 0
        [created] = api.creates()
        assert created["description"] == DESC
        assert created["tags"] == ["free"]   # the listing's own tag stays


class TestShipAlreadyLive:
    def test_line_is_added_to_a_live_description_once(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description="Live words.")]))
        _with_tickets(conn, _stage(ship, monkeypatch, conn), PAID, TICKETS_URL)
        assert ship.main() == 0
        assert api.events["jazz-live"]["description"] == f"{LINE}\n\nLive words."
        n = len(api.patches())
        assert ship.main() == 0                        # re-ship: nothing to change
        assert len(api.patches()) == n
        assert api.events["jazz-live"]["description"] == f"{LINE}\n\nLive words."

    def test_blank_live_description_gets_ours_with_the_line(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description=None)]))
        _with_tickets(conn, _stage(ship, monkeypatch, conn), PAID, TICKETS_URL)
        assert ship.main() == 0
        assert api.events["jazz-live"]["description"] == f"{LINE}\n\n{DESC}"

    def test_a_list_row_without_description_is_read_in_full_first(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description="Live words.")]))
        real_list = api.list

        def list_without_description(resource, **params):
            return [{k: v for k, v in r.items() if k != "description"}
                    for r in real_list(resource, **params)]
        api.list = list_without_description
        _with_tickets(conn, _stage(ship, monkeypatch, conn), PAID, TICKETS_URL)
        assert ship.main() == 0
        assert api.events["jazz-live"]["description"] == f"{LINE}\n\nLive words."

    def test_free_tag_is_added_to_the_live_set(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description="Live words.",
                                                    tags=["jazz"])]))
        uid = _stage(ship, monkeypatch, conn, ev={**EV, "tags": []})
        _with_tickets(conn, uid, FREE, None)
        assert ship.main() == 0
        assert "free" in api.events["jazz-live"]["tags"]
        assert api.events["jazz-live"]["description"] == "Live words."


# --------------------------------------------------------------------------
# Review probes: a wrong price or a wrong `free` is worse than none
# --------------------------------------------------------------------------

class TestNeverInvented:
    @pytest.mark.parametrize("price, line", [
        ("Tickets $30; ages 12+ suggested", "Tickets: $30"),
        ("Free admission; parking $5", None),
        ("Free entry; drinks from $6", None),
        ("Wristbands $0-$20", "Tickets: $0–$20"),
        ("Tickets $10.5", "Tickets: $10.50"),
        ("Not sold out yet - $20", "Tickets: $20"),
        ("Free event, waitlist available", None),
    ])
    def test_line(self, price, line):
        assert event_tickets.ticket_line(price, None) == line

    @pytest.mark.parametrize("price, kind", [
        ("Free admission; parking $5", "free"),
        ("Free event, waitlist available", "free"),
        ("Free with museum admission", None),
        ("Tickets $30; ages 12+ suggested", "paid"),
    ])
    def test_kind(self, price, kind):
        assert event_tickets.price_kind(price) == kind

    def test_a_quoted_fragment_is_read_in_its_page_context(self):
        page_text = "Free parking in lot. Tickets $20 at the door."
        assert event_detail.ticket_price("Free", page_text) is None
        assert event_detail.ticket_price("Tickets $20", page_text) == "Tickets $20"

    @pytest.mark.parametrize("url", ["https://hall.example/home", "https://hall.example/index.html"])
    def test_homepage_paths_are_not_ticket_links(self, url):
        assert event_detail.ticket_url(url, f"[Home]({url})", set()) is None

    def test_a_source_written_ticket_sentence_is_not_replaced(self):
        desc = "Tickets: $20 at the door, cash only.\n\nGreat show."
        assert (event_tickets.with_ticket_line(desc, f"RSVP: {RSVP_URL}")
                == f"RSVP: {RSVP_URL}\n\n{desc}")


class TestLiveFreeTag:
    def test_a_paid_event_drops_free_from_the_live_set(self, ship, monkeypatch, conn):
        api = _use(monkeypatch, FakeAPI([live_event("jazz-live", description="Live words.",
                                                    tags=["free", "jazz"])]))
        _with_tickets(conn, _stage(ship, monkeypatch, conn), PAID, None)
        assert ship.main() == 0
        assert api.events["jazz-live"]["tags"] == ["jazz"]
