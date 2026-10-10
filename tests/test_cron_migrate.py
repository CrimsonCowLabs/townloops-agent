"""Issue 25: every cron wrapper that leads to DB writes migrates the town DB
before it launches its skill, so a code update never meets an older schema."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Wrappers that need no migrate() of their own, and why. Any other
# scripts/*.py is a cron wrapper that must migrate before its skill runs.
NO_MIGRATE = {
    "onboarding_tick.py": "run_tick drains, which migrates",
    "onboarding_summary.py": "read-only digest",
    "api_check.py": "API snapshot, no DB",
    "import_cmo.py": "one-off import, migrates itself",
    "inspect_post_wait.py": "manual Firecrawl probe, no DB",
    "inspect_silent.py": "manual Firecrawl probe, no DB",
    "inspect_silent_deep.py": "manual Firecrawl probe, no DB",
}
WRAPPERS = sorted(p.name for p in (ROOT / "scripts").glob("*.py") if p.name not in NO_MIGRATE)


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(f"cron_migrate_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _migrated(db: Path) -> int:
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


@pytest.mark.parametrize("script", WRAPPERS)
def test_wrapper_migrates_before_launching_the_skill(script, tmp_state_dir, tmp_path, monkeypatch):
    db = tmp_state_dir / "townloops.db"
    wrapper = _load(ROOT / "scripts" / script)
    skill = tmp_path / "skill.py"
    skill.write_text("")
    monkeypatch.setattr(wrapper, "HARVEST_PATH" if hasattr(wrapper, "HARVEST_PATH") else "SKILL", skill)
    monkeypatch.setattr("sys.argv", [script])

    seen_at_launch: list[int] = []

    class FakePopen:
        pid = 4242

        def __init__(self, *a, **k):
            seen_at_launch.append(_migrated(db))

    def fake_execv(*a):
        seen_at_launch.append(_migrated(db))
        return 0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "execv", fake_execv)

    wrapper.main()

    n_files = len(list((ROOT / "engine" / "migrations").glob("*.sql")))
    assert seen_at_launch == [n_files]


# Docker jobs that may call a skill directly: they never touch the town DB.
DIRECT_OK = {"api-check": "TownLoops OpenAPI snapshot, no DB"}


def test_every_docker_script_job_runs_a_wrapper():
    """A job that calls a skill directly skips the wrapper's migrate()."""
    import json
    jobs = json.loads((ROOT / "cron" / "jobs.json").read_text())["jobs"]
    for job in jobs:
        if not job.get("no_agent") or job["id"] in DIRECT_OK:
            continue
        cmd = job["prompt"].split()
        assert cmd[0] == "python", job["id"]
        assert cmd[1].startswith("/home/agent/.hermes/scripts/"), job["id"]
        assert (ROOT / "scripts" / Path(cmd[1]).name).exists(), job["id"]


@pytest.mark.parametrize("script,argv,expected", [
    ("event_seed_discovery.py", [], ["discover", "--limit", "15"]),
    ("event_seed_discovery.py", ["probe", "--limit", "4"], ["probe", "--limit", "4"]),
    ("event_cancellations.py", [], ["apply"]),
    ("event_cancellations.py", ["apply", "--limit", "5"], ["apply", "--limit", "5"]),
])
def test_exec_wrappers_default_or_pass_args_through(script, argv, expected, tmp_state_dir, tmp_path,
                                                    monkeypatch):
    wrapper = _load(ROOT / "scripts" / script)
    skill = tmp_path / "skill.py"
    skill.write_text("")
    monkeypatch.setattr(wrapper, "SKILL", skill)
    monkeypatch.setattr("sys.argv", [script, *argv])
    execd: list[list[str]] = []
    monkeypatch.setattr(os, "execv", lambda python, args: execd.append(args))

    wrapper.main()

    assert execd[0][1:] == [str(skill), *expected]
