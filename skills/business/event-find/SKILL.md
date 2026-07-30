# event-find

Daily pass that scrapes every `approved` event_seed and uses the LLM
(Codex CLI) to extract a JSON list of upcoming events from each seed's
page. Events land directly at `state=metadata-extracted`,
`approval_status=pending` so the operator can approve/decline in the
dashboard.

## Why one skill does find+extract in v1

The spec has separate `event-find` (populate) and `event-extract-metadata`
(deep enrichment) skills. In practice the LLM prompt at find-time already
returns clean structured metadata (title, date, venue, description). We
skip the extract-metadata separate pass for v1 and mark events
`state=metadata-extracted` directly. When per-event Firecrawl re-fetch
matters (e.g. images, RSVP URLs, price), a dedicated extract-metadata
skill can be built and slot into the workqueue.

## CLI

```
find.py                       # scrape every approved seed
find.py --seed-id N           # scrape one seed by id
find.py --limit N             # cap seeds this run (default: all approved)
find.py --dry-run             # scrape + LLM, print without insert
```

## Per-seed loop

1. Firecrawl-scrape seed URL (markdown format — LLM parses better than raw HTML).
2. LLM prompt: "given this scraped page for town {town}, extract upcoming events as JSON array". Return exactly the fields the events table needs.
3. For each returned event, upsert via `event_uid = sha1(seed_name + normalized_title + start_iso)`.
4. Set `state=metadata-extracted`, `approval_status=pending`, `discovered_via=seed:<name>`.
5. Record `source_runs` row for the seed with fetched/inserted counts.
6. Update `event_seeds.last_scraped_at` + `last_event_count`.

## Notes

- **No affinity filter** in v1 — everything the LLM extracts lands as pending. Operator manually rejects out-of-town noise via the approval UX. Later: LLM-scored `in_town_confidence` filters low-scoring events into a "nearby" tab.
- **Idempotency**: rerunning against the same seed dedupes on `event_uid`; already-approved events are not clobbered.
- **Cost**: 1 Firecrawl call + 1 LLM call per seed. 13 approved seeds → ~26 calls per run.
