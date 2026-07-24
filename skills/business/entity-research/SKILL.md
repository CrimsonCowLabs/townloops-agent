---
name: entity-research
description: Fill category_slug on entity rows (businesses/communities/venues/performers/creators) that the discovery + promotion pipeline couldn't auto-classify. Keyword-map first; single-turn Anthropic Messages call as fallback (only if ANTHROPIC_API_KEY is set). Runs in the orchestrator's `researching` phase.
---

# entity-research

Second-pass classifier for entity rows created by `business-discovery`,
`community-discovery`, or events mining that landed without a
`category_slug`. Two subcommands (`classify`, `list`); the first is what
the orchestrator calls, the second is operator inspection.

## Commands

```
python skills/business/entity-research/research.py classify --kind KIND [--limit N]
python skills/business/entity-research/research.py list     --kind KIND [--limit N]
```

`--kind` is one of `business`, `community`, `venue`, `performer`, `creator`.

`classify` output is a one-line JSON status: `{kind, table, processed,
map_hits, llm_hits, still_unclassified}`.

## Resolution order

1. **Keyword map** (`engine.policy.resolve_category_slug`) — Python-side,
   no LLM. Fires when a single available slug matches the name +
   description hints.
2. **Anthropic Messages** — model defaults to `claude-haiku-4-5-20251001`
   (override via `$ANTHROPIC_MODEL`). Prompt is a one-shot classifier with
   the town's category snapshot as the constrained-choice list. Return
   must be in the list or is ignored.
3. **Give up** — leave `category_slug` NULL; row will be reported as
   unclassified in the tick summary, and the shipping phase will skip it
   until an operator or a future sweep resolves it.

## Env

- `TOWNLOOPS_TOWN_SLUG`, `HERMES_HOME` — resolves DB path + categories snapshot.
- `ANTHROPIC_API_KEY` — optional. Absent → step 2 is skipped entirely.
- `ANTHROPIC_MODEL` — optional override; default is the smallest current Haiku.

## Non-goals

- Not for discovery. Use `business-discovery` / `community-discovery`.
- Not for enrichment (address/phone/socials/description). Enrichment
  skills (`business-enrichment`, etc.) handle that.
- Not for shipping to TownLoops — `event-ingest ship` (all entity kinds)
  does that.
