# event-enrich-entities

Workqueue action: **enrich-entities**. Runs for events in
`state=people-derived`.

Researches the venue and host business/community linked to the event
(`linked_events`) from the entity's own site and fills fields that are still
blank. All logic lives in `engine/entity_enrich.py` (`enrich_entity`,
`SPECS`); this script is the per-event driver.

Performers and creators are **skipped** (noted as "left for
performer-research"): a name search can't tell a local act from a namesake,
so the scheduled `performer-research` batch researches them along its
evidence ladder.

## What it does per entity

1. **Skip** live mirrors (`source_kind='live'` — their values are
   TownLoops') and rows already researched (`enrichment_status` in
   `enriched | not-found | image-proposed | live | needs-review`).
2. **Find the site**: the row's `website`, else its `facebook` page, else a
   Firecrawl search for `"<name>" <town>` (first hit plausibly the entity's
   own site; directories/aggregators are blocked).
   `meeting_info` / `accessibility_notes` are not extracted here (no
   structured source).
3. **Read it** (kind-aware fetch plan): a Facebook page root also reads its
   `/about/` tab, where contact details and LocalBusiness JSON-LD live.
4. **Extract verbatim**: description from og:description / meta description as written (never
   synthesized), address/phone/socials from JSON-LD, socials from `sameAs`.
   Creators get `bio` and `youtube_url` per the payload contract. A Facebook
   URL goes in `facebook`, never `website`.
5. **Validate** each value against `engine/entity_payload.schema.json`
   (strings; http(s) URLs for links; https for `image_path`). Invalid values
   are dropped — null beats wrong.
6. **Fill blanks only** (`fill_blanks`) — never overwrites an operator- or
   live-sourced value — and record `enrichment_status` = `enriched` or
   `not-found`.

Image rules: og:image → JSON-LD `image` (never `logo`) → hero `<img>`;
logos, icons, placeholders, SVG/GIF/ICO and data URIs are rejected; each
candidate must HEAD 200 as `image/*`. No image beats a generic one. After the
entities are done, a venue that is a room inside a property (its website is
on the same site as a linked business/community — shared platforms like
Facebook or Linktree don't count) and still has no image borrows the
property's image.

## Budget

`TOWNLOOPS_ENRICH_MAX_LOOKUPS` (default 8) caps Firecrawl searches + page
reads per event. When it runs out before an entity's site is read, the
entity stays `pending` (reported as `deferred`) rather than being marked
not-found. Firecrawl scrapes also share `engine.policy.firecrawl_bucket`'s
QPS ceiling, and the workqueue's `enrich-entities` per-hour rate limit applies.

## Contract

```
enrich.py --event-uid <uid>
```

- Exit 0 once the event's entities are dealt with — including not-found and
  deferred — so the event advances to `entities-enriched` and never goes
  `stuck-` over a hard-to-find entity. The per-entity outcome is recorded in
  `agent_activity` (`enriched` action).
- Exit 2 if the event doesn't exist; exit 1 if Firecrawl rejects the API key
  (workqueue retries, then parks the event for the operator).

Never writes to TownLoops. Getting enrichment of already-shipped entities
live is the PATCH path's job (issue 09); new entities ship enriched with the
event.
