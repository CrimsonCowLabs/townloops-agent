---
name: local-data
description: The agent's own SQLite stores — the schema reference for writing SQL against townloops.db, the one-line log for a direct local edit, and backups of the state DBs.
---

# local-data

The town's local stores are the agent's own: it may query and change
`state/townloops.db` directly (sqlite3, short scripts) whenever it judges it
necessary. The skill verbs (event-seed-discovery, autonomy, operator-*) stay
the way to make a change that needs their side effects — decision records,
holds, workqueue rows. Logic: `engine/local_data.py`.

## Before writing SQL

Read `references/schema.md` — every table and column, generated from the
migrations (never edit it by hand; a test fails when it drifts). After a
code update, `python local-data/local_data.py schema` prints the current one.

## Usage

```bash
python local-data/local_data.py log --reason "merged duplicate venues riva-grill/riva-grill-2" \
    --table venues --rows 2                                  # after a direct edit
python local-data/local_data.py backup                       # state/backups/<db>-<stamp>.db, last 7 kept
python local-data/local_data.py schema [--out PATH]          # the reference, regenerated
```

- **Log every direct edit** with a one-line reason: one `agent_activity` row
  (worker `analyst`, action `local-edit`, target the table) the operator can
  see. Expected, not enforced.
- **Back up first** for anything large: `backup` uses SQLite's online backup
  (safe while other jobs write). The analyst run's `town-report budget start`
  takes one automatically. Restore = stop the agent, copy a backup over
  `state/townloops.db`.
- Scratch tables and views are fine; prefix them `scratch_` so they're
  recognisable, and drop what you no longer need.
