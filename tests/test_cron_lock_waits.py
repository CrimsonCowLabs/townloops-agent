"""Due daily/weekly jobs wait their turn for a shared lock instead of
skipping. SLT: entity-live-sync (03:30) skipped 13 of 15 days because the
10-min heartbeat drain held `workqueue-drain`; org harvest (Wed 03:00) was
skipped because business-promote (*/15) took `business-discovery` the same
second. The wait is bounded; a frequent job (promote) still skips at once."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from tests.autonomy_town import Town
from tests.test_autonomy_harvest import discovery, harvest  # noqa: F401 — fixture


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


class Holder:
    """Another process's run holding `name` (flock: a second open file in this
    process contends like one) until `release_after` seconds pass."""

    def __init__(self, name: str, release_after: float):
        from engine import lock
        held = threading.Event()

        def hold():
            with lock.acquire(name):
                held.set()
                time.sleep(release_after)
        self.thread = threading.Thread(target=hold, daemon=True)
        self.thread.start()
        assert held.wait(5)

    def __exit__(self, *exc):
        self.thread.join(10)

    def __enter__(self):
        return self


def test_acquire_waits_for_a_held_lock_to_be_released(tmp_state_dir):
    from engine import lock
    with Holder("shared", release_after=0.6):
        t0 = time.monotonic()
        with lock.acquire("shared", wait=10):
            assert time.monotonic() - t0 >= 0.4


def test_the_wait_is_bounded(tmp_state_dir):
    from engine import lock
    with Holder("shared", release_after=2):
        t0 = time.monotonic()
        with pytest.raises(lock.LockHeld):
            with lock.acquire("shared", wait=0.3):
                pass
        assert time.monotonic() - t0 < 1.5


def test_live_sync_runs_once_the_drain_releases_the_lock(town):
    with Holder("workqueue-drain", release_after=0.8):
        out = town.live_sync()
    assert "synced" in out and "series" in out       # ran, not "sync skipped"


def test_live_sync_still_skips_when_the_drain_outlasts_the_wait(town, monkeypatch, capsys):
    import importlib
    from tests.autonomy_town import SKILLS, load
    mod = load(SKILLS / "entity-live-sync" / "sync.py", "town_entity_live_sync_wait")
    monkeypatch.setattr(mod, "LOCK_WAIT_S", 0.2)
    monkeypatch.setattr("sys.argv", ["sync.py", "run"])
    with Holder("workqueue-drain", release_after=1.5):
        assert mod.main() == 0
    assert "sync skipped" in capsys.readouterr().out


def test_harvest_runs_once_promote_releases_the_lock(town, harvest):  # noqa: F811
    results, sites = harvest
    results["coffee shops"] = [{"url": "https://newcafe.example/", "title": "New Cafe"}]
    sites["https://newcafe.example"] = "New Cafe in South Lake Tahoe. See our menu and hours."
    with Holder("business-discovery", release_after=0.8):
        out = discovery(town, "harvest")
    assert "still active" not in out
    assert town.conn.execute("SELECT status FROM discovery_candidates WHERE display_name = "
                             "'New Cafe'").fetchone()[0] == "approved"


def test_promote_does_not_queue_behind_a_running_harvest(town):
    # Every 15 min: the next run comes soon, so a held lock skips this one.
    from engine import business_discovery as bd
    town.conn.execute(
        "INSERT INTO discovery_candidates (candidate_uid, town_slug, kind, canonical_url, "
        "display_name, source_signal, status, first_seen_at, last_seen_at) "
        "VALUES (?, 'slt', 'business', 'https://cafe.example', 'Cafe', 'web-search', "
        "'approved', '2026-11-01', '2026-11-01')", (bd.candidate_uid("slt", "https://cafe.example"),))
    town.conn.commit()
    with Holder("business-discovery", release_after=1.5):
        t0 = time.monotonic()
        out = discovery(town, "promote")
        assert time.monotonic() - t0 < 1.0
    assert "still active" in out
