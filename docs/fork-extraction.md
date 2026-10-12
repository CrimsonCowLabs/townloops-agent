# Fork extraction — how to migrate SLT to the shared `ingest_core`

`skills/business/event-ingest/ingest.py` started as a verbatim copy of the SLT skill's `~/.hermes/skills/business/south-lake-tahoe-events/events.py`. Over time, per-P0/P1/P3 fixes, they drifted — every change to one file required a manual port to the other, or a bug survived in half the world.

**Fork extraction complete as of 2026-07-09.** Two phases moved the drift-prone kernel into `engine/ingest_core.py`:

- **Phase 1 (2026-07-08)** — dataclasses (`Event`/`Venue`/`Business`/`Community`/`Performer`/`Creator`/`MinedEntity`), `ENTITY_REG`, pure utils (`_clean`/`_truncate`/`_norm`), venue heuristics, timezone helpers with `AGENT_TZ`-backed `DEFAULT_TZ`, identity + content hashing.
- **Phase 2 (2026-07-09)** — the parser layer: `_http_get`, `_firecrawl_endpoint`, `_firecrawl_scrape`, `_detail_links_from_html`, `walk_jsonld`, `_jsonld_to_event`, `_extract_jsonld_from_html`, `_text`, `_attr_or_text`, `_extract_selectors_from_html`, `parse_ical`, `parse_jsonld`, `parse_rss`, `parse_selectors`, `parse_web`, the `PARSERS` registry, and the enrichment overlay (`_extract_enrichment_field`, `_fetch_for_enrichment`, `_apply_enrichments`).

`ingest.py` went from 2627 → 1737 lines (−890, 34%). `ingest_core.py` is 1051 lines. This doc explains what's shared, what's not, and how SLT can eventually adopt the shared core.

## What's in `engine/ingest_core.py` today

Everything a source-schema or parser change would touch:

- **Dataclasses**: `Event`, `Venue`, `Business`, `Community`, `Performer`, `Creator`, `MinedEntity`.
- **`ENTITY_REG`** — kind → `{table, endpoint, cls, cols, hash_cols}`. Add a new payload field here, both ingest paths pick it up.
- **Pure text utils**: `_clean`, `_truncate`, `_norm`.
- **Venue-guessing heuristics**: `_venue_from_location`, `_extract_venue_from_description`, `_should_replace_venue`.
- **Timezone helpers**: `_now_pt`, `_iso_now`, `_iso`, `_parse_dt`. Backed by module-level `DEFAULT_TZ` (env `AGENT_TZ`, falls back to `America/Los_Angeles`).
- **Identity + content hashing**: `_event_uid`, `_content_hash`, `_entity_uid`, `_entity_content_hash`.
- **HTTP fetch layer**: `_http_get`, `_firecrawl_endpoint`, `_firecrawl_scrape`, `_detail_links_from_html`. Constants `FIRECRAWL_CLOUD_BASE`, `DEFAULT_MAX_DETAIL_FETCHES`.
- **JSON-LD walker + Event synthesis**: `walk_jsonld`, `_jsonld_to_event`, `_extract_jsonld_from_html`.
- **CSS-selector helpers**: `_text`, `_attr_or_text`, `_extract_selectors_from_html`.
- **Top-level parsers**: `parse_ical`, `parse_jsonld`, `parse_rss`, `parse_selectors`, `parse_web`.
- **`PARSERS` registry** — `{ical, jsonld, rss, selectors, web} → callable`.
- **Enrichment overlay**: `_extract_enrichment_field`, `_fetch_for_enrichment`, `_apply_enrichments`.

## What's NOT extracted — per-deployment forever

