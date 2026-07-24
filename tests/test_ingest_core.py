"""Tests for engine/ingest_core.py — the shared kernel extracted 2026-07-08/09.

These tests pin the byte-level behavior of the drift-prone primitives.
When SLT eventually adopts ingest_core (see docs/fork-extraction.md), the
same tests apply — hardcoded expected values are the contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urljoin

import pytest


# =====================================================================
# Text utils
# =====================================================================


class TestClean:
    def test_strips_html_tags(self):
        from engine.ingest_core import _clean
        assert _clean("<p>Hello</p>") == "Hello"

    def test_decodes_entities(self):
        from engine.ingest_core import _clean
        assert _clean("Fish &amp; chips") == "Fish & chips"

    def test_decodes_double_encoded_entities(self):
        """Some feeds encode entities twice — the second unescape catches them."""
        from engine.ingest_core import _clean
        assert _clean("Fish &amp;amp; chips") == "Fish & chips"

    def test_collapses_whitespace(self):
        from engine.ingest_core import _clean
        assert _clean("  a\t\tb\n\nc  ") == "a b c"

    def test_truncates_with_ellipsis(self):
        from engine.ingest_core import _clean
        result = _clean("x" * 500, n=10)
        assert result == "xxxxxxxxx…"
        assert len(result) == 10

    def test_empty_input(self):
        from engine.ingest_core import _clean
        assert _clean("") == ""
        assert _clean(None) == ""


class TestNorm:
    def test_lowercases(self):
        from engine.ingest_core import _norm
        assert _norm("HELLO") == "hello"

    def test_collapses_whitespace(self):
        from engine.ingest_core import _norm
        assert _norm("  a\tb ") == "a b"

    def test_empty(self):
        from engine.ingest_core import _norm
        assert _norm("") == ""
        assert _norm(None) == ""

    def test_nfkd_normalization_no_accent_strip(self):
        """_norm uses NFKD but doesn't drop combining marks — dedupe keys must
        agree on this across all callers, so this is a contract test."""
        from engine.ingest_core import _norm
        # Both forms yield the same NFKD normalization.
        assert _norm("café") == _norm("café")


# =====================================================================
# Venue-guessing heuristics
# =====================================================================


class TestVenueFromLocation:
    def test_first_comma_segment(self):
        from engine.ingest_core import _venue_from_location
        assert _venue_from_location("Riva Grill, South Lake Tahoe, CA") == "Riva Grill"

    def test_address_becomes_venue_guess(self):
        """Not ideal but documented — an address survives the split."""
        from engine.ingest_core import _venue_from_location
        assert _venue_from_location("1000 Rufus Allen Blvd, South Lake Tahoe") \
            == "1000 Rufus Allen Blvd"

    def test_no_comma_returned_stripped(self):
        from engine.ingest_core import _venue_from_location
        assert _venue_from_location("Solo Venue") == "Solo Venue"

    def test_empty(self):
        from engine.ingest_core import _venue_from_location
        assert _venue_from_location("") == ""

    def test_length_cap(self):
        from engine.ingest_core import _venue_from_location
        long_name = "A" * 200
        assert len(_venue_from_location(long_name)) == 120


class TestExtractVenueFromDescription:
    def test_at_x_in_town(self):
        from engine.ingest_core import _extract_venue_from_description
        assert _extract_venue_from_description(
            "Live music at Riva Grill in South Lake Tahoe."
        ) == "Riva Grill"

    def test_at_x_comma(self):
        from engine.ingest_core import _extract_venue_from_description
        assert _extract_venue_from_description(
            "Live music at Riva Grill, doors at 7pm."
        ) == "Riva Grill"

    def test_at_the_x(self):
        from engine.ingest_core import _extract_venue_from_description
        assert _extract_venue_from_description(
            "Show at the Loft, tickets $30."
        ) == "Loft"

    def test_ignores_lowercase(self):
        """Addresses like 'at 1000 Rufus Allen' fail the regex by design —
        Townloops venue lookup is exact-name, so a street address would never
        bind and we don't want to try."""
        from engine.ingest_core import _extract_venue_from_description
        assert _extract_venue_from_description(
            "Live music at 1000 rufus allen blvd in slt."
        ) == ""

    def test_empty(self):
        from engine.ingest_core import _extract_venue_from_description
        assert _extract_venue_from_description("") == ""


