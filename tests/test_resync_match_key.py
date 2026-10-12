"""Resync's remote-side match key is a TOWN-LOCAL date.

TownLoops returns `start_at` as a UTC ISO timestamp; the local `events.date_day`
column is town-local. Keying the remote side off the raw UTC string agrees for
daytime events and disagrees by a day for anything starting after ~17:00 local,
so an evening show never matched and its stale `townloops_slug` survived every
resync. Found in production 2026-10-12 (TownLoops issue #26): four SLT rows
still held `hermes-slt-*` slugs after a clean resync, every one of them evening.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_ingest():
    path = Path(__file__).resolve().parent.parent / "skills/business/event-ingest/ingest.py"
    spec = importlib.util.spec_from_file_location("event_ingest_matchkey", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ingest():
    return _load_ingest()


class TestMatchEventKey:
    def test_evening_show_keys_on_the_local_date(self, ingest):
        """7:30 PM Oct 31 Pacific is 02:30 UTC on Nov 1. The key must say
        Oct 31 — the date `events.date_day` holds and the date the event
        page shows.
        """
        key = ingest._match_event_key(
            "Direct From Sweden: The Music of ABBA", "2026-11-01T02:30:00+00:00"
        )
        assert key[1] == "2026-10-31"

    def test_midnight_utc_keys_on_the_previous_local_day(self, ingest):
        """00:00 UTC is 5 PM the previous day in Pacific — the shape that
        bit the three 'Plates' rows.
        """
        key = ingest._match_event_key("Plates", "2026-10-17T00:00:00+00:00")
        assert key[1] == "2026-10-16"

    def test_daytime_show_is_unchanged(self, ingest):
        """The rows that always matched must keep matching."""
        key = ingest._match_event_key(
            "Planning Commission Meeting", "2026-10-15T22:00:00+00:00"
        )
        assert key[1] == "2026-10-15"

    def test_title_is_still_normalized(self, ingest):
        a = ingest._match_event_key("  Jazz   Night  ", "2026-10-15T22:00:00+00:00")
        b = ingest._match_event_key("Jazz Night", "2026-10-15T22:00:00+00:00")
        assert a == b

    @pytest.mark.parametrize("bad", ["", None, "not-a-timestamp"])
    def test_unparseable_start_at_does_not_crash_a_resync(self, ingest, bad):
        """A malformed timestamp must degrade, not abort the run — resync is
        a recovery tool and has to survive bad remote data.
        """
        key = ingest._match_event_key("Whatever", bad)
        assert isinstance(key, tuple) and len(key) == 2
