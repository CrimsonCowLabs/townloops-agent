# Business vs venue vs community

Distilled from the CMO's `farmers-market-community-vs-organizer`,
`parent-property-internal-space-triage`, `community-to-business-audit` and
`cuppa-business-vs-venue-ship-repair` notes. Applies to `event-derive-hosts`,
`event-derive-venue`, business discovery and any operator classification ask.

## The three kinds

- **Venue** — a place an event happens at. Event venue binding needs a live
  *venue* row; a live *business* of the same name does not bind
  (`venue_slug` comes back null). A place can be both — ship both.
- **Business** — commercially operated, public-facing under its own name,
  discoverable through its own site; people visit, book, dine, shop or call it.
- **Community** — a recurring public gathering or participation-oriented
  group people follow: farmers markets, concert series, weekly meetups, clubs,
  nonprofits and civic bodies without a business-facing operation.

## The event's own venue is never its host

A host that is the event's own venue is never its host business or
community — the venue link already carries it. A suffix variant is the same
place: "Cascade Kitchens Food Hall & Bar" organizing "Plates at Cascade
Kitchens" is venue `cascade-kitchens`, not a new business. A name that adds a
club or non-profit word to the venue's ("… College Foundation") is an
organisation of its own.

A host that is some *other* venue is a genuine host — a brewery that is also
a TownLoops venue, pouring at a festival elsewhere. It is kept, and logged
`host-matches-other-venue` so a misfire is easy to spot.

Enforced by `engine/derive_helpers.own_venue_named` in event-derive-hosts and
event-reconcile; a dropped host is logged `host-is-venue` with the venue.

## Organizer vs the series it runs

Ask: *what do attendees identify with and return to?*

- The recurring program (the market, the concert series) → **community**.
- The event company / promoter running several programs → **business**,
  unless its own site frames it as a membership/participation community.
- Example: SunSnow Event Company → business; Meyers Mountain Market and Ski
  Run Farmers' Market → communities.

## Rooms inside a larger property

A bar, lounge, showroom, stage or shop inside a resort/casino/hotel/marina is
a **venue** (events happen there) but usually **not a separate business**:
roll it up to the parent property's business.

- Skip as business: URL under the parent's dining/nightlife section, no own
  address or phone, copy reads like an amenity, parent already on TownLoops.
- Keep as business: own site or durable canonical page, own address/phone or
  reservation flow, people search for it by name (e.g. Wolf by Vanderpump).
- Record the reason in one line: "internal lounge on parent casino property;
  no standalone address/phone".

## Usually not a business

Libraries, beaches, parks, public land and infrastructure (venues), arts or
community organisations (communities), anything too ambiguous to verify.

## Guardrails

- Null beats wrong; ambiguous → `needs-review`, not a guess.
- Descriptions come from the entity's own page text, never synthesised.
- Minimum to keep a business: name, official website, street address, phone,
  category, source_url.
