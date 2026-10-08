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
