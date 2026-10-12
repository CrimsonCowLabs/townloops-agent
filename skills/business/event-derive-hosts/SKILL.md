# event-derive-hosts

Workqueue action: **extract-hosts**. Runs for events in
`state=venue-derived`.

Identifies the hosting business or community (e.g. "Kings Beach District
Advisory Committee" is a community; "Alibi Ale Works" is a business).
Result may legitimately be NULL — per plan adversarial pass #18, "some
events genuinely have no extractable host" (fireworks, general
community events).

## How: one quote-checked AI pass (`engine/event_people.py`)

The event's title and description, plus its own detail page when
event-read-detail read one (cached), go to Codex once; the answer is cached
on the event (`extra_json.people`) and shared with event-derive-people.

- The host's **name must appear in that text** (normalized) — the LLM may
  only copy, never infer.
- Its **kind** — `business`, `community` (club, group, association, church…)
  or `nonprofit` — is kept only with a quote from the text that supports it,
  or when the name itself says so ("… Foundation", "… Club", "… LLC").
- Unverifiable → NULL host; the state still advances.
- A non-profit is a `communities` row with `kind` 'nonprofit' (TownLoops has
  no separate non-profit resource); a community group gets `kind` 'group'.
- An existing local row of the same name is reused; else a shell is made for
  event-enrich-entities to research.
- A host event-reconcile already resolved (`derived_host_uid`) is kept.
- **A venue is never the host**: a name that matches the event's own venue,
  or any local or live venue (shared name match, so "Cascade Kitchens Food
  Hall & Bar" is venue Cascade Kitchens), gives no host — logged
  `host-is-venue` with the venue. Live venues come from the file cache
  shared with event-derive-venue; a failed lookup retries like a failed AI
  call.

A failed AI call exits 1 (the workqueue retries); on the last attempt it
exits 0 with no host instead of parking the event.

## Contract

```
derive.py --event-uid <uid>
```

Exit 0 when the host is resolved or legitimately NULL; 1 to retry a failed AI
call; 2 when the event doesn't exist.

## References

- `references/entity-classification.md` — business vs venue vs community: organizer vs the series it runs, rooms inside a larger property, when a place is both.
