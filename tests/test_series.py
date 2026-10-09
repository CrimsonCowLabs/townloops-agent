"""engine/series: the agent's half of TownLoops Series (ADR 0007) — the
title key TownLoops auto-links by, the title an occurrence ships under, the
Series-shared fields it may take from a sibling, and the candidates report.
Pure logic; the ship/reconcile/sync paths that use it are tested through
their entry points (test_series_ship.py and friends)."""

from __future__ import annotations

import pytest
from dateutil import tz as _tz

from engine import series


# Expected values are python-slugify 8.0.4 (`slugify(title, lowercase=True,
# max_length=180)` — TownLoops `app/services/slugify.py`), whose output
# `series_key` must reproduce. The first five mirror TownLoops'
# tests/test_event_series.py; the rest pin the punctuation, apostrophe,
# entity, number and transliteration rules.
PARITY = [
    ("Board Meeting", "board-meeting"),
    ("Farmers Market", "farmers-market"),
    ("Town Council Meeting", "town-council-meeting"),
    ("town council meeting", "town-council-meeting"),
    ("Jazz Night", "jazz-night"),
    ("Trivia Thursday!", "trivia-thursday"),
    ("Trivia Thursday – Week 3", "trivia-thursday-week-3"),
    ("Farmers Market — Week 3", "farmers-market-week-3"),
    ("Jon's Jam", "jon-s-jam"),
    ("Jon’s Jam", "jons-jam"),
    ("Farmers' Market", "farmers-market"),
    ("Café Olé", "cafe-ole"),
    ("Mañana Tacos", "manana-tacos"),
    ("Straße Fest", "strasse-fest"),
    ("Ærø Øl", "aero-ol"),
    ("Rock–Roll", "rock-roll"),
    ("“Quoted” Night", "quoted-night"),
    ("Fish &amp; Chips", "fish-chips"),
    ("1,000 Lakes", "1000-lakes"),
    ("  Board  Meeting  ", "board-meeting"),
    ("Live Music @ The Loft", "live-music-the-loft"),
    ("Trivia Night #42", "trivia-night-42"),
    ("Jazz Night (Indoors)", "jazz-night-indoors"),
    ("", ""),
    ("a" * 200, "a" * 180),
]


class TestSeriesKey:
    @pytest.mark.parametrize("title,key", PARITY)
    def test_matches_townloops(self, title, key):
        assert series_key(title) == key

    def test_agrees_with_python_slugify_when_installed(self):
        slugify = pytest.importorskip("slugify").slugify
        for title, _ in PARITY:
            assert series_key(title) == slugify(title, lowercase=True, max_length=180), title


def series_key(t):
    return series.series_key(t)


# --------------------------------------------------------------------------
# Title decision
# --------------------------------------------------------------------------

TRIVIA = {"id": 1, "slug": "trivia-thursday", "title": "Trivia Thursday", "is_active": True}
JON = {"id": 2, "slug": "jon-sills", "title": "Jon Sills", "is_active": True}
OLD = {"id": 3, "slug": "summer-concerts", "title": "Summer Concerts", "is_active": False}


def occ(slug, venue_id=7, source_url="https://bar.example/events/trivia/2026-10-01", **kw):
    return {"slug": slug, "venue_id": venue_id, "source_url": source_url, **kw}


def decide(title, venue_id=7, venue_name="The Loft", *, source_url=None, performers=(),
           all_series=(TRIVIA, JON, OLD), occurrences=None, cluster_venues=(), seed_url=None):
    occurrences = occurrences if occurrences is not None else {
        "trivia-thursday": [occ("t1"), occ("t2", venue_id=None)],
        "jon-sills": [occ("j1", venue_id=9), occ("j2", venue_id=9)],
        "summer-concerts": [occ("s1")],
    }
    return series.decide_title(
        title, venue_id=venue_id, venue_name=venue_name, source_url=source_url,
        performers=performers, series=list(all_series), occurrences=occurrences,
        cluster_venues=set(cluster_venues), seed_url=seed_url)


