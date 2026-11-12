"""The daily town report and its alerts (#64): what the town did in the last
day, what needs the operator, and an immediate Telegram alert when
submissions stop or a cron job keeps failing — transition-only, repeated in
every report while it stands. Driven through the town-report CLI and the
heartbeat wrapper (tests/autonomy_town.py)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.autonomy_town import PROFILE, SKILLS, Town, load, soon

ARENA = "https://arena.example/events"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7, **kw):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live jazz.",
            "categories": ["music"], "tags": [], "source_url": None, **kw}


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def open_circuit_by_hand(town, reason="entity-ingest-ref-filter-ignored"):
    """What SLT's agent did on 2026-11-01: the circuit row written straight
    into the DB, no alert sent."""
    town.conn.execute(
        "INSERT INTO autonomy_state (key, value, updated_at) VALUES ('circuit', ?, ?)",
        (json.dumps({"reason": reason, "at": "2026-11-01T02:25:17+00:00",
                     "evidence": {"wrong_live_venue": "aleworx-stateline"}}),
         "2026-11-01T02:25:17+00:00"))
    town.conn.commit()


def hermes_run(town, job: str, stamp: str, *, failed: bool, body: str = "") -> None:
    """One Hermes cron output file, as $HERMES_HOME/cron/output/<job>/ holds it."""
    d = town.state.parent / "cron" / "output" / job
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stamp}.md").write_text(
        f"# Cron Job: Nightly digest\n\n**Job ID:** {job}\n**Run Time:** {stamp}\n"
        "**Mode:** no_agent (script)\n"
        + ("**Status:** script failed\n\nScript exited with code 1\nstderr:\n" if failed
           else "\n---\n\n")
        + (body or ("Traceback (most recent call last):\n"
                    "ModuleNotFoundError: No module named 'feedparser'" if failed else "{}")))


class TestTheDailyReport:
    def test_it_counts_the_day_saves_the_file_and_sends_a_short_telegram(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night"), concert("Old Show", days=-3)])
        town.run()
        slug = town.api.created_slug("events")
        town.api.approve("events", slug)
        town.poll()

        out = town.report("build", "--send")
        assert out["rc"] == 0
        path = Path(out["report_path"])
        assert path == town.state / "reports" / f"{today()}-analyst.md"
        md = path.read_text()
        events = md.split("## Events")[1].split("\n## ")[0]
        assert "found 2" in events and "submitted 1" in events and "published 1" in events
        assert "Jazz Night" in events
        assert "## Needs you\n- nothing" in md
        tg = town.telegrams[-1]
        assert "submitted 1" in tg and str(path) in tg and "needs you" not in tg.lower()

    def test_a_dedup_names_the_live_record_it_matched(self, town):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        town.conn.execute(
            "INSERT INTO discovery_candidates (candidate_uid, town_slug, kind, canonical_url, "
            "display_name, status, live_slug, first_seen_at, last_seen_at) VALUES "
            "('c1', 'slt', 'business', 'https://cuppa.example', 'Cuppa Coffee', 'already-live', "
            "'cuppa', ?, ?), ('c2', 'slt', 'business', 'https://new.example', 'New Cafe', "
            "'verified', NULL, ?, ?)", (now, now, now, now))
        md = Path(town.report("build")["report_path"]).read_text()
        entities = md.split("## Entities")[1].split("\n## ")[0]
        assert "discovered 2" in entities
        assert "Cuppa Coffee → cuppa" in entities

    def test_the_analyst_narrative_is_included(self, town, tmp_path):
        note = tmp_path / "narrative.md"
        note.write_text("Tried the library iCal; adopted it.")
        md = Path(town.report("build", "--narrative", str(note))["report_path"]).read_text()
        assert "Tried the library iCal; adopted it." in md.split("## Analyst notes")[1]

    def test_held_events_and_unconfirmed_cancellations_need_the_operator(self, town):
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        town.conn.execute("UPDATE events SET cancel_status = 'possibly-cancelled', "
                          "cancel_note = 'missing from every source'")
        md = Path(town.report("build")["report_path"]).read_text()
        needs = md.split("## Needs you")[1].split("\n## ")[0]
        assert "possibly cancelled (not acted on): Jazz Night" in needs


class TestCircuitAlerts:
    def test_a_circuit_opened_by_hand_is_alerted_once_and_repeated_in_every_report(self, town):
        open_circuit_by_hand(town)
        town.heartbeat()
        town.heartbeat()
        stopped = [t for t in town.telegrams if t.startswith("[slt] autonomous")]
        assert len(stopped) == 1
        assert "entity-ingest-ref-filter-ignored" in stopped[0]
        assert "aleworx-stateline" in stopped[0] and "autonomy resume" in stopped[0]

        for _ in range(2):
            md = Path(town.report("build", "--send")["report_path"]).read_text()
            needs = md.split("## Needs you")[1].split("\n## ")[0]
            assert "entity-ingest-ref-filter-ignored" in needs and "autonomy resume" in needs
            assert "entity-ingest-ref-filter-ignored" in town.telegrams[-1]
        assert len([t for t in town.telegrams if t.startswith("[slt] autonomous")]) == 1

        from engine import autonomy
        autonomy.close_circuit(town.conn)
        md = Path(town.report("build")["report_path"]).read_text()
        assert "## Needs you\n- nothing" in md

    def test_a_circuit_the_gate_opens_alerts_once(self, town):
        town.api.event_status = "published"
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()
        town.heartbeat()
        assert len([t for t in town.telegrams if "STOPPED" in t]) == 1

    def test_a_reopened_circuit_alerts_again(self, town):
        from engine import autonomy
        autonomy.open_circuit(town.conn, "rejection-rate", {"rate": 0.8})
        autonomy.close_circuit(town.conn)
        town.heartbeat()
        autonomy.open_circuit(town.conn, "rejection-rate", {"rate": 0.9})
        town.heartbeat()
        assert len([t for t in town.telegrams if "STOPPED" in t]) == 2


class TestCronJobAlerts:
    def test_two_failed_runs_in_a_row_alert_once_until_it_recovers(self, town):
        hermes_run(town, "nightly-digest", "2026-11-10_21-00-14", failed=True)
        town.heartbeat()
        assert not [t for t in town.telegrams if "nightly-digest" in t]

        hermes_run(town, "nightly-digest", "2026-11-11_21-00-01", failed=True)
        town.heartbeat()
        hermes_run(town, "nightly-digest", "2026-11-12_21-00-01", failed=True)
        town.heartbeat()
        [alert] = [t for t in town.telegrams if "nightly-digest" in t]
        assert "failed" in alert and "No module named 'feedparser'" in alert

        md = Path(town.report("build")["report_path"]).read_text()
        assert "nightly-digest" in md.split("## Needs you")[1].split("\n## ")[0]

        hermes_run(town, "nightly-digest", "2026-11-13_21-00-01", failed=False)
        town.heartbeat()
        assert "ok again" in town.telegrams[-1]
        md = Path(town.report("build")["report_path"]).read_text()
        assert "nightly-digest" not in md

    def test_a_failure_between_successes_is_not_alerted(self, town):
        hermes_run(town, "event-find-am", "2026-11-10_06-00-00", failed=True)
        hermes_run(town, "event-find-am", "2026-11-11_06-00-00", failed=False)
        hermes_run(town, "event-find-am", "2026-11-12_06-00-00", failed=True)
        town.heartbeat()
        assert not [t for t in town.telegrams if "event-find-am" in t]

    def test_a_sync_skipped_on_a_lock_twice_in_a_row_alerts(self, town, monkeypatch):
        from engine import lock
        mod = load(SKILLS / "entity-live-sync" / "sync.py", "town_entity_live_sync")
        monkeypatch.setattr("sys.argv", ["sync.py", "run"])
        monkeypatch.setattr(mod, "LOCK_WAIT_S", 0)  # a skip, not a 20-min wait

        def sync():
            town.conn.commit()
            with contextlib.redirect_stdout(io.StringIO()):
                assert mod.main() == 0

        with lock.acquire("workqueue-drain"):
            sync()
            assert not [t for t in town.telegrams if "entity-live-sync" in t]
            sync()
        [alert] = [t for t in town.telegrams if "entity-live-sync" in t]
        assert "skipped" in alert
        sync()
        assert "entity-live-sync" in town.telegrams[-1] and "ok again" in town.telegrams[-1]


class TestAnalystRunBudget:
    def test_a_run_is_budgeted_backed_up_and_finished(self, town):
        town.set_profile({**PROFILE, "analyst": {"max_firecrawl": 5}})
        start = town.report("budget", "start")
        assert start["rc"] == 0
        run_id = start["run_id"]
        assert start["budget"] == {"max_minutes": 90, "max_firecrawl": 5,
                                   "max_submissions": 150}
        [backup] = start["backups"]
        assert Path(backup).parent == town.state / "backups"
        copy = sqlite3.connect(backup)
        assert copy.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] > 0

        from engine import firecrawl_env
        for _ in range(3):
            firecrawl_env.headers()          # one per Firecrawl request
        town.seed("Arena", ARENA)
        town.listing(ARENA, [concert("Jazz Night")])
        town.run()

        status = town.report("budget", "status", str(run_id))
        assert status["used"]["firecrawl"] == 3
        assert status["used"]["submissions"] == 2      # the venue and the event
        assert status["remaining"]["firecrawl"] == 2
        assert status["remaining"]["submissions"] == 148
        assert 89 <= status["remaining"]["minutes"] <= 90

        report = town.report("build")["report_path"]
        done = town.report("budget", "finish", str(run_id), "--status", "ok",
                           "--report", report)
        assert done["status"] == "ok"
        row = town.conn.execute("SELECT status, report_path, used_json FROM analyst_runs "
                                "WHERE id = ?", (run_id,)).fetchone()
        assert row[0] == "ok" and row[1] == report
        assert json.loads(row[2])["firecrawl"] == 3

    def test_a_run_left_running_past_its_time_is_interrupted_and_reported(self, town):
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        town.conn.execute("INSERT INTO analyst_runs (started_at, status, budget_json) "
                          "VALUES (?, 'running', ?)", (old, json.dumps({"max_minutes": 90})))
        out = town.report("budget", "start")
        assert out["interrupted"] == [1]
        assert town.conn.execute("SELECT status FROM analyst_runs WHERE id = 1"
                                 ).fetchone()[0] == "interrupted"
        md = Path(town.report("build")["report_path"]).read_text()
        assert "analyst run #1 interrupted" in md

    def test_a_second_run_does_not_start_while_one_is_running(self, town):
        assert town.report("budget", "start")["rc"] == 0
        assert town.report("budget", "start")["rc"] == 1

    def test_backups_keep_the_last_seven(self, town):
        from engine import local_data
        for i in range(9):
            local_data.backup(stamp=f"20261101T00000{i}Z")
        kept = sorted(p.name for p in (town.state / "backups").iterdir())
        assert kept == [f"townloops-20261101T00000{i}Z.db" for i in range(2, 9)]


class TestLocalData:
    def test_a_direct_edit_is_logged_with_its_reason(self, town):
        mod = load(SKILLS / "local-data" / "local_data.py", "local_data_cli")
        town.conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            assert mod.main(["log", "--reason", "merged two duplicate venues",
                             "--table", "venues", "--rows", "2"]) == 0
        row = town.conn.execute("SELECT worker, action, target, detail FROM agent_activity "
                                "WHERE action = 'local-edit'").fetchone()
        assert tuple(row[:3]) == ("analyst", "local-edit", "venues")
        assert "merged two duplicate venues" in row[3] and "2 rows" in row[3]

    def test_the_committed_schema_reference_matches_the_migrations(self, tmp_path):
        from engine import db, local_data
        conn = db.connect(str(tmp_path / "fresh.db"))
        db.run_migrations(conn)
        committed = (SKILLS / "local-data" / "references" / "schema.md").read_text()
        assert committed == local_data.schema_markdown(conn), (
            "schema reference is stale — regenerate it: python skills/business/local-data/"
            "local_data.py schema --out skills/business/local-data/references/schema.md")
