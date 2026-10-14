---
name: operator-audit-listing-websites
description: Read-only audit of venues, businesses and communities whose website is a directory or listing page (restaurantji, Yelp, maps, ticketing) — locally, live, or both.
---

# operator-audit-listing-websites

## When to use

The operator asks which entities have a listing page as their website: "which
businesses link to restaurantji?", "audit listing-page websites", "how many
venues point at Yelp?", or wants the scope of the listing-website cleanup
before anything changes.

Read-only. It only LISTs/GETs TownLoops and reads the local DB; it proposes
nothing and changes nothing — not even a merged entity's re-point. The check
is `engine/listing_hosts.is_listing_url`, the one listing-host list; a host
missing from it is added there, not here.

## CLI

```bash
python skills/business/operator-audit-listing-websites/audit.py
python skills/business/operator-audit-listing-websites/audit.py --limit 50
```

- `--limit`: max rows, 1–1000, default 200. `total`, `counts` and `by_kind` are never capped.

## Output

```json
{
  "total": 3,
  "counts": {"local": 1, "live": 1, "both": 1},
  "by_kind": {"venue": 1, "business": 2, "community": 0},
  "rows": [{"kind": "business", "name": "Cascade Kitchens Food Hall & Bar", "side": "both",
            "url": "https://www.restaurantji.com/ca/south-lake-tahoe/cascade-kitchens-food-hall-and-bar-/",
            "entity_uid": "…", "live_slug": "cascade-kitchens", "live_id": 7,
            "local_website": "https://www.restaurantji.com/…", "live_website": "https://www.restaurantji.com/…",
            "live_unread": false, "duplicate_of": null}],
  "unverified": [{"kind": "venue", "entity_uid": "…", "live_slug": "…", "error": "…"}]
}
```

- One row per local entity (or per live entity no local row pairs with),
  ordered by kind (venue, business, community) then name.
- `side` — which side's `website` is a listing page: `local`, `live` or
  `both`. A local row pairs with a live row from the live listing by its
  `townloops_slug`, then by `extra_json.live_id`, and only then by a GET of
  the slug that follows a curator's merge to the survivor (the local row is
  not re-pointed).
- `url` — the listing URL (live's when both are listings). `local_website`
  and `live_website` are each side as stored: they can be two different
  listing pages, or one side may hold the entity's own site. `null` means
  that side has no row (`entity_uid` / `live_slug` null too) or a blank
  website — except on a `live_unread` row.
- `live_unread: true` — the live GET failed (an API error other than a 404),
  so the live side is **unknown**, not absent: the row is judged on its local
  website and also listed in `unverified`. Its live row may appear as a
  separate `live` row under another slug. Re-run later; don't clear from it.
- `duplicate_of` — several local rows on one live row (duplicates, or rows
  merged into one survivor) count the live listing once, on the primary
  (paired by slug, then live_id, then merge; then lowest `entity_uid`). The
  others name the primary here and are judged on their local website alone.
- Live rows come from the town's entity listing. Whether TownLoops lists
  entities still pending moderation isn't pinned down here; a pending one no
  local row points at may be missing. Local rows with a slug still reach
  theirs by GET.

## Reading the result

- Report counts first, and say which are live, which local and which both —
  the operator needs to know what is actually on TownLoops.
- A `local` row whose `live_website` is an own site: live is already right;
  only the local copy is wrong.
- Clearing is a separate, operator-gated step (`listing-website-cleanup`);
  this audit never clears a website. Don't PATCH a website from these rows by hand.
