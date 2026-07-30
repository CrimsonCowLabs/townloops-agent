# event-seed-discovery

Uses the LLM (Codex CLI) to propose seed source URLs for a town — chamber
of commerce, city government, county government, library, tourism board,
parks & rec, local newspaper, big venues. These become the starting points
for the daily `event-find` cron.

Runs weekly (Sunday 04:00 UTC) and on first-run from `town-init`. New
proposals land as `event_seeds.status='proposed'` for the operator to
approve, reject, or edit in the dashboard's Seeds panel.

## CLI

```
discover.py                 # runs LLM discovery, HEAD-verifies each URL,
                            # inserts survivors as status=proposed. Idempotent
                            # (skips URLs already in event_seeds).
discover.py --limit N       # max N new seeds this run (default 15)
discover.py --dry-run       # print the LLM's proposals without inserting
discover.py --show          # print current event_seeds contents
```

## Prompt design (LLM)

Given: town name, aliases, region.

Prompt asks Codex CLI to return a compact JSON array of seed candidates,
each with `name`, `url`, `kind`. Kinds are constrained to a fixed set
(chamber / government / tourism / library / parks / newspaper / venue /
other).

## URL verification

Every LLM-proposed URL is HEAD-checked via Firecrawl (bypasses squid).
A 2xx response is required; 4xx/5xx URLs are dropped without ever
reaching the operator (defends against LLM hallucination).

## Auto-approve gate

Nothing is auto-approved. The operator sees the proposals in the
dashboard and approves per-seed. This is intentional — a bad seed dumps
junk events downstream that eat approval bandwidth.
