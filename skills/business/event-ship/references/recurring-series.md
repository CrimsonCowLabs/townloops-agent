# Series-shared enrichment

TownLoops groups the Events that recur under one name into a **Series**
(ADR 0007): `series_slug` / `series_id` on every Event resource, `?series=`
on the event list. That is the grouping key — not title similarity, not
`source_url`. A **Cluster** (titles that normalize to one key) with no Series
is only a candidate: the weekly `series-candidates` report lists the ones
that pass TownLoops' rule for a curator to create. The agent never creates,
renames or archives a Series.

1. Select the run by `series_slug` (`GET events?series=<slug>`). The filter
   is `is_active`-agnostic: an archived Series still groups, it just isn't a
   link target. An event with no `series_slug` has no siblings yet — don't
   assemble a run from title families.
2. Copy only **shared** fields, and only into blanks (live values win):
   description (without a `Tickets:`/`RSVP:` line), categories, tags (minus
   `free`, `cancelled`, `sold-out`), accessibility notes, and the venue only
   when every sibling with a resolved venue is at the same one. Never
   per-occurrence identity: date, start/end, slug, title, price/ticket line,
   cancellation marks. Never take from a cancelled sibling.
3. Ship each fill as a full-body PATCH (engine/live_patch) and GET-verify.
   event-ship does this for every event it ships into a Series;
   entity-live-sync's daily pass does it for the upcoming occurrences this
   agent shipped (`engine/series_sync.py`).
4. Title rules are idempotent — add a prefix or a "<Performer> at <Venue>"
   suffix only when absent, never twice.

A destination calendar's series title can map to a more specific live
occurrence (a "Music on the Beach" listing ↔ that night's named-performer
card) — match by Series and date (`?series=` + the town-local day), then
source URL and venue, not title, before calling it missing. Two live
occurrences of one Series on one date are a duplicate for the operator,
never a guess.
