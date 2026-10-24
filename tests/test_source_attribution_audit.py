"""operator-audit-source-attribution: live events whose source_url is
another event's page (#47).

SLT's `line-dancing-2026-10-08` went live before the #45 guards: a dance
class carrying a half marathon's detail URL, date and time. The sweep reads
each live event's source page and judges it with #45's own predicate
(engine/event_provenance.page_disagreements), so the two can't drift.

Driven through engine.source_attribution_audit against FakeAPI + a fresh
local DB + an injected fetch. Read-only: only LIST/GET, nothing local.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.fake_townloops import FakeAPI
from engine import event_provenance
from engine.source_attribution_audit import audit_source_attribution, find_cross_wired


FIXTURES = Path(__file__).parent / "fixtures" / "provenance"
SEED = "Tahoe Chamber"
SEED_URL = "https://business.tahoechamber.org/events/"
MARATHON_URL = ("https://business.tahoechamber.org/events/details/"
                "lakeside-marathon-nevada-half-marathon-10-09-2026-17355")
DANCING_URL = "https://business.tahoechamber.org/events/details/line-dancing-10-07-2026-17301"


def page(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text()


def _ev(slug, title, start_at, source_url, **kw):
    return {"slug": slug, "title": title, "start_at": start_at, "end_at": None,
            "source_url": source_url, "venue_name": "Cascade Kitchens",
            "venue_slug": "cascade-kitchens", "status": "published", **kw}


# The live row: Line Dancing at Cascade Kitchens, on the marathon's URL and
# its Fri Oct 9 06:30 PDT start.
CROSS_WIRED = _ev("line-dancing-2026-10-08", "Line Dancing", "2026-10-09T13:30:00Z", MARATHON_URL)
# A sibling the page vouches for: Wed Oct 7 19:30 PDT.
DANCING = _ev("line-dancing-2026-10-07", "Line Dancing", "2026-10-08T02:30:00Z", DANCING_URL)


class Fetch:
    def __init__(self, pages: dict):
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        got = self.pages.get(url)
        if isinstance(got, Exception):
            raise got
        return (True, got, "") if got is not None else (False, "", "firecrawl http 500")


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds(name, url, kind, status, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'aggregator', 'approved', 'now', 'now')", (SEED, SEED_URL))
    return fresh_db


def _local(conn, uid, slug, title, date_utc, source_url, extra=None):
    cols = {"event_uid": uid, "source": SEED, "source_kind": "seed", "name": title, "title": title,
            "date": date_utc, "date_utc": date_utc, "date_day": date_utc[:10], "state": "shipped",
            "approval_status": "approved", "townloops_slug": slug, "source_url": source_url,
            "extra_json": json.dumps(extra) if extra else None,
            "first_seen_at": "2026-10-01T00:00:00Z", "last_seen_at": "2026-10-01T00:00:00Z",
            "last_modified_at": "2026-10-01T00:00:00Z", "content_hash": uid}
    conn.execute(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))


def _sweep(conn, events, pages, **kw):
    fetch = Fetch(pages)
    return find_cross_wired(conn, FakeAPI(events), fetch=fetch, **kw), fetch


def test_line_dancing_on_the_marathons_page_is_reported(conn):
    found, _ = _sweep(conn, [CROSS_WIRED], {MARATHON_URL: page("marathon_detail")})
    assert found.rows == [{
        "slug": "line-dancing-2026-10-08", "title": "Line Dancing",
        "start_at": "2026-10-09T13:30:00Z", "date": "2026-10-09", "venue": "Cascade Kitchens",
        "status": "published", "source_url": MARATHON_URL,
        "disagreed": ['detail page does not name "Line Dancing" '
                      '(it reads "Lakeside Marathon - Nevada Half Marathon")'],
        "found_by": ["detail-page"], "event_uid": None, "seed": None,
    }]


def test_an_event_its_page_vouches_for_is_not_reported(conn):
    found, _ = _sweep(conn, [DANCING], {DANCING_URL: page("line_dancing_detail")})
    assert found.rows == [] and found.unverified == []
    assert found.checked == 1


def test_a_page_giving_another_date_is_reported(conn):
    moved = {**DANCING, "start_at": "2026-10-15T02:30:00Z"}   # Wed Oct 14 local
    found, _ = _sweep(conn, [moved], {DANCING_URL: page("line_dancing_detail")})
    [r] = found.rows
    assert r["disagreed"] == ["detail page does not give 2026-10-14 (it gives Oct 7)"]


def test_judged_by_the_same_predicate_as_read_detail(conn):
    # The audit adds no comparison of its own: whatever #45 says of the page.
    event = {"title": "Line Dancing", "date": CROSS_WIRED["start_at"]}
    found, _ = _sweep(conn, [CROSS_WIRED], {MARATHON_URL: page("marathon_detail")})
    assert found.rows[0]["disagreed"] == \
        event_provenance.page_disagreements(event, page("marathon_detail"))


def test_live_occurrences_sharing_a_series_page_are_not_held_to_its_one_date(conn):
    later = {**DANCING, "slug": "line-dancing-2026-10-14", "start_at": "2026-10-15T02:30:00Z"}
    found, fetch = _sweep(conn, [DANCING, later], {DANCING_URL: page("line_dancing_detail")})
    assert found.rows == []
    assert fetch.urls == [DANCING_URL]   # each distinct page fetched once


def test_paired_with_its_local_row_by_slug(conn):
    _local(conn, "e1", CROSS_WIRED["slug"], "Line Dancing", "2026-10-09T13:30:00+00:00",
           MARATHON_URL)
    found, _ = _sweep(conn, [CROSS_WIRED], {MARATHON_URL: page("marathon_detail")})
    assert (found.rows[0]["event_uid"], found.rows[0]["seed"]) == ("e1", SEED)


def test_judges_the_live_title_not_the_local_one(conn):
    # Live is what's wrong: a local row that says marathon doesn't excuse it.
    _local(conn, "e1", CROSS_WIRED["slug"], "Lakeside Marathon - Nevada Half Marathon",
           "2026-10-09T13:30:00+00:00", MARATHON_URL)
    found, _ = _sweep(conn, [CROSS_WIRED], {MARATHON_URL: page("marathon_detail")})
    assert found.rows[0]["title"] == "Line Dancing"
    assert "does not name" in found.rows[0]["disagreed"][0]


def test_no_source_url_and_listing_pages_are_skipped_unfetched(conn):
    bare = _ev("mixer", "Chamber Mixer", "2026-10-14T00:30:00Z", None)
    listed = _ev("art-walk", "Fall Art Walk", "2026-10-10T18:00:00Z", SEED_URL)
    found, fetch = _sweep(conn, [bare, listed], {SEED_URL: page("marathon_detail")})
    assert found.rows == [] and fetch.urls == []
    assert found.skipped == {"no_source_url": 1, "listing_page": 1}


def test_failed_or_blocked_fetch_is_unverified_never_a_mismatch(conn):
    other = _ev("art-walk", "Fall Art Walk", "2026-10-10T18:00:00Z",
                "https://business.tahoechamber.org/events/details/fall-art-walk")
    third = _ev("trivia", "Trivia", "2026-10-11T02:00:00Z", "https://trivia.example/night")
    found, _ = _sweep(conn, [CROSS_WIRED, other, third], {
        MARATHON_URL: "Attention Required! | Cloudflare\nChecking your browser",
        "https://trivia.example/night": TimeoutError("token bucket empty"),
    })
    assert found.rows == []
    assert [(u["source_url"], u["slugs"]) for u in found.unverified] == [
        (MARATHON_URL, ["line-dancing-2026-10-08"]),
        ("https://business.tahoechamber.org/events/details/fall-art-walk", ["art-walk"]),
        ("https://trivia.example/night", ["trivia"]),
    ]
    assert all(u["error"] for u in found.unverified)


def test_fetch_cap_leaves_the_rest_unverified(conn):
    found, fetch = _sweep(conn, [DANCING, CROSS_WIRED],
                          {DANCING_URL: page("line_dancing_detail"),
                           MARATHON_URL: page("marathon_detail")}, max_fetches=1)
    assert fetch.urls == [DANCING_URL]
    assert found.rows == []
    assert found.unverified == [{"source_url": MARATHON_URL, "slugs": [CROSS_WIRED["slug"]],
                                 "error": "not fetched: fetch cap (1) reached"}]


def test_reuses_a_cached_detail_page_instead_of_fetching(conn, tmp_state_dir, monkeypatch):
    from engine import event_detail
    monkeypatch.setattr("engine.derive_helpers._DB_PATH", tmp_state_dir / "townloops.db")
    _local(conn, "e1", CROSS_WIRED["slug"], "Line Dancing", "2026-10-09T13:30:00+00:00",
           MARATHON_URL)
    conn.execute("UPDATE events SET detail_read_status = 'read', detail_read_url = ? "
                 "WHERE event_uid = 'e1'", (MARATHON_URL,))
    event_detail.cache_page("e1", page("marathon_detail"))
    found, fetch = _sweep(conn, [CROSS_WIRED], {})
    assert fetch.urls == []
    assert "does not name" in found.rows[0]["disagreed"][0]


def test_a_local_live_mismatch_flag_is_reported_with_the_live_row(conn):
    reason = (f'listing link {MARATHON_URL} is in the card of '
              '"Lakeside Marathon - Nevada Half Marathon", not this event\'s')
    _local(conn, "e1", CROSS_WIRED["slug"], "Line Dancing", "2020-10-09T13:30:00+00:00",
           MARATHON_URL, extra={"provenance": {"status": "live-mismatch", "url": MARATHON_URL,
                                               "reason": reason}})
    found, _ = _sweep(conn, [CROSS_WIRED], {}, max_fetches=0)   # page unread: the flag stands
    [r] = found.rows
    assert r["disagreed"] == [reason] and r["found_by"] == ["listing"]
    found, _ = _sweep(conn, [CROSS_WIRED], {MARATHON_URL: page("marathon_detail")})
    assert found.rows[0]["found_by"] == ["detail-page", "listing"]
    assert len(found.rows[0]["disagreed"]) == 2


def test_a_flag_live_no_longer_carries_is_dropped(conn):
    _local(conn, "e1", DANCING["slug"], "Line Dancing", "2026-10-08T02:30:00+00:00", DANCING_URL,
           extra={"provenance": {"status": "live-mismatch", "url": MARATHON_URL, "reason": "x"}})
    found, _ = _sweep(conn, [DANCING], {DANCING_URL: page("line_dancing_detail")})
    assert found.rows == [] and found.local_flags == []


def test_a_flag_whose_live_row_is_gone_is_listed_apart(conn):
    _local(conn, "e1", "gone-slug", "Line Dancing", "2026-10-09T13:30:00+00:00", MARATHON_URL,
           extra={"provenance": {"status": "live-mismatch", "url": MARATHON_URL, "reason": "x"}})
    found, _ = _sweep(conn, [], {})
    assert [(f["event_uid"], f["townloops_slug"], f["reason"]) for f in found.local_flags] == \
        [("e1", "gone-slug", "x")]


def test_since_limits_the_sweep_to_later_events(conn):
    api = FakeAPI([DANCING, CROSS_WIRED])
    fetch = Fetch({MARATHON_URL: page("marathon_detail")})
    found = find_cross_wired(conn, api, fetch=fetch,
                             since=datetime(2026, 10, 9, 7, 0, tzinfo=timezone.utc))
    assert fetch.urls == [MARATHON_URL]
    assert [r["slug"] for r in found.rows] == [CROSS_WIRED["slug"]]
    assert api.calls[0][2]["from"] == "2026-10-09T07:00:00Z"


def test_read_only(conn):
    _local(conn, "e1", CROSS_WIRED["slug"], "Line Dancing", "2026-10-09T13:30:00+00:00",
           MARATHON_URL, extra={"provenance": {"status": "live-mismatch", "url": MARATHON_URL,
                                               "reason": "x"}})
    before = list(conn.iterdump())
    api = FakeAPI([CROSS_WIRED, DANCING])
    out = audit_source_attribution(conn, api, fetch=Fetch({
        MARATHON_URL: page("marathon_detail"), DANCING_URL: page("line_dancing_detail")}))
    assert out["total"] == 1
    assert {c[0] for c in api.calls} <= {"LIST", "GET"}
    assert list(conn.iterdump()) == before
    assert api.events[CROSS_WIRED["slug"]]["source_url"] == MARATHON_URL


def test_audit_counts_and_limit(conn):
    a = {**CROSS_WIRED, "slug": "a", "start_at": "2026-10-09T13:30:00Z"}
    b = {**CROSS_WIRED, "slug": "b", "title": "Salsa Night"}
    out = audit_source_attribution(conn, FakeAPI([a, b, DANCING]), limit=1,
                                   fetch=Fetch({MARATHON_URL: page("marathon_detail"),
                                                DANCING_URL: page("line_dancing_detail")}))
    assert (out["live_events"], out["checked"], out["pages_fetched"], out["total"]) == (3, 3, 2, 2)
    assert [r["slug"] for r in out["rows"]] == ["a"]
