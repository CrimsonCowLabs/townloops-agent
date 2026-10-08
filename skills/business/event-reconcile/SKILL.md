---
name: event-reconcile
description: Weekly re-pull of every approved event seed, cross-referenced against the live TownLoops catalog to catch date/venue changes and events missing from TownLoops; stages them locally for operator review, then enriches them (event image, venue, host business/community, entity images).
---

# event-reconcile

Weekly cron (Sunday 02:00, `event-reconcile-weekly`). Never writes to TownLoops —
everything lands in the local DB for the operator.

## What a run does

1. **Live snapshot** (read-only): `verify_town()`, then every live event from
   2 days ago to `--horizon-days` ahead (default 180), plus all live venues,
   businesses, communities.
2. **Pull**: every `approved` seed → Firecrawl markdown → Codex extraction
   (event-find's prompt plus `organizer`, `organizer_kind`, `image_url`).
3. **Cross-reference** each extraction (`engine/reconcile.py`):
   - match by canonical `source_url` (ignored when it is just the seed page), else
     by title similarity ≥ 0.88;
   - same local day → `unchanged`, or `changed` if the start moved > 30 min
     or the venue name differs from the live venue;
   - different day → `changed` (rescheduled) only for a one-off: the key matches
     exactly one live occurrence, it is unclaimed, not already past, and within
     60 days. Recurring series (several live occurrences) always count as `new`
     — a past occurrence is never moved onto a future date;
   - no match → `new`.
4. **Stage locally** — `new` and `changed` are upserted with the same `event_uid`
   as event-find (`seed + title + start`), at `state=metadata-extracted`,
   `approval_status=pending`, with `extra_json.reconcile` = `{kind, changes,
   live_slug, live_id, organizer, …}` and an `approval_note` the operator sees.
   The live slug stays in `extra_json.reconcile.live_slug`; `townloops_slug` is
   only set by event-ship once the PATCH lands (a row re-staged as `new` has a
   stale, never-shipped reconcile slug cleared). A `changed` extraction whose
   source content changed since last seen and that hits an already `shipped`
   (or `stuck-`) local row reopens it (fresh workqueue rows) to
   `metadata-extracted`/`pending` — dropping the old venue link on a venue change
   — so approval re-runs the pipeline and ship PATCHes live. `declined` rows are
   left alone, and so is a shipped row when only the live side differs (an edit
   made on TownLoops is never PATCHed back). `unchanged` events
   insert nothing; a pending event-find copy of one is flagged
   "already live … likely duplicate".
5. **Enrich** every row staged this run (max `--max-lookups` Firecrawl calls):
   - event: `image_url` + empty description from the detail page (og:image / JSON-LD);
   - venue and host (`organizer` → business or community): local name match →
     live TownLoops match (mirrored locally as `source_kind='live'` with
     `townloops_slug`, `extra_json.live_id`) → new local shell, website found via
     Firecrawl search, then address/phone/socials/description/`image_path`
     filled (fill blanks only). A lookup the budget cut short leaves the shell
     `pending` (researched next run, "research deferred" in the report); only a
     search that ran and found nothing records `not-found`;
   - a live entity with no image gets a locally staged `image_path` and
     nothing else (`enrichment_status='image-proposed'`), listed in the
     report. One whose site shows no image is `image-not-found` and is looked
     at again only after 90 days (`entity_enrich.LIVE_IMAGE_RETRY_DAYS`).
   Sets `events.derived_venue_uid` / `derived_host_uid`; event-derive-venue
   and event-derive-hosts keep those instead of re-deriving.
6. **Report**: `state/reports/reconcile-<date>.md`, a Telegram line if configured.

## Existing live-catalog adoption

When TownLoops already contains published events but the local event-state DB
is empty or incomplete, do **not** stage those records as new pending events.
Mirror them locally as terminal `approved` + `shipped` rows, populate
`townloops_slug` and pushed hashes, and leave unmatched local discoveries
pending. Dry-run matching and back up SQLite before the transaction.

See [`references/adopt-live-catalog.md`](references/adopt-live-catalog.md) for
the matching order, field mapping, transaction procedure, and post-import
invariants.

## After operator approval

The normal workqueue runs. For `changed` rows, `event-ship` **PATCHes** the live
event (`start_at`/`end_at`, `venue_name` + `venue_id`) instead of creating a
duplicate. Live-mirrored entities are never re-POSTed.

## CLI

```bash
python skills/business/event-reconcile/reconcile.py                 # = run
python skills/business/event-reconcile/reconcile.py run --dry-run   # classify + print, write nothing
python skills/business/event-reconcile/reconcile.py run --seed-id 9 --skip-enrich
python skills/business/event-reconcile/reconcile.py enrich          # re-enrich all pending staged rows
```

Flags: `--horizon-days N` (180), `--max-lookups N` (80); `run` also takes
`--seed-id`, `--limit`, `--dry-run`, `--skip-enrich`.

The cron wrapper (`scripts/event_reconcile.py`) detaches the run because a full
pass is much longer than Hermes' 120s cron script timeout. Log:
`state/logs/event-reconcile-<date>.log`. A detached supervisor waits on the run
and reports a nonzero exit (token expiry, API/pagination or migration error,
crash, kill) as a `run-failed` row on the dashboard ticker plus a Telegram line.
A flock (`event-reconcile`) keeps runs from overlapping; an overlapping run is
recorded as `skipped-overlap` and exits 0.
