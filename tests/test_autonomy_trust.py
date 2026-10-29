"""Autonomous mode, trust (issue #1, gaps 2 and 3): a seed's trust score
decides its trial approval, its demotion to syndicator and its retirement,
and a Series the moderator keeps publishing is a well-established event
whose new occurrences need no AI judgement. Policy functions are tested
directly; the seed review and event-find through tests/autonomy_town.py."""

from __future__ import annotations

import sqlite3

import pytest

from engine import autonomy
from engine.event_extract import utc_times
from tests.autonomy_town import PROFILE, Town, soon

ARENA = "https://arena.example/events"
TRIVIA = {"id": 1, "slug": "trivia-night", "title": "Trivia Night", "is_active": True,
          "town": "slt", "cadence_text": None, "description": None}
AUTO = autonomy.Settings(mode="autonomous")


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def outcomes(conn, seed: str, published: int = 0, rejected: int = 0,
             series: str = None, at: str = "2026-01-01") -> None:
    """Decided submissions attributed to a seed (and Series), oldest first."""
    n = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    for i, status in enumerate(["published"] * published + ["rejected"] * rejected):
        conn.execute("INSERT INTO submissions (resource, slug, seed, series_slug, status, "
                     "submitted_at, decided_at) VALUES ('events', ?, ?, ?, ?, ?, ?)",
                     (f"s{n + i}", seed, series, status, at, f"{at}T00:00:{i:02d}"))
    conn.commit()


def seed_dict(conn, name: str) -> dict:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return dict(cur.execute("SELECT * FROM event_seeds WHERE name = ?", (name,)).fetchone())


class TestSettings:
    def test_trust_thresholds_and_the_publish_streak_are_configurable(self):
        s = autonomy.settings({"operating_mode": "autonomous", "autonomy": {
            "seed_trust": {"approve": 0.1, "syndicator": 0.6, "retire": 0.2},
            "series_publish_streak": 5}})
        assert (s.seed_trust_approve, s.seed_trust_syndicator, s.seed_trust_retire,
                s.series_publish_streak) == (0.1, 0.6, 0.2, 5)

    def test_defaults(self):
        s = autonomy.settings({})
        assert s.seed_trust_retire < s.seed_trust_syndicator < 0.5   # a new seed starts at 0.5
        assert s.series_publish_streak == 3


class TestTrialApproval:
    def test_a_calendar_that_is_mostly_unusable_scores_too_low_to_approve(self, town):
        town.seed("Arena", ARENA, status="proposed")
        town.listing(ARENA, [concert("Jazz Night")] +
                     [concert(f"Old Show {i}", days=-3 - i) for i in range(9)])
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "rejected"
        [d] = town.decisions("seed")
        assert d["reason"] == "trial-low-yield"
        assert d["evidence"]["trust"] < d["evidence"]["threshold"]
        assert town.seed_row("Arena")["trust_score"] == d["evidence"]["trust"]

    def test_the_approval_bar_is_the_towns_to_set(self, town):
        town.set_profile(PROFILE, seed_trust={"approve": 0.1})
        town.seed("Arena", ARENA, status="proposed")
        town.listing(ARENA, [concert("Jazz Night")] +
                     [concert(f"Old Show {i}", days=-3 - i) for i in range(9)])
        town.seed_review()
        seed = town.seed_row("Arena")
        assert seed["status"] == "approved"
        [d] = town.decisions("seed")
        assert d["reason"] == "trial-yielded-events" and d["evidence"]["trust"] < 0.2
        assert seed["trust_score"] == 0.5      # once approved, trust is the moderator's to earn

    def test_the_trial_score_is_a_smoothed_share_of_usable_events(self):
        ok = autonomy.Decision("approve", "checks-passed")
        drop = autonomy.Decision("drop", "past")
        items = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
        d = autonomy.judge_trial(items, [ok, ok, drop], None, AUTO)
        assert (d.verdict, d.evidence["trust"]) == ("approve", round(3 / 5, 3))


