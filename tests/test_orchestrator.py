"""engine/orchestrator.py — event-first workqueue drainer.

The old phase machine (bootstrapping → … → steady_state) was retired in the
event-first rewrite; `run_tick`/`drain_workqueue` now drain the event workqueue.
These tests cover the action-table invariants and the drain loop's advance /
reap behavior with the skill subprocess mocked out.
"""

from __future__ import annotations

import pytest

from engine import event_state, orchestrator, workqueue


_NOW = "2026-08-01T00:00:00+00:00"


def _insert_event(conn, uid, *, state, approval_status="approved"):
    conn.execute(
        "INSERT INTO events(event_uid, source, source_kind, name, date, date_day,"
        " first_seen_at, last_seen_at, content_hash, last_modified_at,"
        " state, approval_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "seed:test", "html", "Test Event", _NOW, "2026-08-01",
         _NOW, _NOW, "hash-" + uid, _NOW, state, approval_status),
    )
    conn.commit()


class TestActionTables:
    """The three action tables must stay mutually consistent, and the dead
    `extract-metadata` action must be gone from all of them."""

    def test_all_three_tables_agree(self):
        trans_actions = {act for (act, _) in event_state._TRANSITIONS.values()}
        skill_actions = set(orchestrator._ACTION_SKILLS)
        rl_actions = set(workqueue._DEFAULT_RATE_LIMITS)
        assert trans_actions == skill_actions == rl_actions

    def test_no_extract_metadata_anywhere(self):
        assert "extract-metadata" not in orchestrator._ACTION_SKILLS
        assert "extract-metadata" not in workqueue._DEFAULT_RATE_LIMITS
        assert event_state.next_action_for("found") is None

    def test_every_mapped_action_has_a_transition(self):
        # Each drainable action must produce a real next state.
        for action in orchestrator._ACTION_SKILLS:
            assert event_state.next_state_for(action) is not None


class TestDrainWorkqueue:
    def test_success_cascades_the_full_pipeline_in_one_tick(self, fresh_db, monkeypatch):
        # A ready event whose skills all succeed cascades through every action
        # in one drain: _ACTION_SKILLS is ordered detail→venue→hosts→people→enrich→ship,
        # and each success enqueues the next action, which the same pass claims.
        _insert_event(fresh_db, "e1", state="approved")
        workqueue.enqueue(fresh_db, "e1", event_state.next_action_for("approved"))
        fresh_db.commit()

        monkeypatch.setattr(orchestrator, "_invoke_skill",
                            lambda *a, **k: (0, "ok", ""))
        summary = orchestrator.drain_workqueue(fresh_db)

        # Every action advanced exactly one event.
        for action in orchestrator._ACTION_SKILLS:
            assert summary["per_action"][action]["completed"] == 1

        ev = fresh_db.execute(
            "SELECT state FROM events WHERE event_uid='e1'"
        ).fetchone()
        assert ev["state"] == "shipped"                # traversed to terminal
        unresolved = fresh_db.execute(
            "SELECT COUNT(*) FROM event_workqueue "
            "WHERE event_uid='e1' AND resolved_at IS NULL"
        ).fetchone()[0]
        assert unresolved == 0                         # nothing left pending

    def test_drain_brings_a_db_behind_the_repo_up_to_date(self, tmp_state_dir, monkeypatch):
        # Issue 25: the heartbeat runs repo code on every town, so it migrates
        # first — a pulled-but-unmigrated DB must not crash the drain.
        from engine import db as db_mod
        conn = db_mod.connect(str(tmp_state_dir / "behind.db"))
        monkeypatch.setattr(orchestrator, "_invoke_skill",
                            lambda *a, **k: (0, "ok", ""))

        summary = orchestrator.drain_workqueue(conn)

        assert not summary.get("error")
        assert db_mod.run_migrations(conn) == []

    def test_drain_reaps_stale_claims_first(self, fresh_db, monkeypatch):
        _insert_event(fresh_db, "e2", state="approved")
        workqueue.enqueue(fresh_db, "e2", "extract-venue")
        # Orphan the claim (worker died) with a long-past claimed_at.
        fresh_db.execute(
            "UPDATE event_workqueue SET claimed_by='dead', "
            "claimed_at='2000-01-01T00:00:00+00:00' WHERE event_uid='e2'"
        )
        fresh_db.commit()

        monkeypatch.setattr(orchestrator, "_invoke_skill",
                            lambda *a, **k: (0, "ok", ""))
        summary = orchestrator.drain_workqueue(fresh_db)

        # Reaper cleared the orphan (attempts bumped to 1) so it could be
        # re-claimed and processed in the same tick.
        assert summary["reaped_stale_claims"] == 1


