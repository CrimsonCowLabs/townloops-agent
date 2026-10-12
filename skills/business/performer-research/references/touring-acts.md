# Touring and tribute acts — what bridges them

A touring act's public presence never names the town: its homepage is about
the act, and its bio is the same everywhere. A tribute act's page title also
carries the subject ("Direct from Sweden - The Music of ABBA"). Learned on
SLT, 2026-10: Direct From Sweden (sosdirectfromsweden.com) was found by
search, recorded `unresolved` with `venues: []`, and shipped bare.

## The booking's venue is evidence even when no venue row is bound

An event that shipped name-only (ambiguous live venue, autonomous mode) has
no `linked_events` venue, but `events.venue_name`/`venue_raw` still say where
it is. That name is what an own site's tour dates list ("Harrah’s Lake
Tahoe"), so it belongs in the event evidence.

## The act's own shows page is the locality bridge

- Only on the act's **own site** (host carries the name) that already names
  the act and a role. A platform profile, a listing host or an unrelated
  site gets no such look.
- The shows page is the site's own: a "Tour dates"/"Shows"/"Concerts"
  section on the page itself, or a same-site link labelled or pathed that
  way. An off-site tour widget (Bandsintown, Songkick) is a listing, not
  theirs.
- It must list **this town or a linked venue**. An upcoming date elsewhere
  only is name + role → `probable`, the operator's call.
- The name alone still proves nothing: no role, no shows page → no verify.

## An unreadable page is not a page that fails to name the act

A bot challenge ("Just a moment…", "Access denied"), an error page, or an
empty shell with no `<title>` (Firecrawl's cleaned `html` format drops the
whole `<head>`) is **unread** → `deferred`, bounded by `MAX_DEFERRALS`.
Recording it as read is what turned a findable act into `not-found`.

## Names

The stored name stays the act's own ("Direct From Sweden"). The tribute
subject is never added to it; it reaches the row only inside the act's own
verbatim description.
