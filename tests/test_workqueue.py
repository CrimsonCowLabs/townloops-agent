"""engine/workqueue.py — enqueue/claim/fail plus the stale-claim reaper.

The reaper (reap_stale_claims) is the fix for orphaned claims: `claim()`
commits `claimed_at` before the orchestrator runs the skill, so a drainer that
dies mid-run leaves a row that can neither be re-claimed nor surfaced. The
reaper routes each stale claim through `fail()`, so it re-enters the queue and,
after MAX_ATTEMPTS, becomes a visible `stuck-` event.
"""

from __future__ import annotations

import pytest

from engine import workqueue


_NOW = "2026-08-01T00:00:00+00:00"


def _insert_event(conn, uid, *, state="approved", approval_status="approved"):
    conn.execute(
        "INSERT INTO events(event_uid, source, source_kind, name, date, date_day,"
        " first_seen_at, last_seen_at, content_hash, last_modified_at,"
        " state, approval_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "seed:test", "html", "Test Event", _NOW, "2026-08-01",
         _NOW, _NOW, "hash-" + uid, _NOW, state, approval_status),
    )
    conn.commit()


def _orphan_claim(conn, uid, action="extract-venue", *, attempts=0,
                  claimed_at="2000-01-01T00:00:00+00:00"):
    """Simulate a claimed-but-never-resolved row (worker died)."""
    workqueue.enqueue(conn, uid, action)
    conn.execute(
        "UPDATE event_workqueue SET claimed_by='dead-worker', claimed_at=?, "
        "attempts=? WHERE event_uid=? AND next_action=?",
        (claimed_at, attempts, uid, action),
    )
    conn.commit()


class TestWorkerId:
    def test_prefers_agent_hostname_env(self, monkeypatch):
        monkeypatch.setenv("TOWNLOOPS_AGENT_HOSTNAME", "townloops-agent-macneo")
        assert workqueue._worker_id().startswith("townloops-agent-macneo:")

    def test_falls_back_to_gethostname(self, monkeypatch):
        monkeypatch.delenv("TOWNLOOPS_AGENT_HOSTNAME", raising=False)
        import socket
        assert workqueue._worker_id().startswith(socket.gethostname() + ":")


class TestReapStaleClaims:
    def test_reclaims_and_increments_attempts(self, fresh_db):
        _insert_event(fresh_db, "e1")
        _orphan_claim(fresh_db, "e1", attempts=0)

        reaped = workqueue.reap_stale_claims(fresh_db, older_than_minutes=15)
        assert reaped == 1

        row = fresh_db.execute(
            "SELECT attempts, claimed_at, resolved_at, last_error "
            "FROM event_workqueue WHERE event_uid='e1'"
        ).fetchone()
        assert row["attempts"] == 1
        assert row["claimed_at"] is None       # claim cleared → re-claimable
        assert row["resolved_at"] is None      # still below MAX_ATTEMPTS
        assert "stale claim" in (row["last_error"] or "")

    def test_fresh_claim_is_not_reaped(self, fresh_db):
        _insert_event(fresh_db, "e2")
        workqueue.enqueue(fresh_db, "e2", "extract-venue")
        # Claimed "now" per SQLite's clock → within the window, left alone.
        fresh_db.execute(
            "UPDATE event_workqueue SET claimed_by='live', "
            "claimed_at=datetime('now') WHERE event_uid='e2'"
        )
        fresh_db.commit()
        assert workqueue.reap_stale_claims(fresh_db, older_than_minutes=15) == 0

    def test_reaping_to_max_attempts_marks_event_stuck(self, fresh_db):
        _insert_event(fresh_db, "e3", state="approved")
        # One reap away from MAX_ATTEMPTS.
        _orphan_claim(fresh_db, "e3", attempts=workqueue.MAX_ATTEMPTS - 1)

        workqueue.reap_stale_claims(fresh_db, older_than_minutes=15)

        wq = fresh_db.execute(
            "SELECT attempts, resolved_at FROM event_workqueue WHERE event_uid='e3'"
        ).fetchone()
        assert wq["attempts"] == workqueue.MAX_ATTEMPTS
        assert wq["resolved_at"] is not None   # row resolved

        ev = fresh_db.execute(
            "SELECT state FROM events WHERE event_uid='e3'"
        ).fetchone()
        assert ev["state"] == "stuck-approved"  # surfaces to the operator
