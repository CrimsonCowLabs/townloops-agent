# Business discovery — handoff to per-town operator

Web-wide discovery of candidate businesses (find URLs that *should* be TownLoops businesses but aren't yet) is a **CMO-level operator function**, not a per-town operational loop. This doc explains how the two sides connect from the perspective of the per-town operator running this repo.

For the reasoning behind the split, see `docs/STATUS.md` § "Business discovery" and the P2 review dated 2026-07-08.

## Big picture

```
┌──────────────────────────────┐          ┌──────────────────────────────┐
│  CMO Hermes (~/.hermes/)     │          │  Per-town agent (this repo)  │
│                              │          │                              │
│  townloops-business-         │          │  event-ingest                │
│    discovery                 │          │    → mines businesses as     │
│    → sweeps web              │          │      event side-effect       │
│    → verifies                │          │  business-enrichment         │
│    → promotes to draft       │          │    → fills details on rows   │
│                              │          │      already in the local DB │
│  townloops-research          │          │                              │
│    → URL → payload           │          │                              │
│    → ships via API           │          │                              │
└──────────────────────────────┘          └──────────────────────────────┘
                │                                       │
                └───────────► TownLoops API ◄───────────┘
                        POST /towns/<slug>/businesses
```

Both sides POST to the SAME API endpoint. The dedup is enforced server-side (slug collision → `409 slug_taken` → idempotent) and client-side (CMO's discovery fetches the existing businesses list before adding candidates).

**Ownership boundary**: the CMO decides which businesses exist. This per-town agent enriches whatever's in its local DB. It does not scan the web.

## What the per-town operator needs to know

1. **New businesses may appear in TownLoops without going through this repo.** If you see a business row you don't recognize, check `townloops-research/data/drafts.db` on the CMO side — it may have been shipped from there.
2. **You cannot "add a new business" from here.** `event-ingest add-manual` inserts *events* + mined organizer side-effects; it's not a business creation flow. If a client asks "please add Alibi Ale Works as a sponsor-eligible business," that's a CMO ticket.
3. **Enrichment IS your job.** Once a business row lands in this town's `businesses` table (whether from event mining or from a CMO ship), `business-enrichment sweep` fills its address, phone, website, image, and socials.

## The `cross-check` subcommand

To see what the CMO has drafted-but-not-shipped for potential handoff:

```bash
export TOWNLOOPS_RESEARCH_DRAFTS_DB=/path/to/cmo/townloops-research/data/drafts.db
python skills/business/business-enrichment/enrich.py cross-check
python skills/business/business-enrichment/enrich.py cross-check --town-hint "South Lake Tahoe"
```

Output: JSON with the unshipped business draft names, URLs, addresses, and saved-at timestamps. Read-only — this does not touch the CMO's DB.

`--town-hint <substring>` filters the address column (drafts.db has no `town_slug` column; heuristic filter by address text).

**This subcommand does NOT ship anything.** Shipping from a draft happens on the CMO side via `research.py ship`. If you see a draft here that belongs to your town, message the operator to ship it — do not attempt to POST it from this repo.

### How the CMO's `TOWNLOOPS_RESEARCH_DRAFTS_DB` gets mounted

- **Local dev / same workstation as CMO**: point directly at `~/.hermes/skills/business/townloops-research/data/drafts.db`.
- **Docker deploy**: bind-mount the drafts.db path into the container and set the env var. Example additions to `docker-compose.yml`:
  ```yaml
  services:
    agent:
      volumes:
        - /path/on/host/to/townloops-research/data:/opt/townloops-research-drafts:ro
      environment:
        TOWNLOOPS_RESEARCH_DRAFTS_DB: /opt/townloops-research-drafts/drafts.db
  ```
- **Remote CMO (different host)**: not supported by `cross-check` today. Operator would need to `scp` or `rsync` the drafts.db over. Alternatively — flag this as a future feature: expose `/drafts` on the CMO's Hermes gateway.

## Escalation triggers — when to open a CMO ticket

- A client asks to add a business you can't source from event ingest.
- `cross-check` shows unshipped drafts for your town that have been sitting >7 days.
- `business-enrichment sweep` keeps producing the same low-quality "unknown" rows and you suspect discovery's verifier is being too permissive.
- You want the CMO to sweep a new category (e.g. "outdoor recreation") that isn't in the town's category list yet.

For all of the above, edit the CMO skills at `~/.hermes/skills/business/townloops-{business-discovery,research}/` — do not add discovery logic to this repo.