- CLI subcommands (`_cmd_*`).
- DB path resolution + migration runner (the migrations SHOULD be shared; that's already true — `MIGRATIONS_DIR` points at `/app/engine/migrations`).
- Ship functions (`_bulk_post`, `_patch_one`) — deeply tied to town-scoped config.
- Idempotency-key + slug generation (town-scoped by design after the 2026-07-08 P3.3 fix).
- Lock + cross-town-guard wiring.
- DB upserts (`_upsert_event`, `_upsert_entity`) — SQL is town-DB-specific.
- Entity mining (`_mine_entities`, `_derive_venue_address`) — technically shareable but tightly interleaved with mining_extras + source config quirks. Leave for a future Phase 3 if it turns out to be a drift hotspot.

## Migrating SLT to the shared core

There are three viable vendoring options, ordered by permanence:

### Option A: git submodule (permanent, requires coordination)

Split `engine/ingest_core.py` (plus its future parser siblings) into its own tiny repo. Both townloops-agent and SLT add it as a submodule. Bug fixes land in the shared repo and are pulled by both. Best long-term, highest setup cost.

### Option B: PYTHONPATH mount (recommended near-term)

Add `~/workspace/townloops-agent/engine` to SLT's `PYTHONPATH`, then have events.py do:

```python
sys.path.insert(0, "/home/aaron/workspace/townloops-agent")
from engine.ingest_core import (
    # Phase 1 — schema + utils
    Event, Venue, Business, Community, Performer, Creator, MinedEntity,
    ENTITY_REG, ENTITY_KINDS,
    _clean, _truncate, _norm,
    _venue_from_location, _extract_venue_from_description, _should_replace_venue,
    DEFAULT_TZ as PT,
    _now_pt, _iso_now, _iso, _parse_dt,
    _event_uid, _content_hash, _entity_uid, _entity_content_hash,
    _HTML_TAG_RE,
    # Phase 2 — HTTP fetch + parsers + enrichment
    _http_get, _firecrawl_endpoint, _firecrawl_scrape,
    _detail_links_from_html,
    FIRECRAWL_CLOUD_BASE, DEFAULT_MAX_DETAIL_FETCHES,
    walk_jsonld, _jsonld_to_event, _extract_jsonld_from_html,
    _text, _attr_or_text, _extract_selectors_from_html,
    parse_ical, parse_jsonld, parse_rss, parse_selectors, parse_web,
    PARSERS,
    _extract_enrichment_field, _fetch_for_enrichment, _apply_enrichments,
)
```

…then delete all the corresponding definitions from events.py. Zero infrastructure, immediate drift elimination for the extracted surface.

Risk: SLT gains a dependency on the townloops-agent checkout being present. If townloops-agent is moved or renamed, SLT breaks. Mitigate by making the townloops-agent checkout path a shell env var (`INGEST_CORE_PATH`) so it can move.

### Option C: hard copy with a shared-checksum guard

Copy `engine/ingest_core.py` verbatim into SLT's skill dir. Add a CI/pre-commit check that fails if the two copies differ. Simplest today but has to be maintained; if the check ever gets ignored, you're back where you started.

## Migration checklist (Option B)

For each of the following, verify events.py behaves identically after the change:

1. **Backup**: `cp events.py events.py.pre-fork-extraction`
2. **Path fix**: add the `sys.path.insert(0, "/home/aaron/workspace/townloops-agent")` above imports
3. **Import the extracted surface** (copy the block above verbatim)
4. **Delete**:
   - Phase 1 kernel: the seven dataclasses, `ENTITY_REG`/`ENTITY_KINDS`, `_now_pt`/`_iso_now`/`_iso`/`_parse_dt`, `_clean`/`_truncate`/`_norm`, `_venue_from_location`/`_extract_venue_from_description`/`_should_replace_venue`, `_event_uid`/`_content_hash`/`_entity_uid`/`_entity_content_hash`, `_HTML_TAG_RE`/`_VENUE_FROM_DESC_RE_1`/`_VENUE_FROM_DESC_RE_2`.
   - Phase 2 parser layer: `_http_get`, `_firecrawl_endpoint`, `_firecrawl_scrape`, `_detail_links_from_html`, `_walk_jsonld` (now `walk_jsonld` in the core — rename its callers), `_jsonld_to_event`, `_extract_jsonld_from_html`, `_text`, `_attr_or_text`, `_extract_selectors_from_html`, all five `parse_*` parsers, the `PARSERS` registry, `_extract_enrichment_field`, `_fetch_for_enrichment`, `_apply_enrichments`.
   - Constants `FIRECRAWL_CLOUD_BASE` and `DEFAULT_MAX_DETAIL_FETCHES`.
5. **Keep** — SLT-specific `PT` fallback isn't needed; `DEFAULT_TZ as PT` yields the same object because both sides read `AGENT_TZ`. If SLT wants to hardcode PT regardless, do `PT = tz.gettz("America/Los_Angeles")` AFTER the import, overriding.
6. **Run SLT's existing verification**: `events.py fetch --dry-run` against a few sources. Row counts, dedupe keys, uids should be byte-identical.

## Known gotchas

- **`Business`/`Community`/`Performer`/`Creator` gained social fields on 2026-07-08 (P1.1)**. If SLT's events.py has narrower dataclasses, adopting the shared core WIDENS them. Downstream SLT DB migrations must land first, or SLT's shipping code will silently drop the new fields (harmless, but you lose the capability).
- **`ENTITY_REG` gained the same social columns**. Any SLT code that iterates `ENTITY_REG['business']['cols']` will iterate a longer list.
- **`_norm` uses NFKD without accent stripping**. Both sides must match on this or dedupe keys diverge. Verify no drift here before migrating.
- **Timezone**: shared `DEFAULT_TZ` reads `AGENT_TZ`. If SLT's process env doesn't set `AGENT_TZ`, it falls back to `America/Los_Angeles` — same as SLT's original hardcode. No behavior change expected.

## Sequencing note (historical)

Phase 1 landed 2026-07-08. Phase 2 landed 2026-07-09 the next day — Phase 1's smoke tests came back clean and the parser layer was ready to move without introducing an `IngestConfig` dataclass (all runtime knobs — timezone, Firecrawl endpoint, max detail fetches — are already read from module state / env at call time, so parsers slot in with only a `PT → DEFAULT_TZ` rename). Future callers who want testable parser injection can add `IngestConfig` non-invasively later without touching the fork boundary.

### If you're doing a Phase 3

Only two things remain that would be worth extracting: entity mining (`_mine_entities`, `_derive_venue_address`) and per-source ingest orchestration (`_ingest_source`). Both are more tangled with mining_extras + source config quirks than the Phase 1/2 targets were. Do NOT extract unless a real drift bug forces the issue; the marginal drift risk from `_mine_entities` is far lower than the parsers were.
