"""engine.performer_research: the performer/creator evidence ladder.

Event evidence → venue/event page links → public-profile metadata →
search (discovery only). Bio/description is null unless it is verbatim
text from an identified source; ambiguous identities go to the operator.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine import performer_research as pr
from engine.derive_helpers import insert_entity, link_event_to_entity
from engine.entity_enrich import Budget

TOWN = "South Lake Tahoe"


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


@pytest.fixture(autouse=True)
def no_image_head(monkeypatch):
    import engine.research as research
    monkeypatch.setattr(research, "verify_image", lambda url, timeout=10.0: True)


class Web:
    """Fake fetch + search; records every call."""

    def __init__(self, pages=None, hits=None):
        self.pages = pages or {}
        self.hits = hits or []
        self.fetched: list[str] = []
        self.searched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        html = self.pages.get(url)
        return (html, url) if html else (None, None)

    def search(self, query):
        self.searched.append(query)
        return self.hits


def _event(conn, uid, title, source_url, venue=None, venue_name=None):
    """`venue` links a venue row; `venue_name` is only the event's own venue
    text (an event shipped name-only, never bound to a venue row)."""
    conn.execute(
        "INSERT INTO events(event_uid, source, source_kind, name, date, date_day, source_url, "
        "venue_name, venue_raw, first_seen_at, last_seen_at, content_hash, last_modified_at) "
        "VALUES (?, 'seed', 'scraped', ?, '2026-10-10T20:00:00', '2026-10-10', ?, ?, ?, "
        "'x', 'x', 'h', 'x')",
        (uid, title, source_url, venue_name, venue_name),
    )
    if venue:
        vuid = f"v-{venue}"
        if not conn.execute("SELECT 1 FROM venues WHERE entity_uid = ?", (vuid,)).fetchone():
            insert_entity(conn, "venues", vuid, {"name": venue, "source": "t", "source_kind": "derived"})
        link_event_to_entity(conn, uid, "venue", vuid, role="venue")


def _performer(conn, name="Jane Doe", uid="p1", kind="performer", events=(), **fields):
    table = "performers" if kind == "performer" else "creators"
    insert_entity(conn, table, uid, {"name": name, "source": "t", "source_kind": "derived",
                                     "enrichment_status": "pending", **fields})
    for ev in events:
        link_event_to_entity(conn, ev, kind, uid, role=kind)
    return uid


def _row(conn, uid="p1", table="performers"):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (uid,)).fetchone())


def _research(conn, uid="p1"):
    return json.loads(_row(conn, uid)["extra_json"] or "{}").get("research")


def page(title, description="", og_image="", body=""):
    meta = f'<meta property="og:title" content="{title}">'
    if description:
        meta += f'<meta property="og:description" content="{description}">'
    if og_image:
        meta += f'<meta property="og:image" content="{og_image}">'
    return f"<html><head><title>{title}</title>{meta}</head><body>{body}</body></html>"


VENUE_URL = "https://gunbarrel.example/calendar"
IG = "https://www.instagram.com/janedoemusic/"


class TestEventEvidence:
    def test_collects_events_venues_and_recurrence(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _event(conn, "e2", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _event(conn, "e3", "Jane Doe live", "https://grove.example/music", venue="The Grove")
        _performer(conn, events=("e1", "e2", "e3"))
        ev = pr.event_evidence(conn, "performer", "p1")
        assert ev.event_count == 3
        assert ev.venues == ["Gunbarrel Tavern", "The Grove"]
        assert ev.source_urls == [VENUE_URL, "https://grove.example/music"]

    def test_includes_the_venue_of_an_event_not_bound_to_a_venue_row(self, conn):
        """SLT: Direct From Sweden's event shipped name-only (ambiguous live
        venue), so the performer's research recorded `venues: []`."""
        _event(conn, "e1", "Direct From Sweden: The Music of ABBA", VENUE_URL,
               venue_name="Harrah’s Lake Tahoe")
        _event(conn, "e2", "Jane Doe live", "https://grove.example/music", venue="The Grove",
               venue_name="Grove (raw)")
        _performer(conn, events=("e1", "e2"))
        assert pr.event_evidence(conn, "performer", "p1").venues == [
            "Harrah’s Lake Tahoe", "The Grove"]