# --------------------------------------------------------------------------
# Read-detail throughput (issue #1 gap 7): the heartbeat runs detached, so a
# drain may fetch several detail pages, bounded by the town's
# `workqueue_drain` settings and a wall-clock budget.
# --------------------------------------------------------------------------

@pytest.fixture
def profile(tmp_state_dir, monkeypatch):
    """Write the town's town-profile.yaml."""
    path = tmp_state_dir.parent / "town-profile.yaml"
    monkeypatch.setenv("TOWN_PROFILE_PATH", str(path))
    return path.write_text


def _queue_detail_reads(conn, n: int, *, fetching: bool = True) -> list[str]:
    uids = []
    for i in range(n):
        uid = f"d{i}" if fetching else f"n{i}"
        _insert_event(conn, uid, state="approved")
        if fetching:   # a detail page of its own → a scrape + an LLM call
            conn.execute("UPDATE events SET source_url = ? WHERE event_uid = ?",
                         (f"https://example.org/events/{uid}", uid))
        workqueue.enqueue(conn, uid, "read-detail")
        uids.append(uid)
    conn.commit()
    return uids


class _Clock:
    """A fake monotonic clock that each skill invocation advances."""

    def __init__(self, per_call: float):
        self.t = 0.0
        self.per_call = per_call
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self) -> float:
        return self.t

    def invoke(self, skill, entry, uid, *a, **k):
        self.calls.append((skill, uid, self.t))
        self.t += self.per_call
        return (1, "", "boom")   # fail: nothing cascades, the queue stays readable


def _pending(conn, action: str) -> int:
    """Unresolved, unclaimed rows with no attempt counted."""
    return conn.execute(
        "SELECT COUNT(*) FROM event_workqueue WHERE next_action = ? AND resolved_at IS NULL "
        "AND claimed_at IS NULL AND attempts = 0", (action,)).fetchone()[0]


class TestDrainSettings:
    def test_defaults_fetch_more_than_one_detail_page_per_drain(self, profile):
        profile("")
        s = orchestrator.drain_settings()
        assert s.read_detail_per_drain > 1
        # The budget plus one more skill run ends before the next 10-min tick.
        assert s.budget_seconds + orchestrator.SKILL_TIMEOUT_S <= 600

    def test_town_profile_overrides(self, profile):
        profile("workqueue_drain:\n  read_detail_per_drain: 3\n  budget_seconds: 300\n")
        s = orchestrator.drain_settings()
        assert (s.read_detail_per_drain, s.budget_seconds) == (3, 300)

    def test_bad_values_keep_defaults_and_bounds_hold(self):
        d = orchestrator.drain_settings({})
        assert orchestrator.drain_settings(
            {"workqueue_drain": {"read_detail_per_drain": "lots", "budget_seconds": None}}) == d
        s = orchestrator.drain_settings(
            {"workqueue_drain": {"read_detail_per_drain": 10_000, "budget_seconds": 99_999}})
        # A drain ends before workqueue.reap_stale_claims would take its claims.
        assert s.budget_seconds + orchestrator.SKILL_TIMEOUT_S < 15 * 60
        assert s.read_detail_per_drain <= orchestrator.MAX_READ_DETAIL_PER_DRAIN
        s = orchestrator.drain_settings({"workqueue_drain": {"read_detail_per_drain": 0}})
        assert s.read_detail_per_drain >= 1


