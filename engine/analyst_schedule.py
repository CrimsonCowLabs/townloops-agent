"""When a town's daily analyst run starts (#64): towns sharing a host must
not all hit Firecrawl and Codex at once, but every town gets the repo's
cron/jobs.json verbatim. So bin/townloops-agent-add renders one value into
the town's copy — the `town-analyst` job's schedule:

    town-profile.yaml `analyst.schedule` (a 5-field cron expression), else
    a time derived from the slug: minute and hour from a hash, 08:00–10:59
    (after the morning event-find and the 03:00–05:00 jobs, before the
    nightly digest).

Nothing else in the file changes (a text substitution, so the formatting is
the repo's). Run on the host by the scaffolder (python3, stdlib — PyYAML when
present, else a line scan for the one key):

    python3 -m engine.analyst_schedule SRC_JOBS SLUG PROFILE  > DEST_JOBS
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


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: python3 -m engine.analyst_schedule SRC_JOBS SLUG PROFILE", file=sys.stderr)
        return 2
    src, slug, profile = argv
    sys.stdout.write(render(Path(src).read_text(), slug, read_profile(Path(profile))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
