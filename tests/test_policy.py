"""engine/policy.py — daily caps + ramp + token bucket + confidence gate
+ category resolution + category-exists guard.

Uses `fresh_db` (all migrations applied) so post_counters exists.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest


class TestCaps:
    def test_ramp_day_zero(self):
        from engine import policy
        caps = policy.caps_for_day(0)
        assert caps["businesses"] == 5
        assert caps["venues"] == 5
        assert caps["communities"] == 5

    def test_ramp_day_one(self):
        from engine import policy
        caps = policy.caps_for_day(1)
        assert caps["businesses"] == 15

    def test_ramp_steady(self):
        from engine import policy
        assert policy.caps_for_day(2)["businesses"] == 30
        assert policy.caps_for_day(30)["businesses"] == 30


class TestReserveSlot:
    def test_reserves_then_hits_cap(self, fresh_db):
        from engine import policy
        for i in range(5):
            ok, why = policy.reserve_post_slot(fresh_db, "businesses", 0)
            assert ok, f"iter {i}: {why}"
        ok, why = policy.reserve_post_slot(fresh_db, "businesses", 0)
        assert not ok
        assert "daily cap" in why

    def test_events_are_unbounded(self, fresh_db):
        from engine import policy
        # Way past any conceivable cap — events skip the counter.
        for _ in range(1000):
            ok, _ = policy.reserve_post_slot(fresh_db, "events", 0)
            assert ok

    def test_unknown_resource_rejected(self, fresh_db):
        from engine import policy
        ok, why = policy.reserve_post_slot(fresh_db, "widgets", 0)
        assert not ok
        assert "unknown" in why

    def test_counters_are_per_resource(self, fresh_db):
        from engine import policy
        for _ in range(5):
            policy.reserve_post_slot(fresh_db, "businesses", 0)
        # venues share the same day but a different counter — not exhausted
        ok, _ = policy.reserve_post_slot(fresh_db, "venues", 0)
        assert ok

    def test_counters_are_per_day(self, fresh_db):
        from engine import policy
        for _ in range(5):
            policy.reserve_post_slot(fresh_db, "businesses", 0, day="2026-01-01")
        # Different day → fresh counter
        ok, _ = policy.reserve_post_slot(fresh_db, "businesses", 0, day="2026-01-02")
        assert ok


class TestTokenBucket:
    def test_burst_then_deny(self):
        from engine.policy import TokenBucket
        tb = TokenBucket(rate=0.01, burst=3)
        assert tb.try_take()
        assert tb.try_take()
        assert tb.try_take()
        assert not tb.try_take()

    def test_wait_refills(self):
        from engine.policy import TokenBucket
        tb = TokenBucket(rate=200.0, burst=1)
        assert tb.try_take()
        assert not tb.try_take()
        # 200 tokens/sec = ~5ms per token; wait a hair longer.
        time.sleep(0.02)
        assert tb.try_take()

    def test_wait_times_out(self):
        from engine.policy import TokenBucket
        tb = TokenBucket(rate=0.01, burst=1)
        tb.try_take()  # exhaust
        with pytest.raises(TimeoutError):
            tb.wait(timeout=0.1)


class TestConfidence:
    def test_missing_verify_json(self):
        from engine import policy
        ok, why = policy.is_high_confidence(None, "Town")  # type: ignore[arg-type]
        assert not ok
        assert "missing" in why

    def test_verified_false(self):
        from engine import policy
        ok, why = policy.is_high_confidence({"verified": False}, "Town")
        assert not ok

    def test_no_signal(self):
        from engine import policy
        ok, why = policy.is_high_confidence(
            {"verified": True, "evidence": "town"}, "Town"
        )
        assert not ok
        assert "no address" in why

    def test_closure_marker(self):
        from engine import policy
        ok, why = policy.is_high_confidence(
            {"verified": True, "address": "123 Main",
             "evidence": "town business permanently closed"},
            "Town",
        )
        assert not ok
        assert "closure" in why

    def test_town_not_present(self):
        from engine import policy
        ok, why = policy.is_high_confidence(
            {"verified": True, "phone": "555-1212",
             "evidence": "some business in a different city"},
            "South Lake Tahoe",
        )
        assert not ok
        assert "town name" in why

    def test_happy_path(self):
        from engine import policy
        ok, why = policy.is_high_confidence(
            {"verified": True, "address": "123 Main",
             "evidence": "welcome to south lake tahoe cafe menu"},
            "South Lake Tahoe",
        )
        assert ok, why


class TestCategoryResolution:
    def test_unique_match(self):
        from engine import policy
        slug = policy.resolve_category_slug(
            "business", "Sunrise Cafe", "coffee and pastries",
            {"food-and-drink", "shopping"},
        )
        assert slug == "food-and-drink"

    def test_ambiguous(self):
        from engine import policy
        slug = policy.resolve_category_slug(
            "business", "Mix Shop", "gallery restaurant boutique",
            {"food-and-drink", "shopping", "arts"},
        )
        assert slug is None

    def test_no_match(self):
        from engine import policy
        slug = policy.resolve_category_slug(
            "business", "Foo", "asdfghjkl",
            {"food-and-drink", "shopping"},
        )
        assert slug is None

    def test_skips_unavailable_slugs(self):
        from engine import policy
        # 'nonprofit' would match "foundation" but isn't in available.
        slug = policy.resolve_category_slug(
            "community", "The Foundation", "we do good work",
            {"food-and-drink"},
        )
        assert slug is None


class TestCategoryExistsGuard:
    def test_has_category_yes(self, tmp_state_dir):
        from engine import policy
        snap = tmp_state_dir / "categories.json"
        snap.write_text(json.dumps([{"slug": "food"}, {"slug": "arts"}]))
        assert policy.has_category("food", snap)
        assert not policy.has_category("municipal", snap)

    def test_missing_snapshot(self, tmp_state_dir):
        from engine import policy
        assert not policy.has_category("food", tmp_state_dir / "nope.json")

    def test_malformed_snapshot(self, tmp_state_dir):
        from engine import policy
        snap = tmp_state_dir / "categories.json"
        snap.write_text("not json {")
        assert not policy.has_category("food", snap)


class TestDaysSince:
    def test_same_day(self):
        from engine import policy
        past = datetime.now(timezone.utc).isoformat()
        assert policy.days_since(past) == 0

    def test_unparseable(self):
        from engine import policy
        # Sentinel: unparseable → treat as steady state.
        assert policy.days_since("not-a-date") == 999
