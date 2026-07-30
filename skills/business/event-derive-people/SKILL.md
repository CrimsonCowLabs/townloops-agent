# event-derive-people

Workqueue action: **extract-people**. Runs for events in
`state=hosts-derived`.

Extracts performers (bands, comedians, speakers) and creators (artists,
chefs, workshop leaders) referenced in the event description or title.
Inserts one shell row per person into `performers` or `creators` and
links via `linked_events`.

## v1: rule-based, LLM-free

For now this is a lightweight pass over event description:
- If category is `music` and title matches "<Artist Name> at/live at/@ <Venue>",
  extract artist as a `performers` row.
- Otherwise NULL — advance and move on.

Full LLM extraction ("read this description and list all named
performers/creators") is deferred to v2 to keep Codex quota reserved
for enrichment.

## Contract

```
derive.py --event-uid <uid>
```

Exit 0 always (NULL is legit). Non-zero only on DB errors.
