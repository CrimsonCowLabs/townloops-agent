---
name: operator-audit-upcoming
description: Read-only audit of the town's next N days on TownLoops — days with no events, local events not live yet, live events with sparse metadata, and venues of upcoming events missing an image.
---

# operator-audit-upcoming

## When to use

The operator asks about the quality or coverage of what's coming up: "audit
the next 30 days", "what's missing this week?", "which upcoming events have no
venue?", "which venues need images?", "are there empty days next week?".

Read-only. It only LISTs/GETs TownLoops and reads the local DB; it proposes
nothing and changes nothing. Fixes go through the normal paths (operator
approve, enrichment, event-ship PATCH) — say which one when reporting.

## CLI

```bash
A=$HERMES_HOME/skills/business/operator-audit-upcoming/audit.py
$PYTHON $A                          # next 30 days
$PYTHON $A --days 7
$PYTHON $A --days 14 --limit 20
```

`$PYTHON` is `/usr/bin/python3`, the system Python that has the skill's
dependencies; there is no bare `python` on the container's PATH.

- `--days`: window in whole town-local days starting today (`$AGENT_TZ`), 1–90, default 30.
- `--limit`: max rows in each list, 1–200, default 50. Counts and `total`s are never capped.

## Output

```json
{
  "window": {"from": "2026-10-08", "to": "2026-11-06", "days": 30, "tz": "America/Los_Angeles"},
  "live_events": 182,
  "live_groups": 61,
  "gaps": {
    "empty_days": ["2026-10-21"],
    "not_live": {"counts": {"pending_review": 4, "in_pipeline": 2, "stuck": 1}, "total": 7,
                 "events": [{"event_uid": "…", "title": "…", "date": "…", "state": "…",
                             "source": "…", "reason": "pending_review"}]}
  },
  "coverage": {"days": [{"date": "2026-10-08", "groups": 3, "series": 2, "singletons": 1}],
               "series_only_days": ["2026-10-13"]},
  "sparse_metadata": {"counts": {"description": 3, "venue": 9, "source_url": 0, "categories": 1},
                      "total": 11,
                      "events": [{"slug": "…", "title": "…", "date": "2026-10-09",
                                  "source_url": "…", "missing": ["venue"],
                                  "series_slug": "trivia-thursday", "occurrences": 4}]},
  "venues_missing_images": {"total": 5, "unverified": [],
                            "venues": [{"venue_slug": "…", "name": "…", "website": "…",
                                        "events_in_window": 6, "groups_in_window": 2,
                                        "next_event": "2026-10-09"}]}
}
```

- `gaps.empty_days` — town-local days with no live event. A gap is a lead, not a
  defect: quiet weekdays are normal off-season.
- `gaps.not_live` — local events dated in the window that aren't on TownLoops:
  `pending_review` (operator hasn't approved), `in_pipeline` (approved, the
  heartbeat will ship it), `stuck` (`operator-requeue` after checking why).
- **Series count once** (grouped on `series_slug`, never title): `live_groups`
  is distinct Series + one-offs; `coverage.days` counts groups per local day,
  and `series_only_days` are days whose every group is a Series — one-off
  coverage is thin there.
- `sparse_metadata` — live events missing `description`, `venue` (no venue
  binding), `source_url`, or `categories`, **one finding per group**: the row
  is the group's first affected occurrence, `missing` the union across it,
  `occurrences` how many are affected. `counts` count findings, not dates.
- `venues_missing_images` — venues with upcoming events and no `image_path`,
  busiest first (`groups_in_window`, then raw `events_in_window`). Events have
  no image field; the site shows the venue's.
  `unverified` lists venue slugs the venue listing didn't return.

## Reading the result (the CMO's rules)

- The window is local days. TownLoops filters on UTC, so the skill fetches a day
  past the end and re-buckets by local date; don't "fix" an apparent off-by-one
  by re-querying the API with a shifted `to`.
- **Null-venue clusters:** group `missing: ["venue"]` rows by `source_url`. A
  whole calendar-style source going venue-less is a linking/truncation problem
  (`Noel's Coffee a`, `@Heaven`, `B`), not many bad events. Only a full venue
  name confirmed on the current source page for that date is safe; fragments
  stay for review. "Various locations" / work-site-varies events are
  venue-less by design. See `references/upcoming-audit-playbook.md`.
- **Venue images:** official og:image → official hero/facility photo → the
  parent property's image for a room inside it (say so). Reject logos,
  social icons, generic city banners and "Image Unavailable" placeholders.
  No good image → leave it null and list the slug. Null beats generic.
- **Descriptions:** fill from the official event page's own text, or reuse a
  verified sibling occurrence's description for the same series. Never write
  marketing copy.
- Report which counts are **live** and which are **local** — the operator
  needs to know what is actually on TownLoops.
- Big windows: summarise counts first; offer to walk lists in batches (SOUL.md
  §Batching).