class TestReadDetailThroughput:
    def test_fetches_up_to_the_town_cap_and_releases_the_rest(self, fresh_db, profile,
                                                              monkeypatch):
        profile("workqueue_drain:\n  read_detail_per_drain: 3\n")
        _queue_detail_reads(fresh_db, 5)
        clock = _Clock(per_call=1)
        monkeypatch.setattr(orchestrator, "_invoke_skill", clock.invoke)

        summary = orchestrator.drain_workqueue(fresh_db, clock=clock)

        assert len(clock.calls) == 3
        assert summary["per_action"]["read-detail"]["deferred"] == 2
        assert _pending(fresh_db, "read-detail") == 2   # back in the queue, no attempt counted

    def test_events_without_a_detail_page_do_not_count_against_the_cap(
            self, fresh_db, profile, monkeypatch):
        profile("workqueue_drain:\n  read_detail_per_drain: 1\n")
        _queue_detail_reads(fresh_db, 2)
        _queue_detail_reads(fresh_db, 3, fetching=False)
        clock = _Clock(per_call=1)
        monkeypatch.setattr(orchestrator, "_invoke_skill", clock.invoke)

        orchestrator.drain_workqueue(fresh_db, clock=clock)

        assert sorted(uid for _, uid, _ in clock.calls) == ["d0", "n0", "n1", "n2"]

    def test_the_hourly_rate_limit_still_bounds_claims(self, fresh_db, profile, monkeypatch):
        profile("workqueue_drain:\n  read_detail_per_drain: 6\n"
                "workqueue_rate_limits:\n  read-detail: 2\n")
        _queue_detail_reads(fresh_db, 5)
        clock = _Clock(per_call=1)
        monkeypatch.setattr(orchestrator, "_invoke_skill", clock.invoke)

        orchestrator.drain_workqueue(fresh_db, clock=clock)

        assert len(clock.calls) == 2

    def test_detail_fetches_start_only_in_the_first_half_of_the_budget(
            self, fresh_db, profile, monkeypatch):
        # Detail reads go first in the pass; holding them to half the budget
        # leaves the rest of the pipeline the other half.
        profile("workqueue_drain:\n  read_detail_per_drain: 6\n  budget_seconds: 300\n")
        _queue_detail_reads(fresh_db, 6)
        _insert_event(fresh_db, "v1", state="detail-read")
        workqueue.enqueue(fresh_db, "v1", "extract-venue")
        fresh_db.commit()
        clock = _Clock(per_call=100)
        monkeypatch.setattr(orchestrator, "_invoke_skill", clock.invoke)

        summary = orchestrator.drain_workqueue(fresh_db, clock=clock)

        fetched = [uid for skill, uid, _ in clock.calls if skill == "event-read-detail"]
        assert len(fetched) == 2                          # started at t=0 and t=100
        assert ("event-derive-venue", "v1", 200) in clock.calls
        assert summary["per_action"]["read-detail"]["deferred"] == 4
        assert _pending(fresh_db, "read-detail") == 4

    def test_no_skill_starts_once_the_budget_is_spent(self, fresh_db, profile, monkeypatch):
        profile("workqueue_drain:\n  budget_seconds: 120\n")
        for i in range(4):
            _insert_event(fresh_db, f"v{i}", state="detail-read")
            workqueue.enqueue(fresh_db, f"v{i}", "extract-venue")
        fresh_db.commit()
        clock = _Clock(per_call=60)
        monkeypatch.setattr(orchestrator, "_invoke_skill", clock.invoke)

        summary = orchestrator.drain_workqueue(fresh_db, clock=clock)

        assert [t for _, _, t in clock.calls] == [0, 60]
        assert summary["per_action"]["extract-venue"]["deferred"] == 2
        assert summary["budget_spent"] is True
        assert _pending(fresh_db, "extract-venue") == 2
