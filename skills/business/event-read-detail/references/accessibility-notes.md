# Accessibility notes — what counts

Accessibility is a TownLoops differentiator, and a wrong claim does real harm
(someone in a wheelchair turns up to a venue with stairs). So the rule is the
page's own words or nothing.

## Counts (when the event's own page says it)

- Wheelchair / step-free access, ramps named as wheelchair ramps, elevators
- Accessible or ADA seating, parking, restrooms, entrances, viewing areas
- ASL / sign-language interpretation, captioning, audio description
- Assisted/assistive listening, hearing or induction loops
- Sensory-friendly or relaxed performances, quiet rooms
- Service animals, large print / braille, companion seating

Keep the source sentence as written (whitespace and markdown aside). Contact
details for access requests ("email access@… two weeks ahead") are useful;
keep them when they are in the same sentence.

## Never counts

- "All welcome", "everyone welcome", "all ages", "family friendly"
- "Accessible by trail / by boat / by shuttle", "easily accessible from Hwy 50"
- A site-wide "Accessibility statement" link or "committed to digital accessibility"
- A listing page's blanket "all our venues are accessible" for one event's notes
- The venue type ("community center, so it must be accessible")
- Paraphrase or summary — if it isn't on the page, it isn't a note

## Gaps on purpose

Seating or parking lines that don't say "accessible", "ADA" or "wheelchair"
("reserved seating for guests with walkers") are dropped by the subject check.
Null is safer than a false positive. If a real source keeps hitting this,
widen `_ACCESS_TERMS` in `engine/event_detail.py` with a test.

## Live values

A live event's own `accessibility_notes` (CMO or operator edit) is never
overwritten; ours fills a blank only.
