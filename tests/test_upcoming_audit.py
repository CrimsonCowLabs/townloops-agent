"""operator-audit-upcoming: next-N-days gaps, sparse metadata, venue images.

Driven through engine.upcoming_audit.audit_upcoming against FakeAPI + a
fresh local DB. The audit is read-only: it may only LIST/GET.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from tests.fake_townloops import FakeAPI
from engine.upcoming_audit import audit_upcoming

# 2026-10-08 09:00 Pacific — town-local "today" is Oct 8.
NOW = datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc)

VENUES = [
    {"slug": "riva-grill", "id": 1, "name": "Riva Grill",
     "image_path": "https://riva.example/hero.jpg", "image_alt": "Riva deck"},
    {"slug": "lakeview-commons", "id": 2, "name": "Lakeview Commons",
     "image_path": None, "image_alt": None},
    {"slug": "social-house", "id": 3, "name": "Social House",
     "image_path": "", "image_alt": None},
]


def _ev(slug, start_at, **kw):
    row = {
        "slug": slug, "title": slug.replace("-", " ").title(),
        "start_at": start_at, "end_at": None,
        "description": "A real description of the event.",
        "source_url": f"https://src.example/{slug}",
        "category_slugs": ["live-music"], "tags": [],
        "venue_id": 1, "venue_slug": "riva-grill", "status": "published",
    }
    row.update(kw)
    return row


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


def _local(conn, uid, date_utc, **kw):
    cols = {"event_uid": uid, "source": "seed", "source_kind": "seed", "name": uid, "title": uid,
            "date": date_utc, "date_utc": date_utc, "date_day": date_utc[:10], "state": "metadata-extracted",
            "approval_status": "pending", "first_seen_at": "2026-10-01T00:00:00Z",
            "last_seen_at": "2026-10-01T00:00:00Z", "last_modified_at": "2026-10-01T00:00:00Z",
            "content_hash": uid}
    cols.update(kw)
    conn.execute(
        f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )


def test_read_only(conn):
    api = FakeAPI([_ev("jazz", "2026-10-09T03:00:00Z")], VENUES)
    audit_upcoming(conn, api, days=7, now=NOW)
    assert {c[0] for c in api.calls} <= {"LIST", "GET"}


def test_window_is_town_local_days(conn):
    # 2026-10-15T05:30Z is Oct 14 22:30 Pacific — the window's last local
    # day for days=7 — and must count, though its UTC date is past it.
    late = _ev("late-show", "2026-10-15T05:30:00Z")
    beyond = _ev("next-week", "2026-10-15T08:00:00Z")   # Oct 15 local
    past = _ev("yesterday", "2026-10-08T05:00:00Z")      # Oct 7 local
    api = FakeAPI([late, beyond, past], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    assert out["window"] == {"from": "2026-10-08", "to": "2026-10-14", "days": 7,
                             "tz": "America/Los_Angeles"}
    assert out["live_events"] == 1


def test_days_without_live_events_are_gaps(conn):
    api = FakeAPI([_ev("a", "2026-10-08T19:00:00Z"), _ev("b", "2026-10-10T19:00:00Z")], VENUES)
    out = audit_upcoming(conn, api, days=4, now=NOW)
    assert out["gaps"]["empty_days"] == ["2026-10-09", "2026-10-11"]


def test_local_events_not_yet_live_are_gaps(conn):
    _local(conn, "pending-1", "2026-10-09T02:00:00Z")
    _local(conn, "approved-1", "2026-10-10T02:00:00Z", approval_status="approved",
           state="venue-derived")
    _local(conn, "stuck-1", "2026-10-10T02:00:00Z", approval_status="approved",
           state="stuck-venue-derived")
    _local(conn, "shipped-1", "2026-10-10T02:00:00Z", approval_status="approved",
           state="shipped", townloops_slug="shipped-1")
    _local(conn, "declined-1", "2026-10-10T02:00:00Z", approval_status="declined")
    _local(conn, "far-1", "2026-11-30T02:00:00Z")
    out = audit_upcoming(conn, FakeAPI([], VENUES), days=7, now=NOW)
    not_live = out["gaps"]["not_live"]
    assert not_live["counts"] == {"pending_review": 1, "in_pipeline": 1, "stuck": 1}
    assert [e["event_uid"] for e in not_live["events"]] == ["pending-1", "approved-1", "stuck-1"]
    assert not_live["events"][2]["reason"] == "stuck"


def test_sparse_metadata_lists_missing_fields(conn):
    api = FakeAPI([
        _ev("complete", "2026-10-09T03:00:00Z"),
        _ev("bare", "2026-10-09T04:00:00Z", description="  ", source_url=None,
            category_slugs=[], venue_id=None, venue_slug=None),
        _ev("no-venue", "2026-10-09T05:00:00Z", venue_id=None, venue_slug=None),
    ], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    sparse = out["sparse_metadata"]
    assert sparse["counts"] == {"description": 1, "venue": 2, "source_url": 1, "categories": 1}
    by_slug = {e["slug"]: e["missing"] for e in sparse["events"]}
    assert by_slug == {"bare": ["description", "venue", "source_url", "categories"],
                       "no-venue": ["venue"]}


def test_venues_missing_images_ranked_by_window_events(conn):
    api = FakeAPI([
        _ev("a", "2026-10-09T03:00:00Z", venue_id=2, venue_slug="lakeview-commons"),
        _ev("b", "2026-10-10T03:00:00Z", venue_id=2, venue_slug="lakeview-commons"),
        _ev("c", "2026-10-11T03:00:00Z", venue_id=3, venue_slug="social-house"),
        _ev("d", "2026-10-11T03:00:00Z"),  # riva-grill has an image
    ], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    imgs = out["venues_missing_images"]
    assert [(v["venue_slug"], v["events_in_window"], v["next_event"]) for v in imgs["venues"]] == [
        ("lakeview-commons", 2, "2026-10-08"), ("social-house", 1, "2026-10-10")]
    assert imgs["total"] == 2


def test_venue_not_found_live_is_reported_not_raised(conn):
    api = FakeAPI([_ev("a", "2026-10-09T03:00:00Z", venue_id=9, venue_slug="ghost-venue")], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    assert out["venues_missing_images"]["unverified"] == ["ghost-venue"]


def test_limit_caps_lists_not_counts(conn):
    api = FakeAPI([_ev(f"e{i}", "2026-10-09T03:00:00Z", description="") for i in range(5)], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW, limit=2)
    assert out["sparse_metadata"]["counts"]["description"] == 5
    assert out["sparse_metadata"]["total"] == 5
    assert len(out["sparse_metadata"]["events"]) == 2


# --- Series (issue #3): coverage and findings count a Series once ---------

def _series(slug, start_at, series_slug, **kw):
    return _ev(slug, start_at, series_slug=series_slug, **kw)


def test_coverage_counts_a_series_once_and_flags_series_only_days(conn):
    api = FakeAPI([
        # Oct 8: a Series with two same-day occurrences, nothing else
        _series("music-1", "2026-10-08T19:00:00Z", "daily-live-music"),
        _series("music-2", "2026-10-08T23:00:00Z", "daily-live-music"),
        # Oct 9: the Series plus a one-off
        _series("music-3", "2026-10-09T19:00:00Z", "daily-live-music"),
        _ev("book-talk", "2026-10-09T20:00:00Z"),
    ], VENUES)
    out = audit_upcoming(conn, api, days=3, now=NOW)
    cov = out["coverage"]
    assert cov["days"] == [
        {"date": "2026-10-08", "groups": 1, "series": 1, "singletons": 0},
        {"date": "2026-10-09", "groups": 2, "series": 1, "singletons": 1},
        {"date": "2026-10-10", "groups": 0, "series": 0, "singletons": 0},
    ]
    assert cov["series_only_days"] == ["2026-10-08"]
    assert out["live_events"] == 4 and out["live_groups"] == 2


def test_sparse_metadata_is_one_finding_per_series(conn):
    api = FakeAPI([
        *[_series(f"trivia-{i}", f"2026-10-{9 + i:02d}T03:00:00Z", "trivia-thursday", description="")
          for i in range(4)],
        _ev("one-off", "2026-10-09T05:00:00Z", description=""),
    ], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    sparse = out["sparse_metadata"]
    assert sparse["counts"]["description"] == 2 and sparse["total"] == 2
    trivia = next(e for e in sparse["events"] if e.get("series_slug") == "trivia-thursday")
    assert trivia["slug"] == "trivia-0" and trivia["occurrences"] == 4
    assert trivia["missing"] == ["description"]


def test_venue_missing_image_across_a_series_is_one_finding(conn):
    api = FakeAPI([_series(f"trivia-{i}", f"2026-10-{9 + i:02d}T03:00:00Z", "trivia-thursday",
                           venue_id=2, venue_slug="lakeview-commons") for i in range(4)], VENUES)
    out = audit_upcoming(conn, api, days=7, now=NOW)
    imgs = out["venues_missing_images"]
    assert imgs["total"] == 1
    [v] = imgs["venues"]
    assert v["venue_slug"] == "lakeview-commons" and v["events_in_window"] == 4 and v["groups_in_window"] == 1
