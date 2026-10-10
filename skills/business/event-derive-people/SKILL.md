# event-derive-people

Workqueue action: **extract-people**. Runs for events in
`state=hosts-derived`.

Extracts performers (bands, comedians, speakers) and creators (artists,
chefs, workshop leaders) referenced in the event description or title.
Inserts one shell row per person into `performers` or `creators` and
links via `linked_events`.

## How: the same quote-checked AI pass as event-derive-hosts

`engine/event_people.py` asks Codex once per event (cached on the event) for
named performers and creators. A name is kept only if it appears in the
event's own text (title, description, detail page); the venue's and the
host's names are never people. At most five. Each becomes a shell
`performers` (`kind` 'unknown') or `creators` (`kind` 'other') row — or
reuses a local row of the same name — linked by `linked_events`.

Identity is **not** decided here: the `performer-research` batch walks the
evidence ladder before a profile is attached (never inferred from a name).

## Contract

```
derive.py --event-uid <uid>
```

Exit 0 when people are linked or legitimately none; 1 to retry a failed AI
call (on the last attempt: 0, no people); 2 when the event doesn't exist.