class TestDecideTitle:
    def test_variant_at_the_series_venue_takes_the_series_title(self):
        d = decide("Trivia Thursday – Week 3")
        assert (d.title, d.series_slug, d.reason) == ("Trivia Thursday", "trivia-thursday",
                                                      "series-title")

    def test_variant_at_another_venue_keeps_its_title(self):
        d = decide("Trivia Thursday – Week 3", venue_id=8, venue_name="Bar B")
        assert (d.title, d.series_slug, d.reason) == ("Trivia Thursday – Week 3", None,
                                                      "source-title")

    def test_unresolved_venue_never_adopts(self):
        assert decide("Trivia Thursday – Week 3", venue_id=None).series_slug is None

    def test_same_source_branch_at_the_series_venue_adopts_a_reworded_title(self):
        d = decide("Thursday Pub Quiz",
                   source_url="https://bar.example/events/trivia/2026-10-15")
        assert (d.title, d.series_slug) == ("Trivia Thursday", "trivia-thursday")

    def test_seed_listing_page_is_not_source_evidence(self):
        url = "https://bar.example/events/trivia/2026-10-01"
        d = decide("Thursday Pub Quiz", source_url=url, seed_url=url)
        assert d.series_slug is None

    def test_unrelated_title_keeps_its_title(self):
        d = decide("Halloween Costume Party")
        assert (d.title, d.series_slug) == ("Halloween Costume Party", None)

    def test_key_equal_title_auto_links_unchanged(self):
        d = decide("TRIVIA THURSDAY!")
        assert (d.title, d.series_slug, d.reason) == ("TRIVIA THURSDAY!", "trivia-thursday",
                                                      "auto-link")

    def test_archived_series_is_never_a_target(self):
        d = decide("Summer Concerts – Week 2")
        assert (d.title, d.series_slug) == ("Summer Concerts – Week 2", None)
        assert decide("Summer Concerts").series_slug is None

    def test_performer_title_matching_a_series_elsewhere_names_the_venue(self):
        d = decide("Jon Sills", performers=["Jon Sills"])
        assert (d.title, d.series_slug, d.reason) == ("Jon Sills at The Loft", None,
                                                      "performer-at-venue")

    def test_performer_title_at_the_series_own_venue_joins_it(self):
        d = decide("Jon Sills", venue_id=9, venue_name="Divided Sky", performers=["Jon Sills"])
        assert (d.title, d.series_slug) == ("Jon Sills", "jon-sills")

    def test_performer_series_spanning_venues_incl_this_one_names_the_venue(self):
        d = decide("Jon Sills", venue_id=9, venue_name="Divided Sky", performers=["Jon Sills"],
                   occurrences={"jon-sills": [occ("j1", venue_id=9), occ("j2", venue_id=4)]})
        assert (d.title, d.series_slug) == ("Jon Sills at Divided Sky", None)

    def test_performer_title_recurring_at_other_venues_names_the_venue(self):
        d = decide("Kyle Williams", performers=["Kyle Williams"],
                   cluster_venues={"name:bar b", "id:7"})
        assert d.title == "Kyle Williams at The Loft"

    def test_performer_title_seen_only_here_is_kept(self):
        d = decide("Kyle Williams", performers=["Kyle Williams"], cluster_venues={"name:the loft", "id:7"})
        assert d.title == "Kyle Williams"

    def test_venue_suffix_is_never_doubled(self):
        d = decide("Jon Sills at The Loft", performers=["Jon Sills"])
        assert d.title == "Jon Sills at The Loft"
        again = decide(d.title, performers=["Jon Sills"])
        assert again.title == "Jon Sills at The Loft"

    def test_performer_title_with_no_venue_name_is_kept(self):
        assert decide("Jon Sills", venue_id=None, venue_name=None,
                      performers=["Jon Sills"]).title == "Jon Sills"


# --------------------------------------------------------------------------
# Series-shared fields
# --------------------------------------------------------------------------

def live(slug, **kw):
    base = {"slug": slug, "title": "Trivia Thursday", "description": None,
            "category_slugs": [], "tags": [], "accessibility_notes": None,
            "venue_id": None, "start_at": "2026-10-16T02:00:00+00:00", "end_at": None,
            "series_slug": "trivia-thursday"}
    return {**base, **kw}


