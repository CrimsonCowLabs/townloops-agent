# event-enrich-entities

Workqueue action: **enrich-entities**. Runs for events in
`state=people-derived`.

Walks the derived venue + host + performers/creators for this event and
fills in the enrichment fields that are still NULL (website, phone,
address for venue; website + socials for performers). Uses Codex CLI
via LLM prompts constrained by Firecrawl fetches of the derived name.

## v1: pass-through

For M3 v1 this skill is a pass-through that only marks each linked entity
as `enrichment_status='attempted'` and advances the event's state. The
real Firecrawl+LLM enrichment loop is deferred to v2.

Rationale: shipping v1 unblocks the ship skill (M4) and lets a full
approve → shipped roundtrip succeed against TownLoops even though the
entities are shell records. Enrichment can then run as a background
sweep independent of the workqueue.

## Contract

```
enrich.py --event-uid <uid>
```

Exit 0 on success (state advances). Non-zero on DB errors.
