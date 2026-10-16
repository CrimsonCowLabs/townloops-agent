"""Source health: every event-find run of a seed is recorded with its event
count; an established seed whose latest successful run lists nothing, or
far fewer events than its trailing average, raises one operator alert that
holds until the count recovers or the operator accepts the new level."""

from __future__ import annotations

import sqlite3

import pytest

from engine import source_health as sh

SETTINGS = {"window": 4, "min_runs": 3, "drop_pct": 50, "min_baseline": 4}


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
        "VALUES ('Riva Grill', 'https://riva.example/events', 'venue', 'approved', 'x', 'x')")
    return fresh_db


@pytest.fixture
def seed(conn):
    return dict(conn.execute("SELECT * FROM event_seeds").fetchone())


_clock = iter(range(10_000))


def _run(conn, seed, count, ok=True):
    """One event-find run of `seed` that listed `count` events."""
    t = f"2027-03-01T00:{next(_clock):05d}"
    return sh.record_run(conn, seed, started=t, ended=t, ok=ok, fetched=count,
                         error=None if ok else "firecrawl down", settings=SETTINGS)


def _runs(conn, seed, *counts):
    return [_run(conn, seed, c) for c in counts]


def test_every_run_is_recorded_with_its_count(conn, seed):
    _runs(conn, seed, 12, 9)
    _run(conn, seed, 0, ok=False)
    rows = conn.execute("SELECT ok, fetched FROM source_runs WHERE source = 'seed:Riva Grill' "
                        "ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, 12), (1, 9), (0, 0)]


def test_steady_source_raises_nothing(conn, seed):
    assert _runs(conn, seed, 10, 12, 9, 11, 10, 8) == [None] * 6
    assert sh.flagged(conn) == []


def test_drop_to_zero_alerts_once(conn, seed):
    _runs(conn, seed, 10, 12, 9)
    alert = _run(conn, seed, 0)
    assert alert["status"] == "zero" and alert["seed"] == "Riva Grill"
    assert "0 events" in alert["note"] and "10.3" in alert["note"]
    assert _runs(conn, seed, 0, 0) == [None, None]          # no repeat alerts
    assert [s["name"] for s in sh.flagged(conn)] == ["Riva Grill"]


def test_sharp_drop_alerts_but_ordinary_wobble_does_not(conn, seed):
    _runs(conn, seed, 20, 20, 20)
    assert _run(conn, seed, 11) is None                     # 45% down
    alert = _run(conn, seed, 8)                             # well under half the average
    assert alert["status"] == "drop"


def test_new_source_is_not_judged(conn, seed):
    # Fewer than min_runs successful runs behind it: no baseline yet.
    assert _runs(conn, seed, 10, 10, 0) == [None, None, None]


def test_small_source_only_alerts_on_zero(conn, seed):
    _runs(conn, seed, 3, 2, 3)
    assert _run(conn, seed, 1) is None                      # baseline under min_baseline
    assert _run(conn, seed, 0)["status"] == "zero"


def test_failed_runs_are_neither_zero_nor_history(conn, seed):
    _runs(conn, seed, 10, 10, 10)
    for _ in range(3):
        assert _run(conn, seed, 0, ok=False) is None
    assert sh.flagged(conn) == []
    assert _run(conn, seed, 10) is None


def test_recovery_clears_the_alert(conn, seed):
    _runs(conn, seed, 10, 10, 10, 0)
    assert _run(conn, seed, 3) is None                      # still far below the baseline
    back = _run(conn, seed, 9)
    assert back["status"] == "recovered"
    assert sh.flagged(conn) == []


def test_lasting_drop_stays_flagged_once_history_catches_up(conn, seed):
    # The trailing average sinking toward the new level doesn't clear it.
    _runs(conn, seed, 10, 10, 10, 2)
    assert _runs(conn, seed, 2, 2, 2, 2, 2) == [None] * 5
    assert [s["health_status"] for s in sh.flagged(conn)] == ["drop"]


def test_operator_accepts_the_new_level(conn, seed):
    _runs(conn, seed, 10, 10, 10, 2)
    assert sh.ack(conn, seed["seed_id"]) is True
    assert sh.flagged(conn) == []
    # Only runs after the ack count toward the new baseline.
    assert _runs(conn, seed, 2, 3, 2, 2) == [None] * 4
    assert _run(conn, seed, 0)["status"] == "zero"
    assert sh.ack(conn, 999) is False


def test_settings_come_from_the_town_profile():
    got = sh.settings_from({"source_health": {"drop_pct": 70}})
    assert got["drop_pct"] == 70 and got["window"] == sh.DEFAULTS["window"]
    assert sh.settings_from({}) == sh.DEFAULTS


def test_alert_text_names_the_town_and_each_seed():
    text = sh.alert_text("slt", [{"seed": "Riva Grill", "status": "zero", "note": "listed 0 events"},
                                 {"seed": "Chamber", "status": "recovered", "note": "back to 9"}])
    assert text.startswith("[slt] source health")
    assert "Riva Grill" in text and "Chamber" in text


class TestEventFindWiring:
    """event-find records each seed's run, judges it, and tells the operator
    (Telegram + the dashboard activity feed) about changes only."""

    def _find(self, tmp_state_dir, monkeypatch, pages, sent):
        import argparse
        import importlib.util
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "skills/business/event-find/find.py"
        spec = importlib.util.spec_from_file_location("event_find_health", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
        monkeypatch.setattr(mod, "_load_town_profile", lambda: {"source_health": SETTINGS})
        monkeypatch.setattr(mod, "_firecrawl_listing", lambda url: (url in pages, "md", "down"))
        monkeypatch.setattr(mod, "_build_extraction_prompt", lambda p, name, url, md: url)
        monkeypatch.setattr(mod, "_codex_exec", lambda url: (
            True, pages[url] if isinstance(pages[url], str) else json.dumps(pages[url]), ""))
        monkeypatch.setattr(mod, "_telegram", sent.append)
        monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")

        def run(dry_run=False):
            return mod.cmd_find(argparse.Namespace(seed_id=None, limit=None, dry_run=dry_run))
        return run

    def _events(self, n):
        return [{"title": f"Show {i}", "date": f"2027-04-{i + 1:02d}T20:00:00"} for i in range(n)]

    def test_source_going_quiet_alerts_the_operator_once(self, conn, tmp_state_dir, monkeypatch):
        conn.commit()
        url = "https://riva.example/events"
        pages, sent = {url: self._events(10)}, []
        run = self._find(tmp_state_dir, monkeypatch, pages, sent)
        for _ in range(3):
            run()
        assert sent == []
        pages[url] = []
        run()
        run()
        assert len(sent) == 1 and "Riva Grill" in sent[0] and "listed nothing" in sent[0]
        assert [s["health_status"] for s in sh.flagged(conn)] == ["zero"]
        acts = conn.execute("SELECT target FROM agent_activity WHERE action = 'source-health'").fetchall()
        assert [a[0] for a in acts] == ["Riva Grill"]

    def test_dry_run_does_not_judge(self, conn, tmp_state_dir, monkeypatch):
        conn.commit()
        url = "https://riva.example/events"
        pages, sent = {url: self._events(10)}, []
        run = self._find(tmp_state_dir, monkeypatch, pages, sent)
        for _ in range(3):
            run()
        pages[url] = []
        run(dry_run=True)
        assert sent == [] and sh.flagged(conn) == []
        # …and isn't history either.
        assert conn.execute("SELECT COUNT(*) FROM source_runs").fetchone()[0] == 3

    def test_unparseable_extraction_is_not_a_zero(self, conn, tmp_state_dir, monkeypatch):
        conn.commit()
        url = "https://riva.example/events"
        pages, sent = {url: self._events(10)}, []
        run = self._find(tmp_state_dir, monkeypatch, pages, sent)
        for _ in range(3):
            run()
        pages[url] = "Sorry, I couldn't read that page."
        run()
        assert sent == [] and sh.flagged(conn) == []
        pages[url] = "Here are the events:\n[]"     # the page really lists nothing
        run()
        assert len(sent) == 1 and "listed nothing" in sent[0]