class TestShouldReplaceVenue:
    def test_matches_default_location(self):
        from engine.ingest_core import _should_replace_venue
        assert _should_replace_venue(
            "South Lake Tahoe",
            {"default_location": "South Lake Tahoe, CA"},
        )

    def test_matches_skip_list(self):
        from engine.ingest_core import _should_replace_venue
        assert _should_replace_venue(
            "Lake Tahoe",
            {"default_location": "SLT", "skip_venue_names": ["Lake Tahoe, CA"]},
        )

    def test_real_venue_not_replaced(self):
        from engine.ingest_core import _should_replace_venue
        assert not _should_replace_venue(
            "Riva Grill",
            {"default_location": "South Lake Tahoe, CA"},
        )

    def test_empty_treated_as_needs_replacement(self):
        from engine.ingest_core import _should_replace_venue
        assert _should_replace_venue("", {"default_location": "SLT, CA"})


# =====================================================================
# Timezone helpers
# =====================================================================


class TestParseDt:
    def test_iso_with_tz(self):
        from engine.ingest_core import _parse_dt
        dt = _parse_dt("2026-07-15T20:00:00-07:00")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 7 and dt.day == 15

    def test_fuzzy_decorative_tokens(self):
        """dateutil's fuzzy=True handles 'July 6 @ 7:30 p.m.' — the strict
        parse fails first and we fall back."""
        from engine.ingest_core import _parse_dt
        dt = _parse_dt("July 6, 2026 @ 7:30 p.m.")
        assert dt is not None
        assert dt.month == 7 and dt.day == 6

    def test_empty_returns_none(self):
        from engine.ingest_core import _parse_dt
        assert _parse_dt("") is None
        assert _parse_dt(None) is None

    def test_garbage_returns_none(self):
        from engine.ingest_core import _parse_dt
        # No digits at all → both strict and fuzzy fail.
        assert _parse_dt("banana") is None

    def test_naive_gets_default_tz_attached(self):
        from engine.ingest_core import _parse_dt, DEFAULT_TZ
        dt = _parse_dt("2026-07-15 20:00:00")
        assert dt is not None
        assert dt.tzinfo is not None
        # Same offset as DEFAULT_TZ on that date.
        expected = datetime(2026, 7, 15, 20, 0, 0, tzinfo=DEFAULT_TZ)
        assert dt.utcoffset() == expected.utcoffset()

    def test_passthrough_datetime(self):
        from engine.ingest_core import _parse_dt
        d = datetime(2026, 1, 1, 12, 0, 0)
        # Naive datetime gets tz attached
        out = _parse_dt(d)
        assert out.tzinfo is not None


# =====================================================================
# Identity + content hashing
# =====================================================================


class TestEventHashing:
    def test_event_uid_deterministic(self, sample_event):
        from engine.ingest_core import _event_uid
        assert _event_uid(sample_event) == _event_uid(sample_event)

    def test_event_uid_is_sha1_hex(self, sample_event):
        from engine.ingest_core import _event_uid
        uid = _event_uid(sample_event)
        assert len(uid) == 40
        int(uid, 16)  # doesn't raise → is hex

    def test_event_uid_differs_by_source(self, sample_event):
        from engine.ingest_core import _event_uid, Event
        other = Event(**{**sample_event.__dict__, "source": "other-source",
                          "mining_extras": {}})
        assert _event_uid(sample_event) != _event_uid(other)

    def test_event_uid_differs_by_date_day(self, sample_event):
        from engine.ingest_core import _event_uid, Event
        other = Event(**{**sample_event.__dict__,
                          "date": "2026-07-16T20:00:00-07:00",
                          "mining_extras": {}})
        assert _event_uid(sample_event) != _event_uid(other)

    def test_event_uid_ignores_time_of_day(self, sample_event):
        """Only the first 10 chars (date_day) matter for identity."""
        from engine.ingest_core import _event_uid, Event
        same_day = Event(**{**sample_event.__dict__,
                             "date": "2026-07-15T09:00:00-07:00",
                             "mining_extras": {}})
        assert _event_uid(sample_event) == _event_uid(same_day)

    def test_content_hash_deterministic(self, sample_event):
        from engine.ingest_core import _content_hash
        assert _content_hash(sample_event) == _content_hash(sample_event)

    def test_content_hash_includes_extra(self, sample_event):
        from engine.ingest_core import _content_hash
        h1 = _content_hash(sample_event)
        h2 = _content_hash(sample_event, extra={"organizer": {"name": "X"}})
        assert h1 != h2

    def test_content_hash_ignores_mining_extras(self, sample_event):
        """mining_extras is in-memory only and MUST NOT affect content_hash."""
        from engine.ingest_core import _content_hash, Event
        with_extras = Event(**{**sample_event.__dict__,
                                "mining_extras": {"performers": [{"name": "X"}]}})
        assert _content_hash(sample_event) == _content_hash(with_extras)


