---
name: series-candidates
description: Weekly read-only report of what a curator could do with TownLoops Series — title Clusters with no Series that look like a standing event, and Series that now span several venues or have same-day duplicates. Never creates, edits or archives a Series.
---

# series-candidates

A **Series** (TownLoops ADR 0007) is the evergreen hub page that gathers the
Events recurring under one name. Only a curator creates one — the API has no
write surface and the agent's client refuses one. This skill tells the
curator where one is worth creating, and where an existing one has drifted.
Logic: `engine/series.py` (`candidates`).

## What it reports

Over published live Events (90 days back to 180 ahead by default):

- **Clusters that could become a Series** — Events with no Series whose
  titles share a `series_key` (TownLoops' normalization), with **two or more
  distinct town-local dates at a single resolved Venue**: TownLoops'
  `cluster_verdict` rule. Rows whose venue never resolved don't count against
  it. Left out: one evening ingested twice (one date), a Cluster across
  several venues (a touring act), and a Cluster titled with a known
  performer's or creator's name.
- **Series that span several venues** — curator may have meant it (a
  festival across stages); otherwise run the Series audit.
- **Series with two or more occurrences on one date** — duplicate ingests
  that make the hub claim a false "daily" cadence; run the Series audit.

## Output

`state/reports/series-candidates-<date>.md`, a `report` row in the activity
ticker, and a Telegram summary when anything is listed. The nightly/autonomous
digest picks the report up. Refreshes `state/series.json` (the local Series
snapshot) as a side effect.

## Usage

    python report.py [--days-back 90] [--days-ahead 180]

Cron: weekly via `scripts/series_candidates.py` (migrates the DB first).