class TestSeedDecisions:
    def _seed(self, conn, **cols):
        data = {"name": "Arena", "url": ARENA, "kind": "venue", "status": "approved",
                "first_seen_at": "x", "last_seen_at": "x", **cols}
        conn.execute(f"INSERT INTO event_seeds ({', '.join(data)}) "
                     f"VALUES ({', '.join('?' * len(data))})", tuple(data.values()))
        conn.commit()
        return seed_dict(conn, "Arena")

    def test_low_trust_retires_a_seed_with_the_score_in_evidence(self, fresh_db):
        seed = self._seed(fresh_db)
        outcomes(fresh_db, "Arena", published=1, rejected=4)
        d = autonomy.judge_seed(fresh_db, seed, AUTO)
        assert (d.verdict, d.reason) == ("retire", "low-trust")
        assert d.evidence["trust"] < AUTO.seed_trust_retire
        assert d.evidence["threshold"] == AUTO.seed_trust_retire

    def test_a_seed_is_not_judged_on_one_or_two_outcomes(self, fresh_db):
        seed = self._seed(fresh_db, source_role="primary")
        outcomes(fresh_db, "Arena", rejected=2)
        assert autonomy.judge_seed(fresh_db, seed, AUTO).verdict == "keep"
        assert autonomy.classify_role(fresh_db, seed, AUTO).verdict != "syndicator"

    def test_middling_trust_demotes_a_seed_to_syndicator_but_keeps_it(self, fresh_db):
        seed = self._seed(fresh_db, source_role="primary")
        outcomes(fresh_db, "Arena", published=2, rejected=3)
        role = autonomy.classify_role(fresh_db, seed, AUTO)
        assert (role.verdict, role.reason) == ("syndicator", "low-trust")
        assert AUTO.seed_trust_retire <= role.evidence["trust"] < AUTO.seed_trust_syndicator
        assert autonomy.judge_seed(fresh_db, seed, AUTO).verdict == "keep"

    def test_a_low_trust_seed_under_a_health_alert_is_reprobed_before_retiring(self, fresh_db):
        seed = self._seed(fresh_db, health_status="zero", health_flagged_at="2026-01-01")
        outcomes(fresh_db, "Arena", published=1, rejected=4)
        d = autonomy.judge_seed(fresh_db, seed, AUTO)
        assert (d.verdict, d.reason) == ("check-moved", "low-trust")
        assert "trust" in d.evidence

    def test_a_kept_seed_reports_its_trust(self, fresh_db):
        seed = self._seed(fresh_db)
        outcomes(fresh_db, "Arena", published=5)
        d = autonomy.judge_seed(fresh_db, seed, AUTO)
        assert d.verdict == "keep" and d.evidence["trust"] > 0.5

    def test_losing_dedup_as_a_syndicator_is_not_held_against_it(self, fresh_db):
        primary = self._seed(fresh_db, source_role="primary")
        fresh_db.execute("UPDATE event_seeds SET source_role = 'syndicator'")
        syndicator = seed_dict(fresh_db, "Arena")
        for uid, status in (("a1", "duplicate"), ("b1", "active")):
            fresh_db.execute(
                "INSERT INTO events (event_uid, source, source_kind, name, date, date_day, "
                "first_seen_at, last_seen_at, content_hash, last_modified_at, "
                "canonical_event_uid, status) VALUES (?, ?, 'seed', 'X', 'd', 'd', 'x', 'x', "
                "'h', 'x', 'c1', ?)", (uid, "seed:Arena" if uid == "a1" else "seed:Other", status))
        assert autonomy.seed_trust(fresh_db, primary) < 0.5
        assert autonomy.seed_trust(fresh_db, syndicator) == 0.5

    def test_supervised_mode_leaves_seeds_to_the_operator(self, fresh_db):
        seed = self._seed(fresh_db, source_role="primary")
        outcomes(fresh_db, "Arena", rejected=9)
        s = autonomy.Settings()
        assert autonomy.judge_seed(fresh_db, seed, s).verdict == autonomy.AWAIT
        assert autonomy.classify_role(fresh_db, seed, s).verdict == autonomy.AWAIT


