"""The submission gate's pending cap scales with moderation throughput (#64,
story 54): a town whose moderator decides hundreds a week isn't cut off at
an arbitrary 100. Unset, the cap is max(100, 3 × submissions the moderator
decided in the last 7 days); `autonomy.max_pending` in the profile still
overrides it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine import autonomy

AUTO = autonomy.Settings(mode="autonomous")


def submit(conn, n: int, status: str = "pending", days_ago: float = 1) -> None:
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    start = conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
    for i in range(n):
        conn.execute("INSERT INTO submissions (resource, slug, status, submitted_at, decided_at) "
                     "VALUES ('events', ?, ?, ?, ?)",
                     (f"e-{start + i}", status, at, None if status == "pending" else at))
    conn.commit()


def test_a_new_town_gets_the_floor(fresh_db):
    assert autonomy.pending_cap(fresh_db, AUTO) == 100


def test_the_cap_follows_last_weeks_decisions(fresh_db):
    submit(fresh_db, 60, "published")
    submit(fresh_db, 20, "rejected")
    submit(fresh_db, 10, "merged")
    submit(fresh_db, 500, "published", days_ago=9)   # older than the week: not counted
    assert autonomy.pending_cap(fresh_db, AUTO) == 3 * 90


def test_a_profile_value_overrides_the_scaling(fresh_db):
    submit(fresh_db, 200, "published")
    s = autonomy.settings({"operating_mode": "autonomous", "autonomy": {"max_pending": 40}})
    assert autonomy.pending_cap(fresh_db, s) == 40
    assert autonomy.settings({"operating_mode": "autonomous"}).max_pending is None


def test_the_gate_holds_at_the_scaled_cap_not_at_100(fresh_db):
    submit(fresh_db, 50, "published")         # cap 150
    submit(fresh_db, 120)                      # pending
    assert autonomy.submission_gate(fresh_db, AUTO).verdict == "submit"
    submit(fresh_db, 30)
    gate = autonomy.submission_gate(fresh_db, AUTO)
    assert (gate.verdict, gate.reason) == ("hold", "pending-cap")
    assert gate.evidence["max_pending"] == 150 and gate.evidence["pending"] == 150
    assert gate.evidence["scaled_from"] == {"decided_7d": 50}