class TestSharedFill:
    def test_blanks_come_from_the_richest_sibling(self):
        target = live("t3")
        rich = live("t1", description="Six rounds, prizes.", category_slugs=["nightlife"],
                    tags=["trivia", "free"], accessibility_notes="Step-free entrance.",
                    venue_id=7)
        thin = live("t2", description="Trivia.", venue_id=7)
        out = series.shared_fill(target, [thin, rich, target])
        assert out == {"description": "Six rounds, prizes.", "category_slugs": ["nightlife"],
                       "tags": ["trivia"], "accessibility_notes": "Step-free entrance.",
                       "venue_id": 7}

    def test_live_values_are_never_replaced(self):
        target = live("t3", description="Our own words.", tags=["quiz"], venue_id=8)
        sib = live("t1", description="Theirs.", tags=["trivia"], venue_id=7)
        assert series.shared_fill(target, [sib]) == {}

    def test_per_occurrence_fields_never_copy(self):
        target = live("t3")
        sib = live("t1", start_at="2026-10-09T02:00:00+00:00", end_at="2026-10-09T04:00:00+00:00",
                   description="Tickets: $10 — https://bar.example/t\n\nSix rounds.",
                   tags=["free", "cancelled", "trivia"], title="Cancelled: Trivia Thursday")
        other = live("t2", description="Tickets: $10 — https://bar.example/t\n\nSix rounds.")
        out = series.shared_fill(target, [sib, other])
        assert out == {"description": "Six rounds."}
        assert not {"start_at", "end_at", "slug", "title"} & out.keys()

    def test_venue_copied_only_when_every_resolved_sibling_shares_it(self):
        target = live("t3")
        assert series.shared_fill(target, [live("a", venue_id=7), live("b", venue_id=None)]) == {
            "venue_id": 7}
        assert "venue_id" not in series.shared_fill(
            target, [live("a", venue_id=7), live("b", venue_id=8)])
        cancelled = live("c", venue_id=8, tags=["cancelled"])
        assert "venue_id" not in series.shared_fill(target, [live("a", venue_id=7), cancelled])


# --------------------------------------------------------------------------
# Candidates report
# --------------------------------------------------------------------------

TZ = _tz.gettz("America/Los_Angeles")


def ev(slug, title, start, venue_id=7, series_slug=None):
    return {"slug": slug, "title": title, "start_at": start, "venue_id": venue_id,
            "series_slug": series_slug, "status": "published"}


class TestCandidates:
    def test_two_dates_at_one_venue_is_a_candidate(self):
        r = series.candidates(
            [ev("a", "Open Mic", "2026-10-09T03:00:00Z"),
             ev("b", "Open Mic!", "2026-10-16T03:00:00Z", venue_id=None)], TZ)
        [c] = r.clusters
        assert (c.key, c.title, c.dates, c.venue_id) == ("open-mic", "Open Mic", 2, 7)

    def test_same_evening_duplicate_pair_is_not(self):
        r = series.candidates(
            [ev("a", "Fall Fest", "2026-10-10T01:00:00Z"),
             ev("b", "Fall Fest", "2026-10-10T03:00:00Z")], TZ)   # both Oct 9 local
        assert r.clusters == []

    def test_performer_cluster_is_not(self):
        rows = [ev("a", "Jon Sills", "2026-10-09T03:00:00Z", 7),
                ev("b", "Jon Sills", "2026-10-16T03:00:00Z", 7),
                ev("c", "Jon Sills", "2026-10-17T03:00:00Z", 8),
                ev("d", "Jon Sills", "2026-10-18T03:00:00Z", 9)]
        assert series.candidates(rows, TZ).clusters == []
        assert series.candidates(rows[:2], TZ, performers=["Jon Sills"]).clusters == []

    def test_events_already_in_a_series_are_not_clusters(self):
        r = series.candidates(
            [ev("a", "Trivia", "2026-10-09T03:00:00Z", series_slug="trivia"),
             ev("b", "Trivia", "2026-10-16T03:00:00Z", series_slug="trivia")], TZ)
        assert r.clusters == []

    def test_series_drift_is_reported(self):
        r = series.candidates(
            [ev("a", "Trivia", "2026-10-09T03:00:00Z", 7, "trivia"),
             ev("b", "Trivia", "2026-10-16T03:00:00Z", 8, "trivia"),
             ev("c", "Market", "2026-10-10T17:00:00Z", 7, "market"),
             ev("d", "Market", "2026-10-10T18:00:00Z", 7, "market")], TZ)
        assert [(s.slug, s.venues) for s in r.multi_venue] == [("trivia", 2)]
        assert [(s.slug, s.dates) for s in r.duplicate_dates] == [("market", ["2026-10-10"])]
