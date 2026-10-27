---
name: newsletter-weekly
description: Compose the weekly events newsletter from TownLoops event data and push it to Listmonk as a scheduled campaign. Runs Sunday 08:00 compose (draft) → Sunday 20:00 push (schedules Mon 07:00 send). Ask before push — real subscribers.
---

# newsletter-weekly

Turns the coming week's published TownLoops events into an HTML campaign and hands it to Listmonk (`LISTMONK_URL`, `LISTMONK_LIST_ID`).

## Commands

```
$PYTHON $HERMES_HOME/skills/business/newsletter-weekly/newsletter.py compose --week 2026-W28
$PYTHON $HERMES_HOME/skills/business/newsletter-weekly/newsletter.py push    --week 2026-W28 [--send-at ISO]
$PYTHON $HERMES_HOME/skills/business/newsletter-weekly/newsletter.py show    --week 2026-W28
```

- `compose` — queries `GET /api/v1/towns/<slug>/events?from=<mon>&to=<sun>&status=published`, renders `template.html`, writes `state/newsletters/<week>.html`, logs `status='draft'` in the `newsletters` table. Prints `{"week", "events", "entries", "series", "singletons", "body"}` — `events` is raw occurrences, `entries` what the reader sees.
  - **Series collapse** (`engine/event_groups.py`): a Series appears once, as its next occurrence in the week (its own local description, as-is), with its `cadence_text`, "+N more dates this week", and an "All dates" hub link — only when `GET /series/<slug>` says `is_active` and gives a `url` (an archived hub 404s). Events without a `series_slug` are never merged, even under the same title.
- `push` — creates a Listmonk campaign from the draft body + schedules it. Default `send_at` = Monday of that week at 07:00 America/Los_Angeles (override with `--send-at`).
- `show` — dumps the newsletter row from state.

## Rhythm

| When | Job | Command |
|---|---|---|
| 08:00 Sunday | `newsletter-compose` | `compose --week <this-week>` — draft only. |
| 20:00 Sunday | `newsletter-push` | `push --week <this-week>` — schedules Mon 07:00 send. |

## Sponsors / ads in the newsletter

If `SPONSORS_ENABLED=true`, the template renders active `ad_spots` records where `placement IN ('newsletter-header', 'newsletter-footer')`. Populated by `sponsor-management` — dormant until then.

## MUST ask before push

Compose is safe (writes only to `state/`). `push` schedules a real send to real subscribers — always confirm with the user in Telegram before running it. The 20:00 Sunday cron does NOT auto-push; it will draft the push command and wait for the operator to run it.

## Env

- `TOWNLOOPS_API_URL`, `TOWNLOOPS_API_KEY`, `TOWNLOOPS_TOWN_SLUG`
- `LISTMONK_URL`, `LISTMONK_USER`, `LISTMONK_TOKEN`, `LISTMONK_LIST_ID`

Requires `jinja2` in the Python environment (included in the base `hermes-agent` image).
