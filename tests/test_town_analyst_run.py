"""The daily analyst run, scripted (#64, seam 2): a stand-in for the LLM
walks the town-analyst brief's run order — budget, review, hunt, events,
organisations, enrichment, a drain, the report — by calling the CLIs the
brief names, in-process against the fake TownLoops. It proves those tools
take those calls and that a run leaves what it must: its `analyst_runs` row
finished with the report's path, the report sent with the run's own notes,
submissions pending in the moderation queue, and the notebook carried over.

The real-LLM run is `pytest -m integration` (test_real_analyst_run)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.autonomy_town import ROOT, Town, load, soon
from tests.test_town_analyst_brief import BRIEF, brief_files, commands

ARENA = "https://arena.example/events"
LIBRARY = "https://library.example/calendar"


def concert(title, days=7, hour=19):
    return {"title": title, "date": soon(days, hour), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None}


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    t = Town(tmp_state_dir, monkeypatch, fresh_db)
    home = tmp_state_dir.parent / "hermes-home"
    (home / "analyst").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    t.home = home
    return t


class ScriptedAnalyst:
    """What a real run does, one tool call at a time: a CLI (`run`), SQL on
    the town DB, a file written. It checks its budget between steps, as the
    brief says."""

    def __init__(self, town: Town):
        self.town = town
        self.run_id = None
        self.budget_checks = []

    def run(self, skill: str, *argv: str) -> dict:
        out = self.town.cli(skill, *argv)
        assert out["rc"] == 0, (skill, argv, out)
        return out

    def sql(self, query: str, args: tuple = ()) -> list[dict]:
        conn = sqlite3.connect(self.town.state / "townloops.db")
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(query, args)]
            conn.commit()
            return rows
        finally:
            conn.close()

    def budget_ok(self) -> bool:
        status = self.run("town-report", "budget", "status", str(self.run_id))
        self.budget_checks.append(status)
        return not status["exhausted"]

    def drain_once(self) -> None:
        """`python /home/agent/.hermes/scripts/workqueue_heartbeat.py --drain`"""
        mod = load(ROOT / "scripts" / "workqueue_heartbeat.py", "analyst_heartbeat")
        self.town.conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            assert mod.main(["--drain"]) == 0

    def day(self) -> Path:
        home = self.town.home
        notebook = home / "analyst" / "notebook.md"
        # 1. Open the run; read the notebook.
        start = self.run("town-report", "budget", "start")
        self.run_id = start["run_id"]
        leads = notebook.read_text()
        # 2. Review yesterday.
        self.run("town-report", "build", "--hours", "24")
        self.run("town-report", "alerts")
        self.run("autonomy", "status")
        self.run("event-seed-discovery", "health")
        assert self.budget_ok()
        # 3. Hunt sources; trial them the same day.
        self.run("event-seed-discovery", "hunt", "--max-firecrawl", "40")
        self.run("event-seed-discovery", "review", "--trials", "8")
        self.run("event-seed-discovery", "adapters")
        assert self.budget_ok()
        # 4. Events: every approved seed not scraped today, then the holds.
        for seed in self.sql("SELECT seed_id FROM event_seeds WHERE status = 'approved' AND "
                             "(last_scraped_at IS NULL OR last_scraped_at < date('now'))"):
            self.run("event-find", "--seed-id", str(seed["seed_id"]))
        for held in self.sql("SELECT event_uid, title FROM events WHERE extra_json LIKE "
                             "'%held-for-review%'"):
            # Re-read the page: the 05:30 start is the race's real one.
            self.run("autonomy", "accept", held["event_uid"], "--note",
                     "the race page states a 5:30am start")
        assert self.budget_ok()
        # 5. Organisations and venues.
        self.run("business-discovery", "harvest", "--limit", "10")
        self.run("business-discovery", "list", "--status", "verified")
        # 6. Cross-reference and enrich; one direct fix of my own data, logged.
        self.run("entity-live-sync", "run", "--limit", "50")
        self.run("entity-live-sync", "list")
        self.run("performer-research", "run", "--limit", "3")
        self.run("performer-research", "review")
        fixed = self.sql("SELECT entity_uid FROM venues WHERE name = 'Lake Tahoe Arena'")
        self.sql("UPDATE venues SET website = 'https://arena.example' WHERE name = ?",
                 ("Lake Tahoe Arena",))
        self.run("local-data", "log", "--reason", "set Lake Tahoe Arena's own website",
                 "--table", "venues", "--rows", str(len(fixed)))
        assert self.budget_ok()
        # 7. The pipeline submits; one drain now.
        self.drain_once()
        # 8. The narrative, the report, the notebook, the run closed.
        narrative = home / "analyst" / f"narrative-{datetime.now(timezone.utc):%Y-%m-%d}.md"
        narrative.write_text("**Today:** adopted the library calendar (search); "
                             "accepted the 5:30am race start after reading its page.")
        report = self.run("town-report", "build", "--narrative", str(narrative), "--send")
        self.run("town-report", "budget", "finish", str(self.run_id), "--status", "ok",
                 "--report", report["report_path"])
        notebook.write_text(leads.replace("## Last run\n", f"## Last run\n- run #{self.run_id}: "
                                          "ok, library calendar adopted\n"))
        return Path(report["report_path"])


def test_a_scripted_run_walks_the_brief_and_leaves_a_report_and_a_finished_run(town):
    (town.home / "analyst" / "notebook.md").write_text(
        "# Analyst notebook — slt\n\n## Leads\n- library calendar? search for it\n\n"
        "## Last run\n")
    town.seed("Arena", ARENA)
    town.listing(ARENA, [concert("Jazz Night"), concert("Sunrise 5K", days=9, hour=5)])
    town.web["South Lake Tahoe library events"] = [
        {"url": LIBRARY, "title": "Library Calendar | Lake Library",
         "description": "Upcoming programs at the library."}]
    town.listing(LIBRARY, [concert("Story Time", days=3, hour=10),
                           concert("Book Club", days=10, hour=18)])

    analyst = ScriptedAnalyst(town)
    report = analyst.day()

    # The tools the brief names took every call; the run stayed in budget.
    assert all(not s["exhausted"] for s in analyst.budget_checks)
    # Submissions went to the moderation queue, pending.
    assert {"Jazz Night", "Sunrise 5K", "Story Time", "Book Club"} <= set(town.submitted_titles())
    assert {e["status"] for e in town.api.events.values()} == {"pending"}
    # The run is recorded, finished, with its report.
    [row] = town.conn.execute("SELECT status, report_path, used_json FROM analyst_runs").fetchall()
    assert row["status"] == "ok" and row["report_path"] == str(report)
    assert json.loads(row["used_json"])["submissions"] >= 4
    md = report.read_text()
    assert "adopted the library calendar" in md.split("## Analyst notes")[1]
    assert str(report) in town.telegrams[-1]
    # The direct edit is logged; the notebook carries over.
    assert town.conn.execute("SELECT COUNT(*) FROM agent_activity WHERE worker = 'analyst' "
                             "AND action = 'local-edit'").fetchone()[0] == 1
    notebook = (town.home / "analyst" / "notebook.md").read_text()
    assert "library calendar? search for it" in notebook and "run #1: ok" in notebook


def test_every_command_the_scripted_run_used_is_in_the_brief(town):
    """The stand-in only calls what the brief tells a real run to call."""
    town.seed("Arena", ARENA)
    town.listing(ARENA, [concert("Jazz Night")])
    (town.home / "analyst" / "notebook.md").write_text("# notebook\n\n## Last run\n")
    ScriptedAnalyst(town).day()

    def verb(skill, argv):
        return skill, (argv[0] if argv and not argv[0].startswith("--") else None)

    briefed = {verb(skill, tokens) for f in brief_files()
               for skill, tokens in commands(f.read_text())}
    used = {verb(c[0], list(c[1:])) for c in town.commands}
    assert used <= briefed, sorted(used - briefed)
    assert "workqueue_heartbeat.py --drain" in (BRIEF / "SKILL.md").read_text()


@pytest.mark.integration
def test_real_analyst_run():
    """Opt-in, inside a configured town's agent container (`docker compose
    exec agent …` with TOWNLOOPS_ANALYST_INTEGRATION=1): queue the real
    `town-analyst` cron job, run the scheduler once — a real LLM turn on the
    brief — and require that it opened and finished its own budget row."""
    if not os.environ.get("TOWNLOOPS_ANALYST_INTEGRATION"):
        pytest.skip("set TOWNLOOPS_ANALYST_INTEGRATION=1 in a logged-in town container")
    from engine.db import db_path_from_env
    hermes = shutil.which("hermes") or "/opt/hermes/.venv/bin/hermes"
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for verb in (["run", "town-analyst"], ["tick"]):
        proc = subprocess.run([hermes, "cron", *verb], capture_output=True, text=True,
                              timeout=2 * 60 * 60)
        assert proc.returncode == 0, proc.stderr[-2000:]
    conn = sqlite3.connect(db_path_from_env())
    row = conn.execute("SELECT status, report_path FROM analyst_runs WHERE started_at >= ? "
                       "ORDER BY id DESC LIMIT 1", (started,)).fetchone()
    assert row and row[0] in ("ok", "failed") and row[1], row
