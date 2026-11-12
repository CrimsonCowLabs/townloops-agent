"""When a town's daily analyst run starts (#64): towns sharing a host must
not all hit Firecrawl and Codex at once, but every town gets the repo's
cron/jobs.json verbatim. So bin/townloops-agent-add renders one value into
the town's copy — the `town-analyst` job's schedule:

    town-profile.yaml `analyst.schedule` (a 5-field cron expression), else
    a time derived from the slug: minute and hour from a hash, 08:00–10:59
    (after the morning event-find and the 03:00–05:00 jobs, before the
    nightly digest).

`render` changes nothing else (a text substitution). `for_hermes` then
writes the schedule in the shape Hermes' scheduler runs — the repo's file is
shorthand it doesn't schedule: a cron string becomes `{kind: cron, expr}`, and a
script job's `python …/scripts/<wrapper>.py` prompt its `script:` (Hermes runs a
script with no arguments, so a script job's prompt is the bare wrapper — the
wrapper carries the defaults). Hermes' run state (next/last run, pauses) is
carried over by job id from the town's previous file, so a rerender doesn't
reset it. Run on the host by the scaffolder (python3, stdlib — PyYAML when
present, else a line scan for the one key):

    python3 -m engine.analyst_schedule SRC_JOBS SLUG PROFILE [PREVIOUS_JOBS] > DEST_JOBS
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Optional

JOB_ID = "town-analyst"
FIRST_HOUR, HOURS = 8, 3          # 08:00–10:59

_FIELD = r"(\*|\d{1,2}(-\d{1,2})?(,\d{1,2}(-\d{1,2})?)*)(/\d{1,2})?"
_CRON = re.compile(rf"^{_FIELD}( {_FIELD}){{4}}$")
_LIMITS = (59, 23, 31, 12, 7)


def valid(expr) -> bool:
    """A plain 5-field cron expression with in-range numbers."""
    if not isinstance(expr, str) or not _CRON.match(expr.strip()):
        return False
    for field, top in zip(expr.split(), _LIMITS):
        if any(int(n) > top for n in re.findall(r"\d+", field.split("/")[0])):
            return False
    return True


def derived(slug: str) -> str:
    n = int(hashlib.sha256(slug.encode()).hexdigest(), 16)
    return f"{n % 60} {FIRST_HOUR + (n // 60) % HOURS} * * *"


def schedule_for(slug: str, profile: Optional[dict] = None) -> str:
    want = ((profile or {}).get("analyst") or {}).get("schedule")
    return " ".join(want.split()) if valid(want) else derived(slug)


def render(jobs_text: str, slug: str, profile: Optional[dict] = None) -> str:
    """`jobs_text` with the analyst job's schedule set for this town."""
    m = re.search(rf'"id"\s*:\s*"{re.escape(JOB_ID)}"', jobs_text)
    if not m:
        return jobs_text
    end = jobs_text.find("}", m.end())
    s = re.compile(r'("schedule"\s*:\s*)"[^"]*"')
    # The job's own object: its schedule key may come before or after the id.
    start = jobs_text.rfind("{", 0, m.start())
    body = jobs_text[start:end]
    if not s.search(body):
        return jobs_text
    body = s.sub(lambda x: f'{x.group(1)}{json.dumps(schedule_for(slug, profile))}', body, 1)
    return jobs_text[:start] + body + jobs_text[end:]


def read_profile(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        import yaml
        doc = yaml.safe_load(text)
        return doc if isinstance(doc, dict) else {}
    except ImportError:
        m = re.search(r"^analyst:\s*\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
        k = m and re.search(r"^\s+schedule:\s*[\"']?([^\"'#\n]+?)[\"']?\s*(#.*)?$",
                            m.group(1), re.MULTILINE)
        return {"analyst": {"schedule": k.group(1)}} if k else {}
    except Exception:
        return {}


SCRIPTS = "/home/agent/.hermes/scripts/"
# Hermes' own bookkeeping on a job, kept across a rerender.
RUN_STATE = ("enabled", "state", "paused_at", "paused_reason", "next_run_at", "last_run_at",
             "last_status", "last_error", "last_delivery_error", "repeat")


def _script(job: dict) -> str:
    """A script job's wrapper: `python /home/agent/.hermes/scripts/<name>.py`, nothing more."""
    words = (job.get("prompt") or "").split()
    if len(words) != 2 or words[0] != "python" or not words[1].startswith(SCRIPTS):
        raise ValueError(f"cron job {job.get('id')!r}: a script job's prompt must be "
                         f"`python {SCRIPTS}<wrapper>.py` (Hermes passes no arguments; "
                         f"put defaults in the wrapper), got {job.get('prompt')!r}")
    return words[1][len(SCRIPTS):]


def for_hermes(jobs_text: str, previous_text: Optional[str] = None) -> str:
    """The schedule as Hermes' scheduler stores and runs it."""
    doc = json.loads(jobs_text)
    previous = {}
    if previous_text:
        try:
            previous = {j.get("id"): j for j in json.loads(previous_text).get("jobs", [])}
        except (ValueError, AttributeError):
            previous = {}
    out = []
    for job in doc.get("jobs", []):
        job = dict(job)
        expr = job.get("schedule")
        if isinstance(expr, str):
            job["schedule"] = {"kind": "cron", "expr": expr}
            job["schedule_display"] = expr
        if job.get("no_agent"):
            job["script"] = _script(job)
            job.pop("prompt", None)
        job.setdefault("enabled", True)
        job.setdefault("state", "scheduled")
        old = previous.get(job.get("id")) or {}
        if old.get("schedule") == job["schedule"]:
            job.update({k: old[k] for k in RUN_STATE if k in old})
        out.append(job)
    doc["jobs"] = out
    return json.dumps(doc, indent=2) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print("usage: python3 -m engine.analyst_schedule SRC_JOBS SLUG PROFILE [PREVIOUS_JOBS]",
              file=sys.stderr)
        return 2
    src, slug, profile = argv[:3]
    previous = None
    if len(argv) == 4:
        try:
            previous = Path(argv[3]).read_text()
        except OSError:
            previous = None
    try:
        text = for_hermes(render(Path(src).read_text(), slug, read_profile(Path(profile))),
                          previous)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 3
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
