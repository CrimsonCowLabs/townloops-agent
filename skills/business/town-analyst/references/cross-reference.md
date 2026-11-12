# Cross-reference against live (run order steps 4–6)

Nothing is created on TownLoops before it is looked up there. The pipeline
already does it — I check it did, and settle what it couldn't.

## What the pipeline matches on

- **Events** (event-ship, `engine/live_lookup`): Ingest Ref → slug → the
  predicted Series' same town-local day → `source_url` ±12h. Across my own
  seeds, copies of one event share a `canonical_event_uid` (one ships).
- **Venues** (event-derive-venue): local rows and live, by name.
- **Organisations** (business-discovery harvest): live by website (same
  site or under its path) or name similarity ≥0.85 → `already-live`, naming
  the live slug (`business-discovery list --status already-live`).
- **Entities already shipped** (`entity-live-sync run`): resync from live,
  re-point a merged row to its survivor, flag a 404 (`entity-live-sync list`).

## What I check

- `town-report build`'s Entities section names every dedup's live match:
  spot-check a few against the live record (`townloops-api describe venues`
  names the filters the endpoint takes; `townloops-api query venues --all`
  and match by name myself when it takes none).
- Two plausible live records for one local row → don't guess: a notebook
  line with both slugs and the evidence for each, resolved next run.
- A live event that a local pending one duplicates (same title, same local
  day, same venue) → decline the local copy (`autonomy decline UID --note
  "duplicate of live <slug>"` if it's held; otherwise mark it `declined`
  in SQL with the reason in `notes`, logged with `local-data log`).
- Duplicates *on live* are proposals for the operator in the report, never
  something I delete or merge (SOUL.md §Judgement).

## Reads I can always do

`townloops-api query <resource> --filter K=V [--all]` and `townloops-api
describe <resource>` — read-only, my town only. `operator-audit-upcoming
--days 14` lists days with no live event, local events not yet live and
sparse live metadata — where to look next.
