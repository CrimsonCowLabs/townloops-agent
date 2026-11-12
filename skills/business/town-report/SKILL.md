---
name: town-report
description: The daily town report (what the town found, deduped, enriched, submitted, held and failed in the last day, with a "Needs you" section first), the standing alerts, and the analyst run's budget — start a run (backs up the DBs), check what remains, finish it.
---

# town-report

One report per day per town, built from the town DB alone, saved as
`state/reports/<date>-analyst.md` and (with `--send`) sent to Telegram as a
short message: the counts, what needs the operator, and the file's path.
Logic: `engine/town_report.py` (sections), `engine/alerts.py` (standing
alerts — the one place), `engine/analyst_runs.py` (budget).

## Usage

```bash
python town-report/report.py build --send                       # last 24h, file + Telegram
python town-report/report.py build --hours 48                   # a wider window
python town-report/report.py build --narrative /tmp/notes.md --send   # with your own write-up
python town-report/report.py alerts                             # standing alerts as JSON

python town-report/report.py budget start                       # → {"run_id": N, "budget": …, "backups": […]}
python town-report/report.py budget status N                    # used + remaining (minutes, firecrawl, submissions)
python town-report/report.py budget finish N --status ok --report state/reports/<date>-analyst.md
```

## The report

- **Needs you** — only what the operator must act on: an open circuit (reason,
  evidence, `autonomy resume` once fixed), a breaking API change
  (`state/api_freeze.json`), a pause, a cron job failing or skipped twice in a
  row, events held for review, cancellations awaiting confirmation. Repeated
  in every report while it stands.
- **Standing alerts** — source-health alerts, a failed or interrupted analyst run.
- **Analyst notes** — the `--narrative` file, verbatim.
- **Sources** found / trialled / adopted / rejected; **Events** found / judged /
  submitted / shipped and the moderator's published / merged / rejected;
  **Entities** discovered, deduped against live (each naming the live slug it
  matched), enriched (with an image); **Holds**; **Failures** (stuck events,
  workqueue errors, failed source runs); **Analyst runs**.

`build` first runs the immediate-alert tick (as the heartbeat does every 10
minutes): one Telegram alert when the circuit opens — also a circuit written
straight into the DB — and one when a cron job fails or is skipped on two
runs in a row (`engine/job_health`, read from Hermes' `cron/output/` and from
skills that skip on a lock), "ok again" when it recovers.

## Budget

`town-profile.yaml`:

```yaml
analyst:
  max_minutes: 90        # wall time
  max_firecrawl: 300     # Firecrawl requests (counted per request, engine/firecrawl_env)
  max_submissions: 150   # creates/changes sent to TownLoops (`submissions`)
```

`start` backs up the state DBs (`local-data backup`), marks a run left
`running` past its `max_minutes` as `interrupted` (reported until a later run
ends ok), and refuses (exit 1) while another run is still within its time.
Check `status` between steps and stop when a `remaining` value reaches 0
(`exhausted` lists them). Always `finish`, `--status failed` when the run
could not do its work.
