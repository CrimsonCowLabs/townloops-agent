"""Live event PATCHes are full bodies built from the current live row, then
verified with a GET (ticket 03 — a partial PATCH once blanked a title)."""

from __future__ import annotations

import copy

import pytest

from engine.live_patch import apply_event_patch
from tests.fake_townloops import FakeAPI


LIVE = {
    "id": 7, "slug": "jazz", "title": "Jazz Night", "description": "Weekly jazz.",
    "start_at": "2026-10-10T03:00:00Z", "end_at": "2026-10-10T05:00:00Z",
    "status": "published", "is_featured": False,
    "venue_id": 3, "venue_slug": "gunbarrel-tavern",
    "category_slugs": ["food-drink", "live-music"], "category_slug": "food-drink",
    "source_url": "https://example.com/jazz", "accessibility_notes": None,
    "tags": ["21-plus", "jazz"], "town_slug": "slt",
}
VENUES = [
    {"id": 3, "slug": "gunbarrel-tavern", "name": "Gunbarrel Tavern"},
    {"id": 99, "slug": "tipsy-putt", "name": "Tipsy Putt"},
]


class TestApplyEventPatch:
    def test_the_ingest_ref_is_never_in_a_patch_body(self):
        """Set once, by engine/ingest_ref.adopt alone — never round-tripped."""
        from engine.live_patch import build_entity_patch, build_event_patch
        live = {**LIVE, "ingest_ref": "hermes-slt-0123456789abcdef"}
        body = build_event_patch(live, {"ingest_ref": "hermes-slt-x", "venue_ingest_ref": "v",
                                        "description": "New."})
        assert "ingest_ref" not in body and "venue_ingest_ref" not in body
        assert body["description"] == "New."
        venue = {"slug": "riva", "name": "Riva", "ingest_ref": "hermes-slt-0123456789abcdef"}
        assert "ingest_ref" not in build_entity_patch("venues", venue, {"ingest_ref": "x"})

    def test_unchanged_fields_survive_a_start_time_change(self):
        api = FakeAPI([LIVE], VENUES)
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-10T04:00:00+00:00"})
        assert result.mismatches == []
        row = api.events["jazz"]
        assert row["start_at"] == "2026-10-10T04:00:00+00:00"
        for field in ("title", "description", "end_at", "venue_id", "source_url"):
            assert row[field] == LIVE[field], field
        assert row["category_slugs"] == ["food-drink", "live-music"]
        assert row["tags"] == ["21-plus", "jazz"]

    def test_full_category_and_tag_sets_are_always_sent(self):
        api = FakeAPI([LIVE], VENUES)
        apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00"})
        [body] = api.patches()
        assert body["category_slugs"] == ["food-drink", "live-music"]
        assert body["tags"] == ["21-plus", "jazz"]
        assert body["title"] == "Jazz Night"

    def test_change_the_server_drops_is_reported_as_mismatch(self):
        api = FakeAPI([LIVE], VENUES, ignore={"start_at"})
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00"})
        assert len(result.mismatches) == 1
        assert result.mismatches[0].startswith("start_at:")

    def test_equivalent_time_formats_are_not_mismatches(self):
        live = {**LIVE, "start_at": "2026-10-17T03:00:00Z", "end_at": None}
        api = FakeAPI([live], VENUES)
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00",
                                                 "title": "Jazz Night"})
        assert result.mismatches == []

    def test_venue_change_by_id_rebinds_venue(self):
        api = FakeAPI([LIVE], VENUES)
        result = apply_event_patch(api, "jazz", {"venue_id": 99})
        assert result.mismatches == []
        assert api.events["jazz"]["venue_slug"] == "tipsy-putt"

    def test_venue_change_by_name_is_not_overridden_by_old_venue_id(self):
        api = FakeAPI([LIVE], VENUES)
        result = apply_event_patch(api, "jazz", {"venue_name": "Tipsy Putt"})
        assert "venue_id" not in result.body
        assert api.events["jazz"]["venue_slug"] == "tipsy-putt"

    def test_end_before_new_start_is_never_sent(self):
        api = FakeAPI([LIVE], VENUES)
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00"})
        # live end (Oct 10 05:00) would fall before the new start
        assert "end_at" not in result.body

    def test_uses_given_current_row_without_extra_get(self):
        api = FakeAPI([LIVE], VENUES)
        apply_event_patch(api, "jazz", {"title": "Jazz Night!"}, current=copy.deepcopy(LIVE))
        assert [c[0] for c in api.calls] == ["PATCH", "GET"]

    def test_venue_bound_in_another_town_survives_a_date_change(self):
        reno = {"id": 500, "slug": "reno-arena", "name": "Reno Arena", "town": "reno"}
        api = FakeAPI([{**LIVE, "venue_id": 500, "venue_slug": "reno-arena"}], VENUES + [reno])
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-10T04:00:00+00:00"})
        assert result.mismatches == []
        assert api.events["jazz"]["venue_slug"] == "reno-arena"

    def test_empty_live_row_is_refused_not_patched(self):
        api = FakeAPI([LIVE], VENUES)
        with pytest.raises(ValueError):
            apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00"}, current={})
        assert api.patches() == []

    def test_live_title_too_short_to_resend_is_left_alone(self):
        api = FakeAPI([{**LIVE, "title": ""}], VENUES)
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-10T04:00:00+00:00"})
        assert "title" not in result.body
        assert result.mismatches == []

    def test_end_left_before_moved_start_is_a_mismatch(self):
        api = FakeAPI([LIVE], VENUES)
        result = apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00+00:00"})
        assert any(m.startswith("end_at") for m in result.mismatches)

    def test_naive_time_override_is_refused(self):
        api = FakeAPI([LIVE], VENUES)
        with pytest.raises(ValueError):
            apply_event_patch(api, "jazz", {"start_at": "2026-10-17T03:00:00"})
        assert api.patches() == []