class TestEntityHashing:
    def test_entity_uid_deterministic(self):
        from engine.ingest_core import _entity_uid
        assert _entity_uid("business", "Test Cafe") == _entity_uid("business", "Test Cafe")

    def test_entity_uid_collapses_by_norm(self):
        """Same name across casings/whitespace collides — deliberate."""
        from engine.ingest_core import _entity_uid
        assert _entity_uid("business", "Test Cafe") == _entity_uid("business", "TEST  CAFE")

    def test_entity_uid_kind_scoped(self):
        """A venue named X and a business named X are DIFFERENT entities."""
        from engine.ingest_core import _entity_uid
        assert _entity_uid("venue", "Common Name") != _entity_uid("business", "Common Name")

    def test_entity_content_hash_deterministic(self):
        from engine.ingest_core import _entity_content_hash, Business
        b = Business(name="X", website="https://x.com")
        assert _entity_content_hash("business", b) == _entity_content_hash("business", b)

    def test_entity_content_hash_ignores_source_metadata(self):
        """source/source_url/extra are audit fields — they must NOT drive
        PATCH-vs-no-op detection."""
        from engine.ingest_core import _entity_content_hash, Business
        b1 = Business(name="X", website="https://x.com", source="src-a")
        b2 = Business(name="X", website="https://x.com", source="src-b")
        assert _entity_content_hash("business", b1) == _entity_content_hash("business", b2)


# =====================================================================
# Event dedupe_key
# =====================================================================


class TestEventDedupeKey:
    def test_normalizes_name(self):
        from engine.ingest_core import Event
        e = Event(name="  Jazz Night  ", location="Riva",
                   date="2026-07-15", description="", hosting_group="")
        assert e.dedupe_key()[0] == "jazz night"

    def test_uses_first_10_chars_of_date(self):
        from engine.ingest_core import Event
        e = Event(name="X", location="Y",
                   date="2026-07-15T20:00:00Z", description="", hosting_group="")
        assert e.dedupe_key()[1] == "2026-07-15"

    def test_truncates_location(self):
        from engine.ingest_core import Event
        e = Event(name="X", location="A" * 100, date="2026-07-15",
                   description="", hosting_group="")
        assert len(e.dedupe_key()[2]) == 40


# =====================================================================
# ENTITY_REG shape invariants
# =====================================================================


class TestEntityReg:
    def test_all_five_kinds_present(self):
        from engine.ingest_core import ENTITY_REG, ENTITY_KINDS
        assert set(ENTITY_KINDS) == {"venue", "business", "community", "performer", "creator"}
        assert set(ENTITY_REG.keys()) == set(ENTITY_KINDS)

    def test_each_spec_has_required_keys(self):
        from engine.ingest_core import ENTITY_REG
        for kind, spec in ENTITY_REG.items():
            assert "table" in spec, f"{kind} missing table"
            assert "endpoint" in spec, f"{kind} missing endpoint"
            assert "cls" in spec, f"{kind} missing cls"
            assert "cols" in spec, f"{kind} missing cols"
            assert "hash_cols" in spec, f"{kind} missing hash_cols"

    def test_hash_cols_equals_cols(self):
        """After the P1.1 socials addition, hash_cols mirrors cols in full."""
        from engine.ingest_core import ENTITY_REG
        for kind, spec in ENTITY_REG.items():
            assert spec["hash_cols"] == spec["cols"], f"{kind} hash_cols diverged from cols"

    def test_endpoint_pluralization(self):
        """The explicit endpoint table exists to avoid the community→communitys
        and business→businesss pluralization bug."""
        from engine.ingest_core import ENTITY_REG
        assert ENTITY_REG["business"]["endpoint"] == "businesses"
        assert ENTITY_REG["community"]["endpoint"] == "communities"

    def test_socials_present_where_api_accepts(self):
        """P1.1: businesses+communities+performers+creators gained socials."""
        from engine.ingest_core import ENTITY_REG
        for k in ("business", "community", "performer"):
            for social in ("facebook", "tiktok", "twitter", "youtube", "linkedin"):
                assert social in ENTITY_REG[k]["cols"], f"{k} missing {social}"
        # Creator uses `youtube_url` instead of `youtube`.
        assert "linkedin" in ENTITY_REG["creator"]["cols"]
        assert "youtube_url" in ENTITY_REG["creator"]["cols"]
        # Venue is intentionally socials-free.
        for social in ("facebook", "tiktok", "twitter", "youtube", "linkedin"):
            assert social not in ENTITY_REG["venue"]["cols"], \
                f"venue erroneously has {social}"
