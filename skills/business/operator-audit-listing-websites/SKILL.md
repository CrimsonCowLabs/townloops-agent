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
            "local_website": "https://www.restaurantji.com/…", "live_website": "https://www.restaurantji.com/…"}],
  "unverified": []
}
```

- One row per entity, ordered by kind (venue, business, community) then name.
- `side` — which side's `website` is a listing page: `local`, `live` or
  `both`. Local and live are paired as entity-sync pairs them:
  `townloops_slug`, else `extra_json.live_id`, else a GET following a
  curator's merge to the survivor.
- `url` — the listing URL (live's when both are listings). `local_website`
  and `live_website` are each side as stored: they can be two different
  listing pages, or one side may hold the entity's own site. `null` means
  that side has no row (`entity_uid` / `live_slug` null too) or a blank website.
- `unverified` — local rows whose live slug couldn't be read (an API error
  other than a 404); they're judged on the local side only. Re-run later.

## Reading the result

- Report counts first, and say which are live, which local and which both —
  the operator needs to know what is actually on TownLoops.
- A `local` row whose `live_website` is an own site: live is already right;
  only the local copy is wrong.
- Clearing is a separate, operator-gated step; this audit never clears a
  website. Don't PATCH a website from these rows by hand.
