# Source tricks

From the CMO's per-source notes. `engine/feed_probe.py` detects most of these
on a seed page; this is what to do with them.

- **Wix-wrapped Google Calendar** — a `filesusr.com/html/…` iframe that is only
  a Google Calendar shell; the real calendar URL is passed by `postMessage` and
  never appears in HTML. A visible calendar is not dated evidence: don't
  extract events from it. The venue/business metadata on the site is still
  usable. Record the seed as needing a real feed.
- **WordPress Tribe Events** (`/wp-json/tribe/events/v1/events`) — pass
  `start_date`/`end_date`, read `total`/`total_pages`/`next_rest_url` and follow
  every page; `per_page` is capped (50 seen) whatever you ask for. Strip a
  trailing `/YYYY-MM-DD/` to group recurring occurrences, but keep each date.
  Venue may be a list or missing — handle it.
- **EventCalendarApp** — `api.eventcalendarapp.com/events?id=<cal>&page=<n>&widgetUuid=<uuid>`
  from the widget; page until the last page.
- **POINT** (volunteer embeds) — `pointapp-…/api/v1/shared/frames/events?token=…&page=<n>&size=<m>`
  from the iframe; times are epoch ms in the item's `timezone`.
- **Regional / county / basin-wide feeds** — filter to this town by venue
  city or address; out-of-town rows are spillover, not events to ship.
- **Short links** (`link.seated.com`, bit.ly, …) — resolve to the final page
  before trusting identity or geography; see
  `operator-audit-upcoming/references/upcoming-audit-playbook.md`.
- **Facebook login walls** — `og:`/meta description and canonical link are
  supporting evidence only; prefer the organiser or ticketing page.
- A 2xx response is not a working source: branded 404s, bot challenges and
  "no events" widgets are failed runs (see `manual-source-automation-review.md`).
