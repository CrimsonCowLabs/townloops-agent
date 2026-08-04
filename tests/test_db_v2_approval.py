"""dashboard/db_v2.py — atomic approval+enqueue and the pending predicate.

set_event_approval must flip state→approved AND enqueue extract-venue in a
single transaction (connect() is autocommit, so it uses explicit BEGIN/COMMIT):
a failed enqueue must roll the approval back, never leaving an event `approved`
with no queue row. The "pending" surfaces must all gate on
state='metadata-extracted' so legacy event-ingest rows (state='found') don't
inflate the count.
"""

from __future__ import annotations

import pytest

import dashboard.db as dashboard_db
from dashboard import db_v2


_NOW = "2026-08-01T00:00:00+00:00"


@pytest.fixture
def dash_db(fresh_db, tmp_state_dir, monkeypatch):
    """Point dashboard.db (which caches _DB_PATH at import) at the migrated
    temp DB, and hand back the fresh_db connection for seeding."""
    monkeypatch.setattr(dashboard_db, "_DB_PATH",
                        str(tmp_state_dir / "townloops.db"))
    return fresh_db


def _insert_event(conn, uid, *, state, approval_status):
    conn.execute(
        "INSERT INTO events(event_uid, source, source_kind, name, date, date_day,"
        " first_seen_at, last_seen_at, content_hash, last_modified_at,"
        " state, approval_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, "seed:test", "html", "Test Event", _NOW, "2026-08-01",
         _NOW, _NOW, "hash-" + uid, _NOW, state, approval_status),
    )
    conn.commit()


class TestAtomicApproval:
    def test_approve_enqueues_extract_venue(self, dash_db):
        _insert_event(dash_db, "a1", state="metadata-extracted",
                      approval_status="pending")

        n = db_v2.set_event_approval("a1", "approved")
        assert n == 1

        ev = dash_db.execute(
            "SELECT state, approval_status FROM events WHERE event_uid='a1'"
        ).fetchone()
        assert ev["state"] == "approved"
        assert ev["approval_status"] == "approved"

        q = dash_db.execute(
            "SELECT COUNT(*) FROM event_workqueue "
            "WHERE event_uid='a1' AND next_action='extract-venue' "
            "AND resolved_at IS NULL"
        ).fetchone()[0]
        assert q == 1

    def test_enqueue_failure_rolls_back_the_approval(self, dash_db, monkeypatch):
        _insert_event(dash_db, "a2", state="metadata-extracted",
                      approval_status="pending")

        def boom(*a, **k):
            raise RuntimeError("enqueue exploded")
        monkeypatch.setattr(db_v2._workqueue, "enqueue", boom)

        with pytest.raises(RuntimeError):
            db_v2.set_event_approval("a2", "approved")

        # Approval must have rolled back — still pending, still metadata-extracted.
        ev = dash_db.execute(
            "SELECT state, approval_status FROM events WHERE event_uid='a2'"
        ).fetchone()
        assert ev["approval_status"] == "pending"
        assert ev["state"] == "metadata-extracted"
        q = dash_db.execute(
            "SELECT COUNT(*) FROM event_workqueue WHERE event_uid='a2'"
        ).fetchone()[0]
        assert q == 0

    def test_decline_does_not_enqueue(self, dash_db):
        _insert_event(dash_db, "a3", state="metadata-extracted",
                      approval_status="pending")
        db_v2.set_event_approval("a3", "declined")
        ev = dash_db.execute(
            "SELECT state FROM events WHERE event_uid='a3'"
        ).fetchone()
        assert ev["state"] == "declined"
        q = dash_db.execute(
            "SELECT COUNT(*) FROM event_workqueue WHERE event_uid='a3'"
        ).fetchone()[0]
        assert q == 0


class TestPendingPredicate:
    def test_pending_excludes_legacy_found_rows(self, dash_db):
        # One real pending event awaiting review, one legacy ingest row that
        # also has approval_status='pending' but state='found'.
        _insert_event(dash_db, "p1", state="metadata-extracted",
                      approval_status="pending")
        _insert_event(dash_db, "p2", state="found", approval_status="pending")

        rows = db_v2.events_by_approval("pending")
        uids = {r["event_uid"] for r in rows}
        assert uids == {"p1"}

        chips = db_v2.headline_chips()
        assert chips["pending"] == 1
