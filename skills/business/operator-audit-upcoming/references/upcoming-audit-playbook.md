# Upcoming-window audit playbook

Distilled from the CMO's `event-gap-audit-next-30-days`,
`event-window-missing-venue-image-audit`, `upcoming-event-metadata-audit`,
`music-calendar-null-venue-cluster-audit`, `broken-venue-clue-resolution`,
`venue-image-sourcing-and-patch-pattern`, `staged-venue-image-review` and
`official-calendar-and-shortlink-verification` notes.

## Window and paging

- Lock the window in town-local dates. The API filters on UTC; fetch past the
  local end and re-bucket by local date (the skill does this).
- Page with `limit` + `offset` (100 per page is safe); `page`/`per_page` are
  ignored. Check the parsed row count before reporting a thin feed.

## Gaps vs sources

`not_live` covers events our seeds found. For a source-vs-live diff beyond
that, match on title + local date + source URL + venue + series-vs-instance
meaning — never title only — and bucket each as already live / likely missing
/ represented another way. Report only "likely missing" as targets.

## Null or vague venues

1. Group venue-less rows by `source_url` and by the venue clue in the
   description ("at … in <town>").
2. Bucket clues: **safe** (full venue name), **likely truncation**
   (`Noel's Coffee a`, `@Heaven`), **ambiguous** (`B`, `Ba`).
3. Confirm against the *current* source page for that date — descriptions go
   stale. Only safe, confirmed rows get a venue; the rest go to operator review
   with evidence and a high/medium/low confidence.
4. Composite strings ("AleWorX @ the Y x The Cocktail Corner"): bind the one
   canonical live venue, keep the partner in the description.
5. Location-varies events (volunteer work days, "various locations") are
   venue-less by design — don't force one.

## Off-town or redirecting source_url

A short link (e.g. `link.seated.com/<uuid>`) is not evidence. Resolve it to
the final page and check headliner, venue, city and date. If the target is
another town, another headliner or a different event, it's an identity
problem: send it to operator review (correct / reject / relocate) — do not
enrich from it and do not just add a venue.

## Venue images

Events carry no image; the site shows the linked venue's (or business's /
community's). Audit the venues, not events.

- Order: official og:image / twitter:image → official hero or facility photo
  → first-party CDN asset on the official site → the parent property's image
  for a room inside it (say you used a fallback).
- Reject: logos, social icons, generic city/org banners, "Image Unavailable"
  placeholders, anything not https.
- Verify the URL returns 200 with an image content type; always set
  `image_alt`. No trustworthy image → leave null and list the slug.

## Sparse descriptions

Fill from the official event/organiser page's own text, or a verified sibling
occurrence of the same series. Thin page → a short factual summary of what the
page says; never marketing copy. Flag instead of fixing: junk/test rows,
unresolvable venue mismatches, out-of-area spillover from a regional feed
(a source-filter fix, not a description fix).

## Reporting

Separate live counts from local ones, say what is fixable now versus needs
operator review and why, and after any fix re-run the audit before calling it
done.
