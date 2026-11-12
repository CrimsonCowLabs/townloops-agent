# Enrichment (run order step 6)

More data, never worse data: every value is quoted or copied from a page I
can cite; null beats invented (SOUL.md §Judgement).

## The verbs

- `entity-live-sync run [--limit N] [--kind venue|business|community|performer|creator]`
  — local enrichment into live blanks (fill-blank PATCH + GET verify; never
  overwrites a set live value). `entity-live-sync list` shows rows flagged
  for the operator; `entity-live-sync clear` only after I fixed the cause.
- `event-enrich-entities --event-uid UID` — re-run one event's venue / host
  enrichment (budgeted local → live → research). Use it after I corrected
  an entity's website or name.
- `performer-research run --limit N` — the evidence ladder for performers and
  creators (needs working search; if search returns nothing, say so in the
  report once instead of re-running it). `performer-research review` lists
  ambiguous identities; `performer-research resolve --kind performer
  --entity-uid UID --accept URL` (or `--reject`) only when a page I read
  proves it — the same bar the ladder uses.

## Where blanks hide

- `operator-audit-upcoming --days 14` — live events missing description,
  venue, source_url or categories; venues of upcoming events with no image.
- SQL: entities with no `website`, `image_url` or category
  (schema: `local-data/references/schema.md`), most-used first.
- An entity left `not-found` on an earlier day: try its own site, a linked
  social profile, the venue page that named it — then set its fields
  directly (logged) or let the next `entity-live-sync run` push them.

## Images

Official og:image → the official hero/facility photo → the parent
property's image. Never logos, icons, city banners, placeholders or a
listing site's photo. Else null, and the entity goes on my notebook list.
