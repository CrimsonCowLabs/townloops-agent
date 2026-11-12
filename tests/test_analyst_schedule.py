"""Each town's daily analyst run starts at its own time (#64, story 5): the
scaffolder renders the `town-analyst` job's schedule into the town's copy of
cron/jobs.json — the profile's `analyst.schedule`, else a time derived from
the slug — and changes nothing else in the file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine import analyst_schedule as sched
from engine import analyst_schedule

ROOT = Path(__file__).resolve().parent.parent
JOBS = (ROOT / "cron" / "jobs.json").read_text()


def analyst_job(text: str) -> dict:
    return next(j for j in json.loads(text)["jobs"] if j["id"] == sched.JOB_ID)


def test_the_repo_schedules_one_daily_agent_job_running_the_skill():
    job = analyst_job(JOBS)
    assert not job.get("no_agent")
    assert job["skills"] == ["town-analyst"]
    assert (ROOT / "skills" / "business" / "town-analyst" / "SKILL.md").is_file()
    assert sched.valid(job["schedule"])


def test_the_slug_gives_a_stable_morning_time_that_differs_between_towns():
    times = {slug: sched.schedule_for(slug) for slug in ("slt", "truckee", "north-shore", "reno")}
    assert times == {slug: sched.schedule_for(slug) for slug in times}   # deterministic
    assert len(set(times.values())) == len(times)
    for expr in times.values():
        minute, hour, *rest = expr.split()
        assert rest == ["*", "*", "*"]
        assert 0 <= int(minute) < 60 and 8 <= int(hour) <= 10


def test_the_profile_overrides_the_derived_time():
    assert sched.schedule_for("slt", {"analyst": {"schedule": "45 13 * * *"}}) == "45 13 * * *"


@pytest.mark.parametrize("bad", ["", "every day", "61 9 * * *", "* * *", 7])
def test_a_bad_profile_schedule_is_ignored(bad):
    assert sched.schedule_for("slt", {"analyst": {"schedule": bad}}) == sched.schedule_for("slt")


def test_render_changes_only_the_analyst_schedule():
    out = sched.render(JOBS, "slt", {"analyst": {"schedule": "45 13 * * *"}})
    assert analyst_job(out)["schedule"] == "45 13 * * *"
    assert out.replace('"45 13 * * *"', f'"{analyst_job(JOBS)["schedule"]}"', 1) == JOBS
    assert sched.render(out, "slt", {"analyst": {"schedule": "45 13 * * *"}}) == out


def test_render_leaves_a_schedule_without_the_job_alone():
    empty = '{ "version": 1, "jobs": [] }\n'
    assert sched.render(empty, "slt", {}) == empty


class TestForHermes:
    """The repo's shorthand schedule → the shape Hermes' scheduler runs."""

    def _jobs(self, *jobs):
        return json.dumps({"jobs": list(jobs)})

    def test_every_repo_job_converts(self):
        repo = (ROOT / "cron" / "jobs.json").read_text()
        jobs = json.loads(analyst_schedule.for_hermes(repo))["jobs"]
        for job in jobs:
            assert job["schedule"]["kind"] == "cron" and job["schedule"]["expr"], job["id"]
            assert job["enabled"] is True and job["state"] == "scheduled"
            if job.get("no_agent"):
                assert (ROOT / "scripts" / job["script"]).is_file(), job["id"]
                assert "prompt" not in job
            else:
                assert job["prompt"]

    def test_a_script_job_runs_its_wrapper(self):
        out = json.loads(analyst_schedule.for_hermes(self._jobs(
            {"id": "beat", "schedule": "*/10 * * * *", "no_agent": True,
             "prompt": "python /home/agent/.hermes/scripts/workqueue_heartbeat.py"})))
        [job] = out["jobs"]
        assert job["script"] == "workqueue_heartbeat.py"
        assert job["schedule"] == {"kind": "cron", "expr": "*/10 * * * *"}
        assert job["schedule_display"] == "*/10 * * * *"

    def test_a_script_job_with_arguments_is_refused(self):
        with pytest.raises(ValueError, match="no arguments"):
            analyst_schedule.for_hermes(self._jobs(
                {"id": "x", "schedule": "0 1 * * *", "no_agent": True,
                 "prompt": "python /home/agent/.hermes/scripts/business_promote.py --limit 3"}))

    def test_run_state_is_kept_for_an_unchanged_job_only(self):
        repo = self._jobs({"id": "a", "schedule": "0 1 * * *", "no_agent": True,
                           "prompt": "python /home/agent/.hermes/scripts/event_find.py"},
                          {"id": "b", "schedule": "0 3 * * *", "prompt": "hi"})
        previous = json.dumps({"jobs": [
            {"id": "a", "schedule": {"kind": "cron", "expr": "0 1 * * *"},
             "next_run_at": "N", "last_status": "ok", "enabled": False, "state": "paused"},
            {"id": "b", "schedule": {"kind": "cron", "expr": "0 2 * * *"}, "next_run_at": "OLD"}]})
        a, b = json.loads(analyst_schedule.for_hermes(repo, previous))["jobs"]
        assert (a["next_run_at"], a["last_status"], a["enabled"], a["state"]) == \
            ("N", "ok", False, "paused")
        assert "next_run_at" not in b

    def test_the_analyst_job_stays_an_agent_job(self):
        out = analyst_schedule.for_hermes(analyst_schedule.render(
            (ROOT / "cron" / "jobs.json").read_text(), "slt"))
        job = next(j for j in json.loads(out)["jobs"] if j["id"] == "town-analyst")
        assert job["prompt"] and job["skills"] and "script" not in job
