# Duplicate venues and entities — operator proposals only

From the CMO's `duplicate-venue-delete-audit` and `duplicate-entity-canonicalization`
notes. I never DELETE on TownLoops; I build the evidence and the operator decides.

1. List every live row of the duplicated name with `id`, `slug` and richness
   (`engine/derive_helpers.entity_richness`). The richer row is canonical
   unless a stronger source of truth says otherwise.
2. Count live events on the duplicate by **both** `venue_slug` and `venue_id`,
   and confirm the canonical row carries the events it should.
3. Check the local DB for rows that still point at the duplicate (events'
   `derived_venue_uid`, entity `townloops_slug`), so a later ship doesn't
   recreate it.
4. Report to the operator: canonical vs duplicate, live event counts on each,
   local references, and whether the duplicate is safe to remove (zero live
   events, zero local references). Rebinding events to the canonical row is a
   normal full-body PATCH (`venue_id`); removing the duplicate is theirs.
5. After the operator deletes it, a GET of the duplicate slug should 404.

Prevention beats cleanup: event-derive-venue matches live venues before
creating one, and event-ship looks live up before POSTing.