class TestSeedReview:
    def test_the_review_demotes_a_middling_seed_and_retires_a_bad_one(self, town):
        town.seed("Arena", ARENA, source_role="primary")
        town.seed("Bar", "https://bar.example/events", source_role="primary")
        outcomes(town.conn, "Arena", published=2, rejected=3)
        outcomes(town.conn, "Bar", published=0, rejected=5)
        town.seed_review()
        arena, bar = town.seed_row("Arena"), town.seed_row("Bar")
        assert (arena["status"], arena["source_role"]) == ("approved", "syndicator")
        assert bar["status"] == "retired"
        [role] = town.decisions("seed-role", target="seed:Arena")
        assert role["reason"] == "low-trust" and role["evidence"]["trust"] == arena["trust_score"]
        [d] = [d for d in town.decisions("seed") if d["verdict"] == "retire"]
        assert d["reason"] == "low-trust" and d["evidence"]["trust"] == bar["trust_score"]


class TestEstablishedSeries:
    def test_a_publish_streak_makes_a_series_established(self, fresh_db):
        outcomes(fresh_db, "Arena", published=3, series="trivia-night")
        assert autonomy.series_established(fresh_db, "trivia-night", AUTO) == 3
        outcomes(fresh_db, "Arena", rejected=1, series="trivia-night", at="2026-02-01")
        assert not autonomy.series_established(fresh_db, "trivia-night", AUTO)
        outcomes(fresh_db, "Arena", published=2, series="trivia-night", at="2026-03-01")
        assert not autonomy.series_established(fresh_db, "trivia-night", AUTO)
        assert not autonomy.series_established(
            fresh_db, "trivia-night", autonomy.Settings(mode="autonomous",
                                                        series_publish_streak=0))

    def test_an_established_series_skips_the_ai_pass(self):
        ctx = autonomy.JudgeContext(town="South Lake Tahoe",
                                    series_established=lambda it: ("trivia-night", 4))

        def no_llm(prompt):
            raise AssertionError("the AI pass ran")
        item = {"title": "Trivia Night", "date": soon(5), "date_utc": utc_times(soon(5), None)[0]}
        [d] = autonomy.judge_items([item], "a page", ctx, no_llm)
        assert (d.verdict, d.reason) == ("approve", "series-established")
        assert d.evidence == {"series_slug": "trivia-night", "publish_streak": 4}

    def test_an_established_series_still_fails_the_deterministic_checks(self):
        ctx = autonomy.JudgeContext(town="South Lake Tahoe", zip_codes=("96150",),
                                    series_established=lambda it: ("trivia-night", 4))
        item = {"title": "Trivia Night", "date": soon(5), "date_utc": "2000-01-01T03:00:00Z"}
        [d] = autonomy.judge_items([item], None, ctx)
        assert d.reason == "past"

    def test_new_occurrences_of_a_series_the_moderator_keeps_publishing_ship_unjudged(self, town):
        town.api.series["trivia-night"] = dict(TRIVIA)
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Trivia Night", days=d) for d in (3, 10, 17)])
        town.run()
        for p in town.submitted_events():
            town.api.approve("events", town.api.posted_slug("events", p))
        town.poll()

        # A bare listing — no venue or location — that only the AI pass could settle.
        town.listing(ARENA, [concert("Trivia Night", days=24, venue=None, location=None)])
        town.judge = lambda prompt: (_ for _ in ()).throw(AssertionError("the AI pass ran"))
        town.run()
        assert len(town.submitted_events()) == 4
        assert town.reasons()["Trivia Night"] == "series-established"
