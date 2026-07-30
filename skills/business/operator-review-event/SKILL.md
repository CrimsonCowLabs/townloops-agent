# operator-review-event

**When to use:** the operator asks about a single specific event — "review the yoga event", "what's on for the citizens advisory board?", "show me event abc123…".

Fuzzy-matches events by title, venue, or event_uid prefix. Returns:
- **1 match** → full metadata payload the agent can summarize + propose approve/decline actions.
- **N matches** → a numbered disambiguation list; agent asks the operator which one.
- **0 matches** → empty result the agent can report as "nothing matches".

Read-only. Complements `operator-list-events` (which is a status/filter listing rather than a single-event lookup).

## CLI

```
review.py --query "yoga"
review.py --query "kings beach community"
review.py --query 66df1313                     # event_uid prefix
review.py --query "yoga" --limit 5              # cap disambiguation list
```

`--limit` bounds the disambiguation list, default 5, max 20.

## Output shape

```json
{"query": "…",
 "match_count": 3,
 "matches": [
   {"event_uid": "…", "title": "…", "date": "…", "venue": "…",
    "location": "…", "description": "…", "source": "…", "category": "…",
    "approval_status": "pending", "state": "metadata-extracted"},
   …
 ]}
```

When `match_count == 1`, the single match is the full record. When >1,
each match is trimmed to the disambiguation-relevant fields.
