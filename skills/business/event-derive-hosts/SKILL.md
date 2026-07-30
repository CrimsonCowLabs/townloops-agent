# event-derive-hosts

Workqueue action: **extract-hosts**. Runs for events in
`state=venue-derived`.

Identifies the hosting business or community (e.g. "Kings Beach District
Advisory Committee" is a community; "Alibi Ale Works" is a business).
Result may legitimately be NULL — per plan adversarial pass #18, "some
events genuinely have no extractable host" (fireworks, general
community events).

## v1: rule-based, LLM-free

For now this is a light rule-based pass:
- If the event title contains recognizable community/civic keywords
  (committee, council, board, church, library, foundation), create a
  shell `communities` row named after the title (trimmed of the
  meeting-noise suffix).
- Otherwise leave `derived_host_uid` NULL and advance the state anyway.

The heavier LLM version (given title+description+venue, extract the
hosting org and classify business-vs-community) is deferred to v2.
NULL results are treated as success — the orchestrator advances state.

## Contract

```
derive.py --event-uid <uid>
```

Exit 0 always (NULL host is legit). Non-zero only on DB errors.
