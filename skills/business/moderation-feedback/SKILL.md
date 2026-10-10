---
name: moderation-feedback
description: Autonomous mode — hourly poll of what the TownLoops moderator did with each event and entity the agent submitted (published, rejected, merged, gone); re-points merged records, updates seed trust and Series rejection streaks, remembers rejected organisations, and stops submissions when the rejection rate passes the town's threshold.
---

# moderation-feedback

In autonomous mode every event and entity the agent creates on TownLoops lands
`pending` in the TownLoops admin moderation queue (`/admin/submissions`) and is
recorded in the local `submissions` table. This skill follows each one to its
outcome. Logic: `engine/moderation.py`.

## Outcomes

- **published** / **rejected** — the record's own `status` on a GET. An entity
  with no `status` field (TownLoops before the entity gate) counts as published.
- **merged** — the GET 404s and the record was folded into a published one: a
  date/venue change or a cancellation into the event it targeted, otherwise the
  one published event on the same day with the same `source_url` (or title), or
  the one published entity with the same name. The local row is re-pointed to
  the survivor, so later enrichment goes to the right record.
- **gone** — 404 and no survivor found: listed in the nightly digest.

## What it learns

- Each outcome is a `submission:<outcome>` decision in the activity log,
  attributed to the event's seed and Series (or the entity's discovery source).
- Seed trust (`event_seeds.trust_score`) is refreshed for every seed with a new
  outcome; the daily seed review retires seeds the moderator mostly rejects.
- A Series whose last `autonomy.series_reject_streak` submissions were all
  rejected is skipped by event-find until one is published.
- A rejected discovered organisation is remembered by name: discovery never
  proposes it again, under any URL.
- When the rejected share of recent outcomes passes
  `autonomy.circuit_breaker.reject_rate`, submissions stop (the circuit opens)
  and the operator is alerted. Collecting and enriching go on. Resume with
  `autonomy resume`.

## Usage

```bash
python moderation-feedback/poll.py [--limit 100]
```

Cron: `moderation-feedback` (hourly). A no-op when nothing is pending.