class TestLadder:
    def _setup(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))

    def test_venue_page_link_to_performer_profile_verifies(self, conn):
        self._setup(conn)
        web = Web(pages={
            VENUE_URL: page("Calendar", body=f'<a href="{IG}">Jane Doe</a> plays Friday'),
            IG: page("Jane Doe (@janedoemusic) • Instagram photos and videos"),
        })
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified"
        assert f.payload["instagram"] == IG
        assert web.searched == []          # search is the last rung

    def test_booking_alone_does_not_verify_a_search_hit(self, conn):
        self._setup(conn)
        web = Web(pages={
            VENUE_URL: page("Calendar", body="Jane Doe plays Friday"),
            IG: page("Jane Doe (@janedoemusic) • Instagram photos and videos"),
        }, hits=[{"url": IG, "title": "Jane Doe (@janedoemusic)"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        # A name-only profile (no role, no locality) is non-evidence.
        assert f.status == "unresolved"
        assert f.payload == {}

    def test_search_hit_with_name_locality_and_role_verifies(self, conn):
        self._setup(conn)
        bio = "Singer-songwriter based in South Lake Tahoe. Acoustic covers &amp; originals."
        web = Web(pages={
            VENUE_URL: page("Calendar", body="Jane Doe plays Friday"),
            IG: page("Jane Doe (@janedoemusic) • Instagram photos and videos",
                     description=f'1,234 Followers, 56 Following, 78 Posts - Jane Doe (@janedoemusic) on Instagram: &quot;{bio}&quot;'),
        }, hits=[{"url": IG, "title": "Jane Doe (@janedoemusic) • Instagram"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified"
        assert f.payload["instagram"] == IG
        assert f.payload["description"] == ("Singer-songwriter based in South Lake Tahoe. "
                                            "Acoustic covers & originals.")
        assert f.description_source == IG

    def test_profile_naming_a_linked_venue_counts_as_locality(self, conn):
        self._setup(conn)
        web = Web(pages={
            IG: page("Jane Doe | Music", description="Live music every Friday at Gunbarrel Tavern"),
        }, hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified"

    def test_name_and_role_without_locality_is_probable(self, conn):
        self._setup(conn)
        web = Web(pages={
            IG: page("Jane Doe", description="Singer-songwriter based in Austin, TX"),
        }, hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "probable"
        assert f.payload == {}

    def test_two_distinct_name_matches_is_ambiguous(self, conn):
        """A common name: two public profiles fit and nothing bridges either
        one to the booking."""
        self._setup(conn)
        other = "https://www.instagram.com/janedoe.dj/"
        web = Web(pages={
            IG: page("Jane Doe", description="Singer"),
            other: page("Jane Doe", description="DJ in Reno"),
        }, hits=[{"url": IG, "title": "Jane Doe"}, {"url": other, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "ambiguous"
        assert f.payload == {}

    def test_conflicting_verified_profiles_are_ambiguous(self, conn):
        self._setup(conn)
        other = "https://www.instagram.com/janedoe.tahoe/"
        desc = "Musician in South Lake Tahoe"
        web = Web(pages={
            IG: page("Jane Doe", description=desc),
            other: page("Jane Doe", description=desc),
        }, hits=[{"url": IG, "title": "Jane Doe"}, {"url": other, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "ambiguous"

    def test_no_name_match_is_unresolved(self, conn):
        self._setup(conn)
        web = Web(hits=[{"url": "https://someone.example", "title": "Somebody Else"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "unresolved"

    def test_search_snippet_never_verifies(self, conn):
        """The hit's own title/snippet says everything, but the page can't be
        read — that is not evidence, so nothing is attached."""
        self._setup(conn)
        web = Web(hits=[{"url": IG, "title": "Jane Doe — South Lake Tahoe musician",
                         "description": "Jane Doe, singer in South Lake Tahoe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "deferred"
        assert f.payload == {}

    def test_budget_exhausted_defers(self, conn):
        self._setup(conn)
        web = Web(pages={VENUE_URL: page("Calendar")}, hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(1), fetch=web.fetch, search=web.search)
        assert f.status == "deferred"
        assert len(web.fetched) + len(web.searched) == 1


class TestNoInvention:
    def _verified_web(self, description=""):
        return Web(pages={
            VENUE_URL: page("Calendar", body=f'<a href="{IG}">Jane Doe</a>'),
            IG: page("Jane Doe (@janedoemusic) • Instagram photos and videos", description=description),
        })

    def _run(self, conn, web):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        return pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)

    def test_follower_count_boilerplate_is_not_a_bio(self, conn):
        f = self._run(conn, self._verified_web(
            "1,234 Followers, 56 Following, 78 Posts - See Instagram photos and videos "
            "from Jane Doe (@janedoemusic)"))
        assert f.status == "verified"
        assert "description" not in f.payload

    def test_no_description_means_null_never_a_template(self, conn):
        f = self._run(conn, self._verified_web())
        pr.apply_finding(conn, "performer", "p1", f)
        assert _row(conn)["description"] is None

    def test_description_is_verbatim_from_the_source(self, conn):
        text = "Jane plays folk and blues across the basin."
        web = self._verified_web(text)
        f = self._run(conn, web)
        assert f.payload["description"] == text
        assert text in web.pages[IG]

    def test_review_findings_fill_nothing(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages={IG: page("Jane Doe", description="Singer-songwriter in Austin")},
                  hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        filled = pr.apply_finding(conn, "performer", "p1", f)
        row = _row(conn)
        assert filled == [] and row["instagram"] is None and row["description"] is None
        assert row["enrichment_status"] == "needs-review"
        rec = _research(conn)
        assert rec["status"] == "probable"
        assert rec["candidates"][0]["url"] == IG


class TestApply:
    def test_verified_fills_blanks_only_and_records_source(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",), website="https://operator-set.example")
        web = Web(pages={
            VENUE_URL: page("Calendar", body=f'<a href="{IG}">Jane Doe</a>'),
            IG: page("Jane Doe", description="Folk singer."),
        })
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        filled = pr.apply_finding(conn, "performer", "p1", f)
        row = _row(conn)
        assert set(filled) == {"instagram", "description"}
        assert row["website"] == "https://operator-set.example"
        assert row["enrichment_status"] == "enriched"
        assert _research(conn)["description_source"] == IG

    def test_unresolved_is_not_found(self, conn):
        _performer(conn)
        f = pr.Finding("unresolved", why="no public profile names this performer")
        pr.apply_finding(conn, "performer", "p1", f)
        assert _row(conn)["enrichment_status"] == "not-found"

    def test_deferred_stays_pending_but_records_the_attempt(self, conn):
        _performer(conn)
        pr.apply_finding(conn, "performer", "p1", pr.Finding("deferred", why="budget"))
        assert _row(conn)["enrichment_status"] == "pending"
        assert _research(conn)["status"] == "deferred"

    def test_creator_gets_bio_not_description(self, conn):
        _event(conn, "e1", "Pottery with Sam Lee", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, name="Sam Lee", uid="c1", kind="creator", events=("e1",))
        site = "https://samleeceramics.example/"
        web = Web(pages={site: page("Sam Lee Ceramics",
                                    description="Sam Lee is a ceramic artist in South Lake Tahoe.")},
                  hits=[{"url": site, "title": "Sam Lee Ceramics"}])
        f = pr.investigate(conn, "creator", "c1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified"
        pr.apply_finding(conn, "creator", "c1", f)
        row = _row(conn, "c1", "creators")
        assert row["bio"] == "Sam Lee is a ceramic artist in South Lake Tahoe."
        assert row["website"] == site


class TestBatch:
    def test_takes_at_most_limit_and_skips_live_and_done(self, conn):
        for i in range(5):
            _performer(conn, name=f"Band {i}", uid=f"p{i}")
        _performer(conn, name="Live Band", uid="live", source_kind="live")
        conn.execute("UPDATE performers SET enrichment_status = 'enriched' WHERE entity_uid = 'p0'")
        picked = pr.next_batch(conn, limit=3)
        assert len(picked) == 3
        assert ("performer", "p0") not in picked and ("performer", "live") not in picked

    def test_never_attempted_first_then_most_booked(self, conn):
        for ev in ("e1", "e2", "e3"):
            _event(conn, ev, "x", "https://x.example")
        _performer(conn, name="Rare", uid="rare", events=("e1",))
        _performer(conn, name="Busy", uid="busy", events=("e2", "e3"))
        _performer(conn, name="Tried", uid="tried", events=("e1", "e2", "e3"))
        pr.apply_finding(conn, "performer", "tried", pr.Finding("deferred", why="budget"))
        assert pr.next_batch(conn, limit=3) == [
            ("performer", "busy"), ("performer", "rare"), ("performer", "tried")]

    def test_run_batch_processes_capped_batch(self, conn):
        for i in range(4):
            _performer(conn, name=f"Nobody {i}", uid=f"p{i}")
        web = Web(hits=[{"url": "https://someone.example", "title": "Somebody Else"}])
        results = pr.run_batch(conn, TOWN, limit=3, lookups=4, fetch=web.fetch, search=web.search)
        assert len(results) == 3
        assert all(r["status"] == "unresolved" for r in results)
        statuses = [r["enrichment_status"] for r in
                    conn.execute("SELECT enrichment_status FROM performers ORDER BY entity_uid")]
        assert statuses.count("not-found") == 3 and statuses.count("pending") == 1


class TestOperatorResolve:
    def _review(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages={IG: page("Jane Doe", description="Singer-songwriter in Austin")},
                  hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        pr.apply_finding(conn, "performer", "p1", f)

    def test_listed_for_review(self, conn):
        self._review(conn)
        items = pr.review_queue(conn)
        assert [(i["kind"], i["entity_uid"], i["status"]) for i in items] == [
            ("performer", "p1", "probable")]

    def test_accept_applies_the_stored_candidate(self, conn):
        self._review(conn)
        filled = pr.resolve(conn, "performer", "p1", accept_url=IG)
        row = _row(conn)
        assert "instagram" in filled and row["instagram"] == IG
        assert row["description"] == "Singer-songwriter in Austin"
        assert row["enrichment_status"] == "enriched"
        assert _research(conn)["status"] == "operator-verified"

    def test_accept_unknown_url_is_refused(self, conn):
        self._review(conn)
        with pytest.raises(ValueError):
            pr.resolve(conn, "performer", "p1", accept_url="https://made-up.example")

    def test_reject_marks_not_found(self, conn):
        self._review(conn)
        assert pr.resolve(conn, "performer", "p1") == []
        assert _row(conn)["enrichment_status"] == "not-found"
        assert _research(conn)["status"] == "operator-rejected"


class TestCandidatesMustBeTheirOwnPresence:
    """Event listings, ticket pages and posts name the performer and the
    town — they are not the performer's identity (seen live on SLT:
    visitlaketahoe / livenation / extrachill event pages came back as
    search hits)."""

    def _setup(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))

    @pytest.mark.parametrize("url", [
        "https://www.livenation.com/event/17C8v/jane-doe",
        "https://www.ticketmaster.com/jane-doe-south-lake-tahoe/event/39006",
        "https://visittown.example/event/jane-doe-at-gunbarrel/2026-10-10/",
        "https://www.instagram.com/p/Cxyz123/",
        "https://www.facebook.com/events/123456/",
    ])
    def test_listing_pages_are_not_candidates(self, conn, url):
        self._setup(conn)
        web = Web(pages={url: page("Jane Doe live in South Lake Tahoe",
                                   description="Jane Doe, singer, live in South Lake Tahoe")},
                  hits=[{"url": url, "title": "Jane Doe live in South Lake Tahoe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "unresolved"
        assert f.candidates == [] and url not in web.fetched

    def test_venue_link_to_a_ticket_page_is_not_a_bridge(self, conn):
        self._setup(conn)
        tix = "https://www.eventbrite.com/e/jane-doe-tickets-123"
        web = Web(pages={VENUE_URL: page("Calendar", body=f'<a href="{tix}">Jane Doe tickets</a>')})
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.candidates == [] and f.payload == {} and tix not in web.fetched

    def test_search_hit_on_the_event_source_host_is_not_a_candidate(self, conn):
        self._setup(conn)
        same_host = "https://gunbarrel.example/performers/jane-doe"
        web = Web(pages={same_host: page("Jane Doe", description="Singer in South Lake Tahoe")},
                  hits=[{"url": same_host, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "unresolved"

    def test_search_hit_on_an_unrelated_site_is_not_a_candidate(self, conn):
        self._setup(conn)
        url = "https://localnews.example/2026/jane-doe-interview"
        web = Web(pages={url: page("Jane Doe", description="Singer in South Lake Tahoe")},
                  hits=[{"url": url, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "unresolved"

    def test_own_site_page_records_the_site_root_as_website(self, conn):
        self._setup(conn)
        url = "https://janedoemusic.example/about"
        web = Web(pages={url: page("About | Jane Doe", description="Jane Doe is a singer in South Lake Tahoe.")},
                  hits=[{"url": url, "title": "About | Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified"
        assert f.payload["website"] == "https://janedoemusic.example/"

    def test_short_name_own_site_matches_host_label(self, conn):
        _event(conn, "e1", "An evening with moe.", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, name="moe.", events=("e1",))
        url = "https://www.moe.org/pages/tour"
        web = Web(pages={url: page("moe.", description="moe. tour dates")},
                  hits=[{"url": url, "title": "moe."}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "probable"     # name + role (tour), nothing local
        assert f.candidates[0].fields["website"] == "https://www.moe.org/"


class TestReviewFixes:
    def _setup(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))

    def test_search_auth_failure_escalates(self, conn):
        from engine import discovery
        from engine.research import FirecrawlAuthError
        self._setup(conn)

        def search(q):
            raise discovery.FirecrawlError("[firecrawl-search][http 401] 'x': bad key")
        with pytest.raises(FirecrawlAuthError):
            pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=Web().fetch, search=search)

    def test_deferred_gives_up_after_max_attempts(self, conn):
        _performer(conn)
        for _ in range(pr.MAX_DEFERRALS - 1):
            pr.apply_finding(conn, "performer", "p1", pr.Finding("deferred", why="unreadable"))
            assert _row(conn)["enrichment_status"] == "pending"
        pr.apply_finding(conn, "performer", "p1", pr.Finding("deferred", why="unreadable"))
        assert _row(conn)["enrichment_status"] == "not-found"
        assert _research(conn)["status"] == "unresolved"

    def test_tried_rows_rotate_by_last_attempt_not_bookings(self, conn):
        for ev in ("e1", "e2", "e3"):
            _event(conn, ev, "x", "https://x.example")
        _performer(conn, name="Busy", uid="busy", events=("e1", "e2", "e3"))
        _performer(conn, name="Quiet", uid="quiet", events=("e1",))
        pr.apply_finding(conn, "performer", "quiet", pr.Finding("deferred", why="x"))
        conn.execute("UPDATE performers SET extra_json = json_set(extra_json, '$.research.at', "
                     "'2026-01-01T00:00:00+00:00') WHERE entity_uid = 'quiet'")
        pr.apply_finding(conn, "performer", "busy", pr.Finding("deferred", why="x"))
        assert pr.next_batch(conn, limit=1) == [("performer", "quiet")]

    def test_broad_words_are_not_a_role(self, conn):
        self._setup(conn)
        web = Web(pages={IG: page("Jane Doe", description="Country living in South Lake Tahoe. Tourism fan.")},
                  hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status != "verified" and f.payload == {}

    def test_venue_link_to_an_article_is_not_a_bridge(self, conn):
        self._setup(conn)
        article = "https://localnews.example/2026/jane-doe-interview"
        web = Web(pages={
            VENUE_URL: page("Calendar", body=f'<a href="{article}">Jane Doe</a>'),
            article: page("Jane Doe talks music", description="An interview."),
        })
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status != "verified" and article not in web.fetched

    def test_venue_link_to_a_site_homepage_is_a_bridge(self, conn):
        self._setup(conn)
        home = "https://thecowboys.band/"
        web = Web(pages={
            VENUE_URL: page("Calendar", body=f'<a href="{home}">Jane Doe</a>'),
            home: page("Jane Doe", description="Songs."),
        })
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch, search=web.search)
        assert f.status == "verified" and f.payload["website"] == home

    def test_resolve_refuses_rows_not_under_review(self, conn):
        _performer(conn)
        conn.execute("UPDATE performers SET enrichment_status = 'enriched' WHERE entity_uid = 'p1'")
        with pytest.raises(ValueError):
            pr.resolve(conn, "performer", "p1")
        assert _row(conn)["enrichment_status"] == "enriched"

    def test_empty_search_is_deferred_not_unresolved(self, conn):
        """A search backend that's throttled returns nothing; that isn't
        proof the performer has no public presence."""
        self._setup(conn)
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=Web().fetch,
                           search=Web().search)
        assert f.status == "deferred"


class TestUnreadablePages:
    """A bot challenge, a block page or an empty shell says nothing about the
    performer — it is an unread page (deferred), never a read page that fails
    to name them (unresolved)."""

    SITE = "https://janedoemusic.example/"

    def _run(self, conn, html):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages={self.SITE: html}, hits=[{"url": self.SITE, "title": "Jane Doe Music"}])
        return pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch,
                              search=web.search)

    @pytest.mark.parametrize("html", [
        page("Just a moment...", body="Checking your browser before accessing the site."),
        page("Attention Required! | Cloudflare", body="Sorry, you have been blocked. Access denied."),
        # Firecrawl's `html` format drops <head>: no title, no meta — nothing to judge.
        "<html><body><div id='cookie-banner'>Manage consent</div>" + "<p>ABBA hits.</p>" * 40
        + "</body></html>",
    ])
    def test_challenge_blocked_or_empty_shell_defers(self, conn, html):
        f = self._run(conn, html)
        assert f.status == "deferred"
        assert f.payload == {}
        assert f.candidates[0].unreadable

    def test_repeatedly_unreadable_gives_up_after_max_deferrals(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages={self.SITE: page("Just a moment...")},
                  hits=[{"url": self.SITE, "title": "Jane Doe Music"}])
        for _ in range(pr.MAX_DEFERRALS - 1):
            pr.run_batch(conn, TOWN, fetch=web.fetch, search=web.search)
            assert _row(conn)["enrichment_status"] == "pending"
        pr.run_batch(conn, TOWN, fetch=web.fetch, search=web.search)
        assert _row(conn)["enrichment_status"] == "not-found"

    def test_short_profile_with_a_title_is_still_read(self, conn):
        f = self._run(conn, page("Jane Doe", description="Singer-songwriter based in Austin, TX"))
        assert f.status == "probable"


class TestOwnSiteShowsPage:
    """A touring act's own site bridges locality through its own shows /
    tour-dates page listing this town or a linked venue — never through the
    name alone."""

    SITE = "https://janedoemusic.example/"
    TOUR = "https://janedoemusic.example/tour/"

    def _run(self, conn, pages, lookups=6):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages=pages, hits=[{"url": self.SITE, "title": "Jane Doe Music"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(lookups), fetch=web.fetch,
                           search=web.search)
        return f, web

    def _home(self, body=""):
        return page("Jane Doe | Music", description="Touring singer-songwriter.",
                    body=f'<a href="/tour/">Tour dates</a> {body}')

    @pytest.mark.parametrize("listing", [
        "Oct 10 — Gunbarrel Tavern, Stateline NV",
        "Oct 10 — The Tavern, South Lake Tahoe, CA",
    ])
    def test_shows_page_naming_a_linked_venue_or_the_town_verifies(self, conn, listing):
        f, web = self._run(conn, {self.SITE: self._home(),
                                  self.TOUR: page("Tour | Jane Doe", body=f"<li>{listing}</li>")})
        assert f.status == "verified"
        assert f.payload["website"] == self.SITE
        assert f.candidates[0].shows_page == self.TOUR
        assert self.TOUR in web.fetched

    def test_tour_dates_on_the_homepage_itself_verify_without_another_fetch(self, conn):
        home = page("Jane Doe | Music", description="Touring singer-songwriter.",
                    body="<h2>Tour dates</h2><li>Oct 10 — Gunbarrel Tavern</li>")
        f, web = self._run(conn, {self.SITE: home})
        assert f.status == "verified"
        assert f.candidates[0].shows_page == self.SITE
        assert web.fetched.count(self.SITE) == 1 and self.TOUR not in web.fetched

    def test_shows_page_not_naming_the_town_is_probable(self, conn):
        f, _ = self._run(conn, {self.SITE: self._home(),
                                self.TOUR: page("Tour", body="<li>Oct 12 — Yoshi's, Oakland, CA</li>")})
        assert f.status == "probable" and f.payload == {}

    def test_shows_page_needs_the_role_too(self, conn):
        home = page("Jane Doe", body='<a href="/tour/">Tour dates</a>')
        f, _ = self._run(conn, {self.SITE: home,
                                self.TOUR: page("Dates", body="<li>Gunbarrel Tavern</li>")})
        assert f.status != "verified" and f.payload == {}

    def test_off_site_shows_link_is_not_followed(self, conn):
        other = "https://www.bandsintown.example/a/jane-doe"
        home = page("Jane Doe | Music", description="Touring singer-songwriter.",
                    body=f'<a href="{other}">Tour dates</a>')
        f, web = self._run(conn, {self.SITE: home, other: page("x", body="Gunbarrel Tavern")})
        assert f.status == "probable" and other not in web.fetched

    def test_platform_profile_gets_no_shows_page_step(self, conn):
        _event(conn, "e1", "Jane Doe at Gunbarrel", VENUE_URL, venue="Gunbarrel Tavern")
        _performer(conn, events=("e1",))
        web = Web(pages={IG: page("Jane Doe", description="Singer. Tour dates in bio.")},
                  hits=[{"url": IG, "title": "Jane Doe"}])
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch,
                           search=web.search)
        assert f.status == "probable" and web.fetched == [VENUE_URL, IG]


class TestDirectFromSweden:
    """SLT, 2026-10: an ABBA tribute act booked at Harrah's Lake Tahoe via
    visitlaketahoe.com, found by search on its own site — recorded
    unresolved with `venues: []` and shipped with no metadata."""

    LISTING = ("https://visitlaketahoe.com/event/"
               "direct-from-sweden-the-music-of-abba-at-harrahs-lake-tahoe/")
    SITE = "https://sosdirectfromsweden.com/"
    DESCRIPTION = ("The closest ABBA experience you’ll ever get with some of the best "
                   "and most talented singers and musicians. Buy tickets online today.")

    def _home(self, tour):
        return page("Direct from Sweden - The Music of ABBA", description=self.DESCRIPTION,
                    body=f"<nav><a href='#tourdates'>Tour dates</a></nav>"
                         f"<h2 id='tourdates'>TOUR DATES</h2>{tour}")

    def _run(self, conn, pages):
        _event(conn, "e1", "Direct From Sweden: The Music of ABBA", self.LISTING,
               venue_name="Harrah’s Lake Tahoe")
        _performer(conn, name="Direct From Sweden", events=("e1",))
        web = Web(pages={self.LISTING: page("Direct From Sweden at Harrah's - Visit Lake Tahoe"),
                         **pages},
                  hits=[{"url": self.SITE, "title": "Direct from Sweden - The Music of ABBA"}])
        return pr.run_batch(conn, TOWN, fetch=web.fetch, search=web.search)

    def test_tour_dates_listing_the_booked_venue_verify(self, conn):
        tour = ("<li>October 31, 2026 — Direct from Sweden – The Music of ABBA at "
                "Harrah’s Lake Tahoe<br>South Shore Room at Harrah's, Lake Tahoe, NV, USA</li>")
        [result] = self._run(conn, {self.SITE: self._home(tour)})
        assert result["status"] == "verified"
        row = _row(conn)
        assert row["name"] == "Direct From Sweden"
        assert row["website"] == self.SITE
        assert row["description"] == self.DESCRIPTION
        assert row["enrichment_status"] == "enriched"
        assert _research(conn)["venues"] == ["Harrah’s Lake Tahoe"]

    def test_tour_dates_without_the_town_go_to_review(self, conn):
        tour = "<li>October 27, 2026 — at Yoshi’s<br>Yoshi’s, Oakland, CA, USA</li>"
        [result] = self._run(conn, {self.SITE: self._home(tour)})
        assert result["status"] == "probable"
        row = _row(conn)
        assert row["enrichment_status"] == "needs-review"
        assert row["name"] == "Direct From Sweden" and row["website"] is None

    def test_listing_page_linking_the_own_site_is_a_bridge(self, conn):
        _event(conn, "e1", "Direct From Sweden: The Music of ABBA", self.LISTING,
               venue_name="Harrah’s Lake Tahoe")
        _performer(conn, name="Direct From Sweden", events=("e1",))
        web = Web(pages={
            self.LISTING: page("Direct From Sweden at Harrah's - Visit Lake Tahoe",
                               body=f'<a href="{self.SITE}">Visit Website</a>'),
            self.SITE: self._home(""),
        })
        f = pr.investigate(conn, "performer", "p1", TOWN, Budget(6), fetch=web.fetch,
                           search=web.search)
        assert f.status == "verified" and f.payload["website"] == self.SITE
        assert web.searched == []


class TestDefaultFetch:
    def test_firecrawl_fallback_keeps_the_head(self, monkeypatch):
        """Firecrawl's `html` format strips <head> — the title and meta the
        ladder judges by — so profile reads ask for the raw HTML."""
        import engine.research as research
        calls = []
        monkeypatch.setattr(research, "fetch_html", lambda url: (None, None))
        monkeypatch.setattr(research, "firecrawl_fetch",
                            lambda url, raw=False: calls.append(raw) or ("<html/>", url))
        assert pr._default_fetch("https://x.example/") == ("<html/>", "https://x.example/")
        assert calls == [True]
