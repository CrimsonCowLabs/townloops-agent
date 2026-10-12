# performer-research

Researches performers and creators linked to events and fills their blank
fields (links, bio/description, image) — but only from a public profile the
evidence ties to the local booking. A performer's name alone proves nothing:
common names and stage names match strangers. All logic lives in
`engine/performer_research.py`; this script is the batch driver.

`event-enrich-entities` skips performers and creators (it would match by
name search alone); this skill picks them up from `enrichment_status =
'pending'`.

## The evidence ladder

1. **Event evidence** (local DB): linked events, their venues (the bound
   venue row, else the event's own venue name — an event shipped name-only
   still names it), recurrence, the event pages. Recorded for the operator;
   never proves identity.
2. **Venue / event pages**: a link the page gives to the performer's own
   site or profile (labelled with their name, or carrying it in the URL) is
   a bridge. A booking without a link is event evidence only.
3. **Public-profile metadata**: the candidate's page title and meta
   description. Name match = the exact name in the title (or, for a
   venue-linked profile, in its handle). Locality = the town name or a
   linked venue named in the profile. Role = words that say "this is an act"
   (musician, band, singer, DJ, touring, …) for performers, or a maker
   (artist, potter, photographer, author, …) for creators — not bare genres
   ("country living") or "tour" as in tourism.
   An act's **own site** (host carries the name) that names the act and a
   role but not the town gets one more look: its own shows / tour-dates
   page — the page itself when it carries a "Tour dates"/"Shows" section,
   else its same-site shows link (one extra read) — listing the town or a
   linked venue is locality. Recorded as the candidate's `shows_page`.
   Touring and tribute acts verify this way (detail:
   `references/touring-acts.md`).
4. **Search, last**: `"<name>" <town>` only *discovers* candidates whose
   result title carries the name. A search snippet never verifies; the
   candidate's own page must.

A candidate (from either rung) must look like the performer's **own
presence**: a platform profile (Instagram/TikTok/X handle, Facebook page,
Spotify artist, YouTube channel, LinkedIn `/in/`) — not a post, event or
video — or a site whose host carries the name (`moe.org`,
`janedoe.bandcamp.com`); a site the venue links to needn't carry it, but
then only its homepage counts (a deeper page is an article *about* them). Ticketing and
listing hosts (Ticketmaster, Live Nation, Eventbrite, Bandsintown, …), event
paths (`/event/`, `/tickets/`, …) and the event's own source host are never
candidates: they name the performer and the town, but they are the booking.
A site's `website` is recorded as its root.

## Outcomes

| Finding | When | Row |
|---|---|---|
| verified | name match + (venue-page link, or locality + role) | blanks filled, `enriched` |
| probable | one profile with the name and a role *or* locality, but not both | `needs-review`, nothing filled |
| ambiguous | several such profiles, or two verified profiles disagree | `needs-review`, nothing filled |
| unresolved | no profile names them, or only empty shells do (name, no role, no locality) | `not-found` |
| deferred | budget ran out, search failed or returned nothing, or the only candidates couldn't be read (fetch failed, bot challenge, block/error page, or an empty shell with no title) | stays `pending`; the 3rd deferral in a row → unresolved |

A Firecrawl 401/403 (search or scrape) aborts the run with exit 1 rather
than deferring every row.

Every outcome is written to `extra_json.research` (status, why, event count,
venues, event URLs, each candidate checked with its signals and the fields
it would fill, `description_source`).

## No invented bios

Description (performers) / bio (creators) is the profile's own meta
description, verbatim — the Instagram bio inside its
`N Followers … on Instagram: "…"` wrapper, a Facebook page's text after its
like-count prefix. Follower-count boilerplate, an empty profile, or a
probable/ambiguous match all leave it **null**. No template copy
("Catch X live at Y…") is ever written. Images from Instagram/Facebook are
skipped (their CDN URLs expire); others follow the shared image rules
(`engine/research.image_candidates`: no logos/placeholders, HEAD-verified).

## Operator review

```bash
python performers.py review                                   # needs-review entities + candidates
python performers.py resolve --kind performer --entity-uid UID --accept URL   # attach that candidate
python performers.py resolve --kind performer --entity-uid UID --reject       # none of them → not-found
```

`--accept` fills blanks from the fields recorded for that candidate when it
was researched (no re-fetch). Only `needs-review` rows can be resolved.
The queue also holds the CMO's imported research drafts (`scripts/import_cmo.py`,
status `imported`; performers and communities): the draft is the one candidate,
so `--accept <draft url>` fills the entity from it and `--reject` drops it. Review items also show on the dashboard
ticker as failed `needs-review` activity.

## Schedule & budget

```bash
python performers.py run [--limit 3] [--lookups 6]
```

Cron `performer-research` runs every 20 minutes via
`scripts/performer_research.py`, which detaches the run (Hermes kills cron
scripts after 120s) and logs to `state/logs/performer-research-<date>.log`.
An flock (`performer-research`) stops overlapping runs. ≤3 entities per run,
each with its own budget of 6 Firecrawl searches + page reads (≤2 event
pages, 1 search, ≤3 candidate pages, a shows page per own-site candidate
that needs one); a Firecrawl fallback read asks for the raw page (its
cleaned `html` drops the `<head>` the ladder judges by); scrapes share `engine.policy`'s
Firecrawl QPS bucket. Never-researched entities go first, most-booked
first; after that, deferred ones take turns, longest-waiting first.

Never writes to TownLoops. Filled fields refresh `content_hash`, so an
already-shipped performer gets them live via `entity-live-sync`. A bare
performer/creator (name only) not yet `enriched` — pending, deferred,
not-found or in review — is held back from TownLoops by event-ship; once
verified (or accepted with `resolve --accept`) `entity-live-sync` creates it
when one of its events is live (`engine/performer_research.awaits_research`).
