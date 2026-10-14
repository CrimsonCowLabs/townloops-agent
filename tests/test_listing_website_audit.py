"""operator-audit-listing-websites: entities whose `website` is a listing page.

Driven through engine.listing_website_audit against FakeAPI + a fresh local
DB. The audit is read-only: it may only LIST/GET, and writes nothing local.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.fake_townloops import FakeAPI
from engine.townloops_api import TownloopsError
from engine.listing_website_audit import audit_listing_websites, find_listing_websites

RESTAURANTJI = "https://www.restaurantji.com/ca/south-lake-tahoe/cascade-kitchens-food-hall-and-bar-/"
CASCADE = "Cascade Kitchens Food Hall & Bar"


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


def _local(conn, table, uid, name, website, *, slug=None, extra=None, source_kind="mined"):
    conn.execute(
        f"INSERT INTO {table} (entity_uid, source, source_kind, name, website, townloops_slug,"
        " extra_json, first_seen_at, last_seen_at, content_hash, last_modified_at)"
        " VALUES (?, 'seed', ?, ?, ?, ?, ?, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z',"
        " ?, '2026-10-01T00:00:00Z')",
        (uid, source_kind, name, website, slug, json.dumps(extra) if extra else None, uid))


def _live(slug, name, website, id_=1):
    return {"slug": slug, "id": id_, "name": name, "website": website}


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_restaurantji_business_local_and_live_is_both(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-kitchens",
           extra={"live_id": 7})
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    rows = find_listing_websites(conn, api).rows
    assert rows == [{
        "kind": "business", "name": CASCADE, "side": "both", "url": RESTAURANTJI,
        "entity_uid": "b1", "live_slug": "cascade-kitchens", "live_id": 7,
        "local_website": RESTAURANTJI, "live_website": RESTAURANTJI,
        "live_unread": False, "duplicate_of": None,
    }]


def test_own_site_website_is_not_flagged(conn):
    _local(conn, "businesses", "b1", "Riva Grill", "https://www.rivagrill.com/",
           slug="riva-grill")
    _local(conn, "venues", "v1", "Lakeview Commons", "https://cityofslt.us/lakeview",
           slug="lakeview-commons")
    api = FakeAPI(venues=[_live("lakeview-commons", "Lakeview Commons", "https://cityofslt.us/lakeview")],
                  entities={"businesses": [_live("riva-grill", "Riva Grill", "https://www.rivagrill.com/")],
                            "communities": [_live("rotary", "Rotary", "https://slt-rotary.org")]})
    assert find_listing_websites(conn, api).rows == []


def test_local_only_listing_is_local(conn):
    # Not shipped yet, and shipped but live holds the own site.
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI)
    _local(conn, "venues", "v1", "Riva Grill", "https://www.yelp.com/biz/riva-grill",
           slug="riva-grill")
    api = FakeAPI(venues=[_live("riva-grill", "Riva Grill", "https://www.rivagrill.com/", 3)])
    rows = _by_name(find_listing_websites(conn, api).rows)
    assert rows[CASCADE]["side"] == "local"
    assert rows[CASCADE]["live_slug"] is None and rows[CASCADE]["live_website"] is None
    riva = rows["Riva Grill"]
    assert (riva["side"], riva["url"]) == ("local", "https://www.yelp.com/biz/riva-grill")
    assert (riva["live_slug"], riva["live_id"]) == ("riva-grill", 3)
    assert riva["live_website"] == "https://www.rivagrill.com/"


def test_live_only_listing_is_live(conn):
    # No local row at all; and a local row whose website is blank.
    _local(conn, "communities", "c1", "Tahoe Quilters", None, slug="tahoe-quilters")
    api = FakeAPI(entities={
        "businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)],
        "communities": [_live("tahoe-quilters", "Tahoe Quilters",
                              "https://www.facebook.com/events/12345", 9)],
    })
    rows = _by_name(find_listing_websites(conn, api).rows)
    assert rows[CASCADE] == {
        "kind": "business", "name": CASCADE, "side": "live", "url": RESTAURANTJI,
        "entity_uid": None, "live_slug": "cascade-kitchens", "live_id": 7,
        "local_website": None, "live_website": RESTAURANTJI,
        "live_unread": False, "duplicate_of": None,
    }
    quilters = rows["Tahoe Quilters"]
    assert (quilters["kind"], quilters["side"], quilters["entity_uid"]) == ("community", "live", "c1")


def test_different_listing_urls_each_side_is_both_with_both_urls(conn):
    _local(conn, "venues", "v1", "Riva Grill", "https://www.yelp.com/biz/riva-grill", slug="riva-grill")
    api = FakeAPI(venues=[_live("riva-grill", "Riva Grill",
                                "https://www.tripadvisor.com/Restaurant-riva", 3)])
    [row] = find_listing_websites(conn, api).rows
    assert row["side"] == "both"
    assert row["url"] == "https://www.tripadvisor.com/Restaurant-riva"     # live's first
    assert row["local_website"] == "https://www.yelp.com/biz/riva-grill"


def test_matches_live_by_live_id_when_slug_was_renamed(conn):
    _local(conn, "businesses", "b1", CASCADE, "https://cascadekitchens.com",
           slug="cascade-old", extra={"live_id": 7})
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    [row] = find_listing_websites(conn, api).rows
    assert (row["side"], row["entity_uid"], row["live_slug"]) == ("live", "b1", "cascade-kitchens")


def test_follows_a_merged_slug_to_its_survivor(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-dupe")
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    api.merged_into[("businesses", "cascade-dupe")] = "cascade-kitchens"
    rows = find_listing_websites(conn, api).rows
    assert [(r["side"], r["entity_uid"], r["live_slug"]) for r in rows] == \
        [("both", "b1", "cascade-kitchens")]


def test_gone_live_row_leaves_the_local_side(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-gone")
    [row] = find_listing_websites(conn, FakeAPI()).rows
    assert (row["side"], row["live_slug"]) == ("local", None)


def test_covers_three_kinds_not_performers(conn):
    _local(conn, "venues", "v1", "V", "https://maps.google.com/?q=v")
    _local(conn, "businesses", "b1", "B", "https://www.yelp.com/biz/b")
    _local(conn, "communities", "c1", "C", "https://www.eventbrite.com/o/c")
    _local(conn, "performers", "p1", "P", "https://www.bandsintown.com/a/p")
    rows = find_listing_websites(conn, FakeAPI()).rows
    assert [(r["kind"], r["name"]) for r in rows] == \
        [("venue", "V"), ("business", "B"), ("community", "C")]


def test_read_only(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-dupe")
    _local(conn, "venues", "v1", "Riva Grill", "https://www.yelp.com/biz/riva-grill", slug="riva-grill")
    api = FakeAPI(venues=[_live("riva-grill", "Riva Grill", "https://www.yelp.com/biz/riva-grill")],
                  entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    api.merged_into[("businesses", "cascade-dupe")] = "cascade-kitchens"
    before = conn.total_changes
    audit_listing_websites(conn, api)
    assert conn.total_changes == before
    assert conn.execute("SELECT townloops_slug FROM businesses").fetchone()[0] == "cascade-dupe"
    assert {c[0] for c in api.calls} <= {"LIST", "GET"}


def test_audit_counts_and_limit(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-kitchens")
    _local(conn, "venues", "v1", "V", "https://www.yelp.com/biz/v")
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)],
                            "communities": [_live("c", "C", "https://www.yelp.com/biz/c", 2)]})
    out = audit_listing_websites(conn, api, limit=2)
    assert out["total"] == 3
    assert out["counts"] == {"local": 1, "live": 1, "both": 1}
    assert out["by_kind"] == {"venue": 1, "business": 1, "community": 1}
    assert len(out["rows"]) == 2


class Flaky(FakeAPI):
    """Lists fine; every GET fails with something other than a 404."""

    def get(self, resource, slug):
        self.calls.append(("GET", resource, slug))
        raise TownloopsError(503, "unavailable", "try later")


def test_unreadable_live_slug_is_marked_unread_and_returned(conn):
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-gone")
    found = find_listing_websites(conn, Flaky())
    [row] = found.rows
    assert (row["side"], row["entity_uid"], row["live_unread"]) == ("local", "b1", True)
    assert row["live_slug"] is None
    assert [(u["kind"], u["entity_uid"], u["live_slug"]) for u in found.unverified] == \
        [("business", "b1", "cascade-gone")]
    out = audit_listing_websites(conn, Flaky())
    assert out["unverified"] == found.unverified
    assert out["rows"][0]["live_unread"] is True


def test_pairs_from_the_listing_before_any_get(conn):
    # Slug renamed live but live_id known; and a slug in the listing.
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-old", extra={"live_id": 7})
    _local(conn, "venues", "v1", "Riva Grill", "https://www.yelp.com/biz/riva", slug="riva-grill")
    api = Flaky(venues=[_live("riva-grill", "Riva Grill", "https://www.yelp.com/biz/riva", 3)],
                entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    found = find_listing_websites(conn, api)
    assert [(r["kind"], r["side"], r["entity_uid"], r["live_slug"]) for r in found.rows] == \
        [("venue", "both", "v1", "riva-grill"), ("business", "both", "b1", "cascade-kitchens")]
    assert found.unverified == []
    assert not [c for c in api.calls if c[0] == "GET"]


def test_two_local_rows_on_one_live_row_count_the_live_side_once(conn):
    # b2's live row was merged into b1's: both now resolve to cascade-kitchens.
    _local(conn, "businesses", "b2", CASCADE, "https://www.yelp.com/biz/cascade", slug="cascade-dupe")
    _local(conn, "businesses", "b1", CASCADE, RESTAURANTJI, slug="cascade-kitchens")
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    api.merged_into[("businesses", "cascade-dupe")] = "cascade-kitchens"
    out = audit_listing_websites(conn, api)
    rows = {r["entity_uid"]: r for r in out["rows"]}
    assert (rows["b1"]["side"], rows["b1"]["duplicate_of"]) == ("both", None)
    assert (rows["b2"]["side"], rows["b2"]["duplicate_of"]) == ("local", "b1")
    assert rows["b2"]["live_slug"] == "cascade-kitchens"
    assert out["counts"] == {"local": 1, "live": 0, "both": 1}


def test_duplicate_with_own_site_adds_no_row(conn):
    _local(conn, "businesses", "b1", CASCADE, "https://cascadekitchens.com", slug="cascade-kitchens")
    _local(conn, "businesses", "b2", CASCADE, "https://cascadekitchens.com",
           slug="cascade-dupe", extra={"live_id": 7})
    api = FakeAPI(entities={"businesses": [_live("cascade-kitchens", CASCADE, RESTAURANTJI, 7)]})
    out = audit_listing_websites(conn, api)
    assert [(r["entity_uid"], r["side"]) for r in out["rows"]] == [("b1", "live")]
    assert out["total"] == 1
