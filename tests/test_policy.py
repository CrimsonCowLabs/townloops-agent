"""engine/policy.py — token bucket + confidence gate + category-exists guard."""

from __future__ import annotations

import json
import time

import pytest


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
